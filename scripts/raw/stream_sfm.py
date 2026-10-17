#!/usr/bin/env python3
"""Export Alembic SfM with bounded JSON batches using bundled AliceVision IO.

Run with the isolated Python 3.11 runtime and Meshroom's read-only bindings.
Native SfM stays in memory; the second, much larger property tree contains
only one batch. Native serialization preserves every landmark/observation
field, including descriptor types, colors, feature coordinates and scales.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time

BATCH_POINTS = 5000


def signature(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def export(source: Path, output: Path) -> dict:
    from pyalicevision import sfmData, sfmDataIO

    before = signature(source)
    started = time.monotonic()
    scene = sfmData.SfMData()
    if not sfmDataIO.load(scene, str(source), sfmDataIO.ALL):
        raise RuntimeError(f'Cannot load completed SfM: {source}')
    landmarks = scene.getLandmarks()
    total = len(landmarks)
    if not total:
        raise RuntimeError('Completed SfM has no landmarks')
    print(f'Loaded native SfM: {total} landmarks', flush=True)
    with tempfile.TemporaryDirectory(prefix='.sfm-export-', dir=output.parent) as temp:
        folder = Path(temp)
        header_path = folder / 'header.json'
        if not sfmDataIO.save(scene, str(header_path), sfmDataIO.ALL & ~sfmDataIO.STRUCTURE):
            raise RuntimeError('Cannot export SfM camera metadata')
        header = json.loads(header_path.read_text())
        header.pop('structure', None)
        batch_scene = sfmData.SfMData()
        batch = batch_scene.getLandmarks()
        batch_path = folder / 'batch.json'
        pending = folder / 'complete.json'
        count = observations = 0
        with pending.open('w', buffering=1024 * 1024) as out:
            out.write(json.dumps(header, ensure_ascii=False, separators=(',', ':'))[:-1])
            out.write(',"structure":[')

            def flush():
                nonlocal count, observations
                if shutil.disk_usage(output.parent).free < 10e9:
                    raise RuntimeError('SfM export stopped to preserve the 10 GB SSD reserve')
                flags = sfmDataIO.STRUCTURE | sfmDataIO.OBSERVATIONS_WITH_FEATURES
                if not sfmDataIO.save(batch_scene, str(batch_path), flags):
                    raise RuntimeError('Cannot export SfM landmark batch')
                data = json.loads(batch_path.read_text())['structure']
                if len(data) != len(batch):
                    raise RuntimeError('Incomplete SfM landmark batch')
                for landmark in data:
                    if count:
                        out.write(',')
                    out.write(json.dumps(landmark, separators=(',', ':')))
                    count += 1
                    observations += len(landmark.get('observations', []))
                batch.clear()
                if count % 100000 == 0 or count == total:
                    print(f'Exported {count}/{total} landmarks; {observations} observations', flush=True)

            for key, landmark in landmarks.iteritems():
                batch[key] = landmark
                if len(batch) == BATCH_POINTS:
                    flush()
            if len(batch):
                flush()
            out.write(']}\n')
            out.flush()
            os.fsync(out.fileno())
        if count != total or signature(source) != before:
            raise RuntimeError('SfM changed or export is incomplete')
        pending.replace(output)
    return {'landmarks': count, 'observations': observations, 'bytes': output.stat().st_size,
            'seconds': time.monotonic() - started,
            'peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}


def main():
    p = json.loads(sys.argv[1])
    result = Path(p['result'])
    try:
        data = export(Path(p['sfm']), Path(p['output']))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        data = {'error': str(exc)}
    pending = result.with_name(result.name + '.pending')
    pending.write_text(json.dumps(data, indent=2))
    pending.replace(result)
    print(json.dumps(data), flush=True)
    return 1 if 'error' in data else 0


if __name__ == '__main__':
    sys.exit(main())
