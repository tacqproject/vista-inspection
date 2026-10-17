#!/usr/bin/env python3
"""Review cached full-resolution feature matches against drone pose metadata.

This stage only creates an isolated COLMAP database and diagnostic reports.
It neither filters matches automatically nor promotes a reconstruction.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import resource
import shutil
import sys
import time

import numpy as np
import pycolmap as colmap

VERSION = 1
ROOT = Path('/media/epr/ssd/vista-raw')
WORKERS = 4


def atomic_json(path, data):
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def identity(path):
    s = path.stat()
    return {'path': str(path.resolve()), 'size': s.st_size, 'mtime_ns': s.st_mtime_ns}


def reserve(work):
    if shutil.disk_usage(work).free < 10e9:
        raise RuntimeError('SfM match review stopped to preserve the 10 GB SSD reserve')


def drone_rotation(metadata):
    yaw, pitch, roll = np.deg2rad([float(metadata[k]) for k in
                                 ('GimbalYawDegree', 'GimbalPitchDegree', 'GimbalRollDegree')])
    forward = np.array([np.sin(yaw)*np.cos(pitch), np.cos(yaw)*np.cos(pitch), np.sin(pitch)])
    right = np.array([np.cos(yaw), -np.sin(yaw), 0.0])
    down = np.cross(forward, right)
    return np.column_stack([right*np.cos(roll)+down*np.sin(roll),
                            -right*np.sin(roll)+down*np.cos(roll), forward])


def camera_parameters(intrinsic):
    if intrinsic.get('type', 'pinhole') != 'pinhole':
        raise ValueError('Only pinhole radial-k3 input is supported')
    if intrinsic.get('distortionType', 'radialk3') not in ('none', 'radialk3'):
        raise ValueError('Unsupported AliceVision distortion model')
    w, h = int(intrinsic['width']), int(intrinsic['height'])
    focal = intrinsic.get('pxFocalLength')
    if isinstance(focal, list):
        fx, fy = map(float, focal)
    elif focal is not None:
        fx = fy = float(focal)
    else:
        fx = float(intrinsic['focalLength']) / float(intrinsic['sensorWidth']) * w
        fy = fx / float(intrinsic.get('pixelRatio', 1))
    dx, dy = map(float, intrinsic.get('principalPoint', [0, 0]))
    distortion = [float(x) for x in intrinsic.get('distortionParams', [])]
    if len(distortion) not in (0, 3):
        raise ValueError('Expected the three AliceVision radial distortion coefficients')
    k1, k2, k3 = distortion or [0.0, 0.0, 0.0]
    return {'model': 'FULL_OPENCV', 'width': w, 'height': h, 'has_prior_focal_length': True,
            'params': [fx, fy, w/2+dx, h/2+dy, k1, k2, 0.0, 0.0, k3, 0.0, 0.0, 0.0]}


def read_matches(folder):
    """AliceVision text pairs, preserving every original feature index."""
    files = sorted(folder.glob('*.matches.txt'), key=lambda p: int(p.name.split('.')[0]))
    if not files:
        raise ValueError('No cached AliceVision match files')
    for path in files:
        with path.open() as f:
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                a, b = map(str, line.split())
                groups = int(f.readline())
                for _ in range(groups):
                    descriptor, number = f.readline().split()
                    number = int(number)
                    values = np.fromstring(' '.join(f.readline() for _ in range(number)),
                                           dtype=np.int64, sep=' ')
                    if values.size != number*2 or np.any(values < 0):
                        raise ValueError(f'Incomplete feature matches in {path}')
                    if descriptor != 'dspsift':
                        raise ValueError(f'Unexpected descriptor type {descriptor}')
                    yield a, b, values.reshape(-1, 2).astype(np.uint32)


def import_database(args):
    work = Path(args['work'])
    features, matches = Path(args['features']), Path(args['matches'])
    header = json.loads(Path(args['sfm']).read_text())
    metadata = {m['FileName']: m for m in json.loads(Path(args['metadata']).read_text())}
    ordered = sorted(header['views'], key=lambda v: Path(v['path']).name)
    inputs = {'version': VERSION, 'pycolmap': colmap.__version__,
              'sfm': identity(Path(args['sfm'])), 'metadata': identity(Path(args['metadata'])),
              'features': [identity(features / (v['viewId'] + '.dspsift.feat')) for v in ordered],
              'matches': [identity(p) for p in sorted(matches.glob('*.matches.txt'))]}
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    request = work / 'input-identity.json'
    if request.exists() and json.loads(request.read_text())['key'] != key:
        raise ValueError('Inputs changed; choose a new isolated match-review work directory')
    atomic_json(request, {'key': key, 'inputs': inputs})
    manifest_path = work / 'import.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest['key'] != key or not (work / 'database.db').is_file():
            raise ValueError('Invalid database import checkpoint')
        return manifest
    (work / 'features').mkdir(exist_ok=True)
    (work / 'matches').mkdir(exist_ok=True)
    pending = work / 'import-pending.db'
    for suffix in ('', '-wal', '-shm'):
        pending.with_name(pending.name + suffix).unlink(missing_ok=True)
    lla = np.array([[float(metadata[Path(v['path']).name][k]) for k in
                    ('GPSLatitude', 'GPSLongitude', 'GPSAltitude')] for v in ordered])
    origin = np.median(lla, axis=0)
    enu = np.column_stack([(lla[:, 1]-origin[1])*np.pi/180*6378137*np.cos(np.deg2rad(origin[0])),
                           (lla[:, 0]-origin[0])*np.pi/180*6378137, lla[:, 2]-origin[2]])
    intrinsics = {i['intrinsicId']: i for i in header['intrinsics']}
    cameras = {iid: {'camera_id': n, **camera_parameters(intrinsics[iid])}
               for n, iid in enumerate(sorted({v['intrinsicId'] for v in ordered}), 1)}
    rows, pairs = [], []
    with colmap.Database.open(pending) as db, colmap.DatabaseTransaction(db):
        for spec in cameras.values():
            db.write_camera(colmap.Camera(**spec), use_camera_id=True)
            sensor = colmap.sensor_t(colmap.SensorType.CAMERA, spec['camera_id'])
            rig = colmap.Rig(rig_id=spec['camera_id'])
            rig.add_ref_sensor(sensor)
            db.write_rig(rig, use_rig_id=True)
        for index, view in enumerate(ordered):
            image_id = index + 1
            vid = view['viewId']
            name = Path(view['path']).name
            md = metadata[name]
            camera_id = cameras[view['intrinsicId']]['camera_id']
            sensor = colmap.sensor_t(colmap.SensorType.CAMERA, camera_id)
            data_id = colmap.data_t(sensor, image_id)
            frame = colmap.Frame(frame_id=image_id, rig_id=camera_id)
            frame.add_data_id(data_id)
            db.write_frame(frame, use_frame_id=True)
            db.write_image(colmap.Image(image_id=image_id, camera_id=camera_id,
                                       frame_id=image_id, name=name), use_image_id=True)
            values = np.loadtxt(features / f'{vid}.dspsift.feat', dtype=np.float32, ndmin=2)
            if values.shape[1] != 4 or not np.isfinite(values).all():
                raise ValueError(f'Invalid features for {name}')
            np.save(work / 'features' / f'{image_id}.npy', values, allow_pickle=False)
            db.write_keypoints(image_id, np.ascontiguousarray(values[:, :2]))
            prior = colmap.PosePrior(pose_prior_id=image_id, corr_data_id=data_id,
                                    position=enu[index], position_covariance=np.diag([9.0, 9.0, 25.0]),
                                    coordinate_system=colmap.PosePriorCoordinateSystem.CARTESIAN)
            db.write_pose_prior(prior, use_pose_prior_id=True)
            rows.append({'id': image_id, 'view_id': vid, 'name': name, 'camera_id': camera_id,
                         'feature_count': len(values), 'enu': enu[index].tolist(),
                         'camera_to_enu': drone_rotation(md).tolist(), 'source': view['path']})
            if image_id % 100 == 0:
                reserve(work)
                print(f'Imported features: {image_id}/{len(ordered)} images', flush=True)
        by_view = {r['view_id']: r for r in rows}
        seen = set()
        for av_a, av_b, values in read_matches(matches):
            a, b = by_view[av_a], by_view[av_b]
            if values.size and (values[:, 0].max() >= a['feature_count'] or
                                values[:, 1].max() >= b['feature_count']):
                raise ValueError('Match feature index is outside its original feature array')
            if a['id'] > b['id']:
                a, b = b, a
                values = np.ascontiguousarray(values[:, ::-1])
            pair = [a['id'], b['id']]
            pair_key = f'{pair[0]}-{pair[1]}'
            if pair_key in seen:
                raise ValueError(f'Duplicate cached image pair {pair_key}')
            seen.add(pair_key)
            np.save(work / 'matches' / f'{pair_key}.npy', values, allow_pickle=False)
            db.write_matches(*pair, values)
            pairs.append(pair)
            if len(pairs) % 1000 == 0:
                reserve(work)
                print(f'Imported cached match pairs: {len(pairs)}', flush=True)
    pending.replace(work / 'database.db')
    manifest = {'key': key, 'views': rows, 'cameras': list(cameras.values()), 'pairs': pairs,
                'origin_lla': origin.tolist(), 'gps_position_covariance': [9, 9, 25]}
    atomic_json(manifest_path, manifest)
    return manifest


_worker = {}


def init_worker(work, manifest):
    _worker.update(work=Path(work), views={r['id']: r for r in manifest['views']},
                   cameras={s['camera_id']: colmap.Camera(**s) for s in manifest['cameras']})


@lru_cache(maxsize=16)
def keypoints(image_id):
    values = np.load(_worker['work'] / 'features' / f'{image_id}.npy', mmap_mode='r')
    return np.ascontiguousarray(values[:, :2], dtype=np.float64)


def review_pair(pair):
    a, b = pair
    va, vb = _worker['views'][a], _worker['views'][b]
    raw = np.load(_worker['work'] / 'matches' / f'{a}-{b}.npy', mmap_mode='r')
    options = colmap.TwoViewGeometryOptions(min_num_inliers=30, compute_relative_pose=True)
    options.ransac.random_seed = 0
    geometry = colmap.estimate_two_view_geometry(
        _worker['cameras'][va['camera_id']], keypoints(a),
        _worker['cameras'][vb['camera_id']], keypoints(b), raw, options)
    delta = np.asarray(va['enu']) - np.asarray(vb['enu'])
    distance = float(np.linalg.norm(delta))
    row = {'a': a, 'b': b, 'name_a': va['name'], 'name_b': vb['name'],
           'raw_matches': len(raw), 'inliers': len(geometry.inlier_matches),
           'config': geometry.config, 'gps_distance_m': distance,
           'rotation_error_deg': None, 'direction_error_deg': None}
    pose = geometry.cam2_from_cam1
    if pose is not None:
        r1, r2 = np.asarray(va['camera_to_enu']), np.asarray(vb['camera_to_enu'])
        expected = r2.T @ r1
        cosine = (np.trace(pose.rotation.matrix() @ expected.T) - 1) / 2
        row['rotation_error_deg'] = float(np.degrees(np.arccos(np.clip(cosine, -1, 1))))
        translation = np.asarray(pose.translation)
        if distance > .1 and np.linalg.norm(translation) > 1e-9:
            cosine = translation @ (r2.T @ delta) / (np.linalg.norm(translation) * distance)
            row['direction_error_deg'] = float(np.degrees(np.arccos(np.clip(cosine, -1, 1))))
    return row, geometry


def component_sizes(rows, count, predicate):
    parent = list(range(count+1))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    edges = inliers = 0
    for row in rows:
        if predicate(row):
            parent[find(row['a'])] = find(row['b'])
            edges += 1
            inliers += row['inliers']
    sizes = {}
    for i in range(1, count+1):
        root = find(i)
        sizes[root] = sizes.get(root, 0) + 1
    return {'pairs': edges, 'inliers': inliers, 'components': sorted(sizes.values(), reverse=True)}


def review(args):
    work = Path(args['work'])
    started = time.monotonic()
    manifest = import_database(args)
    target = work / 'pair-review.json'
    saved = json.loads(target.read_text()) if target.exists() else {}
    with colmap.Database.open(work / 'database.db') as db:
        todo = [pair for pair in manifest['pairs'] if f'{pair[0]}-{pair[1]}' not in saved
                or not db.exists_two_view_geometry(*pair)]
        print(f'Reviewing {len(todo)} pairs; {len(saved)} completed checkpoints', flush=True)
        def commit(batch):
            with colmap.DatabaseTransaction(db):
                for row, geometry in batch:
                    pair = row['a'], row['b']
                    if db.exists_two_view_geometry(*pair):
                        db.delete_two_view_geometry(*pair)
                    db.write_two_view_geometry(*pair, geometry)
            for row, _ in batch:
                saved[f'{row["a"]}-{row["b"]}'] = row
            atomic_json(target, saved)
            reserve(work)
            print(f'Geometric review: {len(saved)}/{len(manifest["pairs"])} pairs', flush=True)
        if todo:
            with mp.get_context('spawn').Pool(WORKERS, initializer=init_worker,
                                             initargs=(str(work), manifest)) as pool:
                batch = []
                for value in pool.imap_unordered(review_pair, todo, chunksize=1):
                    batch.append(value)
                    if len(batch) == 100:
                        commit(batch)
                        batch.clear()
                if batch:
                    commit(batch)
                pool.close()
                pool.join()
    rows = list(saved.values())
    good = lambda r: r['inliers'] >= 30 and r['rotation_error_deg'] is not None
    scenarios = {'verified': component_sizes(rows, len(manifest['views']), good)}
    for angle in (10, 20, 30):
        keep = lambda r, angle=angle: good(r) and r['rotation_error_deg'] <= angle
        scenarios[f'rotation_{angle}_deg'] = component_sizes(rows, len(manifest['views']), keep)
        scenarios[f'rotation_{angle}_deg_direction_60_deg'] = component_sizes(
            rows, len(manifest['views']), lambda r, keep=keep: keep(r) and
            (r['gps_distance_m'] < 12 or (r['direction_error_deg'] is not None and r['direction_error_deg'] <= 60)))
    angles = [r['rotation_error_deg'] for r in rows if good(r)]
    result = {'views': len(manifest['views']), 'pairs': len(rows), 'scenarios': scenarios,
              'rotation_error_median_p90_p95_p99_deg': np.quantile(angles, [.5, .9, .95, .99]).tolist() if angles else [],
              'worst_strong_pairs': sorted([r for r in rows if good(r) and r['inliers'] >= 100],
                                           key=lambda r: r['rotation_error_deg'], reverse=True)[:30],
              'seconds': time.monotonic()-started,
              'parent_peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
              'note': 'Diagnostic thresholds only. No match filtering or model promotion has been applied.'}
    atomic_json(work / 'review-summary.json', result)
    return result


def main():
    stages = {'sfm-match-review': {'sfm', 'features', 'matches', 'metadata', 'work', 'result'},
              'sfm-gps-map': {'review', 'work', 'result'}}
    if len(sys.argv) != 3 or sys.argv[1] not in stages:
        raise SystemExit('Expected a fixed SfM stage and JSON paths')
    args = json.loads(sys.argv[2])
    if set(args) != stages[sys.argv[1]]:
        raise SystemExit('Unexpected SfM stage arguments')
    for value in args.values():
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).resolve().is_relative_to(ROOT):
            raise SystemExit('All paths must remain in isolated RAW storage')
    if colmap.__version__ != '4.2.1':
        raise SystemExit('Expected pinned PyCOLMAP 4.2.1 runtime')
    work = Path(args['work'])
    work.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (work / '.stage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if sys.argv[1] == 'sfm-match-review':
                result = review(args)
            else:
                from gps_map import map_scene
                result = map_scene(args)
        except Exception as exc:
            atomic_json(Path(args['result']), {'error': str(exc)})
            raise
        atomic_json(Path(args['result']), result)
    print(json.dumps({'completed': sys.argv[1], 'result': args['result']}), flush=True)


if __name__ == '__main__':
    main()
