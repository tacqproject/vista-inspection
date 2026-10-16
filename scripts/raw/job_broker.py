#!/usr/bin/env python3
"""Host-side bounded Docker jobs. The web app never gets the Docker socket.

Only inspect/run of installed, pinned images is allowed. Writable bind mounts
must resolve under the RAW data root; other explicitly listed roots are RO.
No shell evaluation, builds, host networking, privileges or arbitrary mounts.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import select
import shutil
import socketserver
import subprocess
import threading
import uuid

DOCKER = '/usr/bin/docker'
IMAGES = {'vista-meshroom:latest', 'vista-openmvs:2.3-cuda', 'vista-openmvs:2.3',
          'vista-dsine:latest', 'vista-defects:latest'}
ENV_NAMES = {'HOME', 'LD_LIBRARY_PATH', 'ALICEVISION_ROOT', 'OMP_NUM_THREADS',
             'OPENBLAS_NUM_THREADS', 'MPLCONFIGDIR', 'XDG_CACHE_HOME'}
_slot = threading.Semaphore(1)


def under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def validate(argv: list[str], root: Path, readonly: list[Path], uid: str,
             pinned: dict[str, str]) -> tuple[list[str], str | None]:
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        raise ValueError('Expected argument array')
    if len(argv) == 3 and argv[:2] == ['image', 'inspect'] and argv[2] in pinned:
        return [DOCKER, 'image', 'inspect', pinned[argv[2]]], None
    if not argv or argv[0] != 'run':
        raise ValueError('Only installed image inspect / restricted run is allowed')
    passed = []
    i = 1
    while i < len(argv) and argv[i].startswith('-'):
        flag = argv[i]
        if flag == '--rm':
            i += 1
            continue
        if flag not in {'-v', '--volume', '-e', '--env', '-w', '--workdir', '-u', '--user', '--gpus'}:
            raise ValueError(f'Forbidden Docker option: {flag}')
        if i + 1 >= len(argv):
            raise ValueError('Missing option value')
        value = argv[i + 1]
        i += 2
        if flag in {'-v', '--volume'}:
            parts = value.split(':')
            if len(parts) not in (2, 3):
                raise ValueError('Expected absolute bind mount')
            src, dst = Path(parts[0]), Path(parts[1])
            mode = parts[2] if len(parts) == 3 else 'rw'
            if not src.is_absolute() or not src.exists() or not dst.is_absolute() or '..' in dst.parts:
                raise ValueError('Invalid mount path')
            real = src.resolve()
            if mode not in {'ro', 'rw'}:
                raise ValueError('Forbidden mount mode')
            if not under(real, root) and not (mode == 'ro' and any(under(real, p) for p in readonly)):
                raise ValueError('Mount escapes RAW storage or read-only sources')
            if not (under(dst, root) or str(dst) in {'/meshroom', '/in', '/out', '/defects', '/masks', '/tmp/MeshroomCache'}
                    or (mode == 'ro' and any(under(dst, p) for p in readonly))):
                raise ValueError('Forbidden mount destination')
            passed += ['-v', f'{real}:{dst}:{mode}']
        elif flag in {'-u', '--user'}:
            if value != uid:
                raise ValueError('User must match RAW service owner')
        elif flag == '--gpus':
            if value != 'all':
                raise ValueError('Unsupported GPU selector')
            passed += [flag, value]
        elif flag in {'-e', '--env'}:
            if value.partition('=')[0] not in ENV_NAMES or '=' not in value:
                raise ValueError('Environment variable not allowed')
            passed += ['-e', value]
        else:
            if not Path(value).is_absolute() or not under(Path(value).resolve(), root):
                raise ValueError('Working directory must be inside RAW storage')
            passed += ['-w', value]
    if i >= len(argv) or argv[i] not in pinned:
        raise ValueError('Image is not in the installed allowlist')
    name = 'vista-raw-job-' + uuid.uuid4().hex[:16]
    command = [DOCKER, 'run', '--rm', '--name', name, '--label', 'vista.instance=raw',
               '--read-only', '--network', 'none', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--user', uid,
               '--cpus', '8', '--memory', '24g', '--memory-swap', '24g', '--pids-limit', '512',
               '--tmpfs', '/tmp:rw,size=4g,mode=1777',
               '-e', 'OMP_NUM_THREADS=8', '-e', 'OPENBLAS_NUM_THREADS=4',
               *passed, pinned[argv[i]], *argv[i + 1:]]
    return command, name


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    def send(self, frame):
        self.wfile.write(json.dumps(frame).encode() + b'\n')
        self.wfile.flush()

    def handle(self):
        name = None
        proc = None
        try:
            line = self.rfile.readline(131073)
            if len(line) > 131072:
                raise ValueError('Request too large')
            command, name = validate(json.loads(line), self.server.root, self.server.readonly,
                                     self.server.uid, self.server.pinned)
            with _slot:
                if name and shutil.disk_usage(self.server.root).free < self.server.reserve:
                    raise RuntimeError('SSD free space is below RAW reserve; free space before retrying')
                print(json.dumps({'event': 'start', 'name': name, 'command': command}), flush=True)
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                while proc.poll() is None:
                    ready, _, _ = select.select([proc.stdout, self.connection], [], [], 1)
                    if self.connection in ready and not self.connection.recv(1, __import__('socket').MSG_PEEK):
                        raise ConnectionError('Client disconnected')
                    if proc.stdout in ready:
                        data = os.read(proc.stdout.fileno(), 65536)
                        if data:
                            self.send({'data': base64.b64encode(data).decode()})
                    if name and shutil.disk_usage(self.server.root).free < self.server.reserve:
                        raise RuntimeError('RAW job stopped to preserve SSD free-space reserve')
                tail = proc.stdout.read()
                if tail:
                    self.send({'data': base64.b64encode(tail).decode()})
                self.send({'exit': proc.returncode})
                print(json.dumps({'event': 'exit', 'name': name, 'code': proc.returncode}), flush=True)
        except Exception as exc:
            if name:
                subprocess.run([DOCKER, 'kill', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=20)
            if proc and proc.poll() is None:
                proc.terminate()
            try:
                self.send({'data': base64.b64encode(f'RAW broker: {exc}\n'.encode()).decode()})
                self.send({'exit': 125})
            except (BrokenPipeError, ConnectionError):
                pass
            print(json.dumps({'event': 'error', 'name': name, 'error': str(exc)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--readonly', type=Path, action='append', default=[])
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--reserve-gb', type=float, default=10)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    forbidden = [Path('/home/epr/VISTA'), Path('/media/epr/ssd/vista-work')]
    if any(under(root, p) or under(p, root) for p in forbidden):
        raise SystemExit('Refusing unsafe RAW root')
    pinned = {}
    for tag in sorted(IMAGES):
        result = subprocess.run([DOCKER, 'image', 'inspect', '--format', '{{.Id}}', tag], capture_output=True, text=True)
        if result.returncode == 0:
            pinned[tag] = result.stdout.strip()
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    os.umask(0o077)
    with Server(str(args.socket), Handler) as server:
        server.root, server.readonly = root, [p.resolve(strict=True) for p in args.readonly]
        server.uid = f'{os.getuid()}:{os.getgid()}'
        server.pinned, server.reserve = pinned, args.reserve_gb * 1e9
        print('RAW broker ready', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
