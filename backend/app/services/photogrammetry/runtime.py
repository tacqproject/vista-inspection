"""Bounded, restartable stages for the isolated RAW instance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from app.config import settings


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def identity(path: Path) -> dict:
    st = path.stat()
    return {'path': str(path.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sfm_items(path: Path, key: str):
    """One landmark at a time, even for multi-GB AliceVision JSON exports."""
    import ijson
    with path.open('rb') as f:
        yield from ijson.items(f, key + '.item', use_float=True)


def sfm_header(path: Path) -> dict:
    # Stop at the end of each requested array; do not parse the large structure
    # array that AliceVision writes after the camera metadata.
    import ijson
    from itertools import takewhile
    result = {}
    for key in ('views', 'intrinsics', 'poses'):
        with path.open('rb') as f:
            events = takewhile(lambda e: not (e[0] == key and e[1] == 'end_array'),
                               ijson.parse(f, use_float=True))
            result[key] = list(ijson.items(events, key + '.item'))
    return result


class Checkpoints:
    """A step is reusable only when its inputs AND output identities match."""
    def __init__(self, root: Path):
        self.root = root
        self.path = root / 'checkpoints.json'
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def valid(self, name: str, inputs) -> bool:
        record = self.data.get(name, {})
        if record.get('key') != fingerprint(inputs):
            return False
        outputs = record.get('outputs', [])
        try:
            return bool(outputs) and all(identity(Path(x['path'])) == x and x['size'] > 0 for x in outputs)
        except OSError:
            return False

    def save(self, name: str, inputs, outputs: list[Path], **extra) -> None:
        identities = [identity(p) for p in outputs]
        if not identities or any(x['size'] == 0 for x in identities):
            raise RuntimeError(f'Prázdný výstup kroku {name}')
        self.data[name] = {'key': fingerprint(inputs), 'outputs': identities, **extra}
        atomic_json(self.path, self.data)


def require_space(path: Path, additional: int, stage: str) -> None:
    free = shutil.disk_usage(path).free
    need = int(settings.disk_reserve_gb * 1e9) + max(0, additional)
    if free < need:
        raise RuntimeError(f'{stage}: potřeba přibližně {need / 1e9:.1f} GB volného místa '
                           f'včetně rezervy, dostupných {free / 1e9:.1f} GB. '
                           'Hotové mezivýsledky zůstávají uložené.')


def run_python_stage(stage: str, payload: dict, log: Path) -> dict:
    """Python/native libraries run in their own 24 GiB container, never the API."""
    result = Path(payload['result'])
    result.unlink(missing_ok=True)
    cmd = (['docker', 'python-stage', stage, json.dumps(payload)] if os.environ.get('VISTA_JOB_SOCKET')
           else [sys.executable, '-m', 'app.services.photogrammetry.worker', stage, json.dumps(payload)])
    with log.open('a') as fh:
        fh.write(f'\nPython stage: {stage}\n'); fh.flush()
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode or not result.is_file():
        detail = ''
        if result.is_file():
            detail = json.loads(result.read_text()).get('error', '')
        raise RuntimeError(f'Krok {stage} selhal (kód {proc.returncode}). {detail} Detaily: {log}')
    data = json.loads(result.read_text())
    if 'error' in data:
        raise RuntimeError(data['error'])
    return data


def current_sfm(cache: Path) -> Path:
    graph = json.loads((cache / 'project.mg').read_text())['graph']
    nodes = [n for n in graph.values() if n.get('nodeType') == 'StructureFromMotion']
    if len(nodes) != 1:
        raise RuntimeError('Pipeline musí mít jednoznačný výstup StructureFromMotion.')
    uid = str(nodes[0]['uid'])
    if Path(uid).name != uid:
        raise RuntimeError('Neplatný identifikátor SfM')
    folder = cache / 'StructureFromMotion' / uid
    if json.loads((folder / 'status').read_text()).get('status') != 'SUCCESS':
        raise RuntimeError('SfM aktuálního běhu není dokončeno.')
    for name in ('sfm.abc', 'cameras.sfm'):
        if not (folder / name).is_file() or (folder / name).stat().st_size == 0:
            raise RuntimeError(f'Chybí dokončený SfM výstup {name}')
    return folder


def recover_interrupted_statuses(cache: Path) -> int:
    """Called under this inspection's exclusive reconstruction lock."""
    changed = []
    for path in cache.glob('*/*/*status'):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get('status') in ('RUNNING', 'SUBMITTED'):
            changed.append({'path': str(path), 'before': data})
    if changed:
        import time
        atomic_json(cache / f'interrupted-{time.time_ns()}.json', changed)
        for item in changed:
            data = dict(item['before'], status='NONE')
            atomic_json(Path(item['path']), data)
    return len(changed)
