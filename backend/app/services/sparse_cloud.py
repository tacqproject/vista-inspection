"""Stream SfM landmarks into the viewer's compact binary without loading tracks."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile


def _signature(path: Path) -> list[int]:
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def _fresh(source: Path, output: Path) -> bool:
    try:
        marker = output.with_name(output.name + '.source.json')
        if json.loads(marker.read_text()).get('source') != _signature(source):
            return False
        with output.open('rb') as f:
            count, = struct.unpack('<I', f.read(4))
        return output.stat().st_size == 4 + count * 15
    except (OSError, ValueError, struct.error):
        return False


def build_pointcloud(source: Path, output: Path) -> None:
    """[uint32 count][float32 mesh-frame XYZ][uint8 RGB], atomically published.

    Observations are consumed one landmark at a time. Colors are spooled to
    disk so even large clouds do not accumulate Python objects or large arrays.
    Concurrent requests reuse the first successful build under a file lock.
    """
    from app.services.photogrammetry.runtime import atomic_json, sfm_items
    source, output = Path(source), Path(output)
    with output.with_name(output.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _fresh(source, output):
            return
        before = _signature(source)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.pointcloud-',
                                             suffix='.tmp', delete=False) as positions, \
                    tempfile.TemporaryFile(dir=output.parent) as colors:
                temp = Path(positions.name)
                positions.write(b'\0' * 4)
                count = 0
                for lm in sfm_items(source, 'structure'):
                    x, y, z = map(float, lm['X'])
                    positions.write(struct.pack('<fff', x, -y, -z))
                    rgb = bytes(int(c) for c in lm.get('color', [200, 200, 200]))
                    if len(rgb) != 3:
                        raise ValueError('SfM landmark color must have three channels')
                    colors.write(rgb)
                    count += 1
                colors.seek(0)
                shutil.copyfileobj(colors, positions)
                positions.seek(0)
                positions.write(struct.pack('<I', count))
                positions.flush()
                os.fsync(positions.fileno())
            if before != _signature(source):
                raise RuntimeError('SfM changed while building its point cloud; retry the request')
            temp.replace(output)
            atomic_json(output.with_name(output.name + '.source.json'), {'source': before})
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
