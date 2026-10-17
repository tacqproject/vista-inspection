#!/usr/bin/env python3
"""Start only the isolated RAW containers on epr-bedna; production is untouched.

Requires an existing copied venv/node_modules and a built frontend. Existing
named containers are not removed: use docker restart after editing this branch.
"""
import os
import json
from pathlib import Path
import shlex
import subprocess

REPO = Path(__file__).resolve().parents[2]
ROOT = Path('/media/epr/ssd/vista-raw')
SOURCE = Path('/media/epr/ssd/vista-work/datasets/corvallis-bridge/VLABDW/photos')
MESHROOM = Path('/media/epr/ssd/Meshroom-2025.1.0')
NODE = ROOT / 'tools/node'
NETWORK = 'vista-raw-net'


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def main():
    if REPO != Path('/home/epr/git/vista-inspection-raw'):
        raise SystemExit('Deploy only from the separate vista-inspection-raw worktree')
    env = ROOT / 'operations' / 'backend.env'
    if not env.is_file() or not (ROOT / 'state/vista.db').is_file():
        raise SystemExit('Initialize the private environment and database first')
    values = dict(line.split('=', 1) for line in env.read_text().splitlines()
                  if line and not line.startswith('#') and '=' in line)
    sources = list(dict.fromkeys(Path(p).resolve(strict=True) for p in
                   [values.get('RAW_SOURCE_ROOT', str(SOURCE)),
                    *json.loads(values.get('RAW_SOURCE_ROOTS', '[]'))] if p))
    if any(p.is_relative_to(ROOT) or ROOT.is_relative_to(p) for p in sources):
        raise SystemExit('Read-only datasets must be separate from writable RAW storage')
    readonly = ' '.join('--readonly ' + shlex.quote(str(p)) for p in [*sources, MESHROOM, REPO])
    if subprocess.run(['/usr/bin/docker', 'network', 'inspect', NETWORK], capture_output=True).returncode:
        run('/usr/bin/docker', 'network', 'create', '--label', 'vista.instance=raw', NETWORK)

    unit = Path.home() / '.config/systemd/user/vista-raw-broker.service'
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f'''[Unit]
Description=Restricted job broker for isolated VISTA RAW

[Service]
Type=simple
ExecStart=/usr/bin/python3 {REPO}/scripts/raw/job_broker.py --root {ROOT} {readonly} --socket {ROOT}/state/jobs.sock
Restart=on-failure
RestartSec=5
UMask=0077
StandardOutput=append:{ROOT}/operations/broker.log
StandardError=append:{ROOT}/operations/broker.log

[Install]
WantedBy=default.target
''')
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'enable', '--now', 'vista-raw-broker.service')

    common = ['/usr/bin/docker', 'run', '-d', '--restart', 'unless-stopped', '--label', 'vista.instance=raw',
              '--network', NETWORK, '--read-only', '--cap-drop', 'ALL',
              '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
              '--pids-limit', '512', '--tmpfs', '/tmp:rw,size=1g,mode=1777',
              '-v', '/usr:/usr:ro', '-v', '/lib:/lib:ro', '-v', '/lib64:/lib64:ro',
              '-v', '/etc/ssl/certs:/etc/ssl/certs:ro', '-v', '/etc/fonts:/etc/fonts:ro',
              '-v', f'{REPO}:{REPO}:ro', '-e', 'HOME=/tmp', '-e', 'PYTHONDONTWRITEBYTECODE=1']
    backend = [*common, '--name', 'vista-raw-backend', '--cpus', '4', '--memory', '16g', '--memory-swap', '16g',
               '-p', '127.0.0.1:8001:8001', '--env-file', str(env),
               '-v', f'{ROOT}:{ROOT}:rw',
               *[arg for source in sources for arg in ('-v', f'{source}:{source}:ro')],
               '-v', f'{MESHROOM}:{MESHROOM}:ro', '-v', '/tmp/vista-gpu.lock:/tmp/vista-gpu.lock:rw',
               '-w', str(REPO / 'backend'), 'ubuntu:22.04',
               str(REPO / 'backend/.venv/bin/python'), '-m', 'uvicorn', 'app.main:app',
               '--host', '0.0.0.0', '--port', '8001']
    frontend = [*common, '--name', 'vista-raw-frontend', '--cpus', '1', '--memory', '512m', '--memory-swap', '512m',
                '-p', '5174:5174', '-v', f'{NODE}:/opt/node/bin/node:ro',
                '-e', 'VISTA_API_TARGET=http://vista-raw-backend:8001',
                '-w', str(REPO / 'frontend'), 'ubuntu:22.04', '/opt/node/bin/node',
                str(REPO / 'frontend/serve-raw.mjs')]
    for name, command in [('vista-raw-backend', backend), ('vista-raw-frontend', frontend)]:
        if subprocess.run(['/usr/bin/docker', 'inspect', name], capture_output=True).returncode == 0:
            print(f'{name} already exists; left untouched')
        else:
            print(name, run(*command))


if __name__ == '__main__':
    main()
