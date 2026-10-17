"""Fresh SfM with approximate horizontal position priors and reviewed matches.

Outputs are candidates only. The original SfM and VISTA model are untouched.
"""
from pathlib import Path
import json
import os
import sqlite3
import time

import numpy as np
import pycolmap as colmap

import gps_sfm as core

PROFILE = {'version': 1, 'max_relative_rotation_error_deg': 30,
           'min_horizontal_baseline_m': 15, 'max_horizontal_direction_error_deg': 60,
           'position_covariance_m2': [9, 9, 400], 'fixed_intrinsics': True,
           'global_ba_iterations': 50, 'local_ba_iterations': 25, 'threads': 8,
           'extract_colors': False, 'snapshot_frames': 50}
# ENU -> AliceVision-style X east, Y down, Z north. The existing OpenMVS
# adapter then produces an app frame with Y up. This is a rigid rotation.
ENU_TO_SFM = np.array([[1., 0, 0], [0, 0, -1.], [0, 1., 0]])


def horizontal_error(row, geometry, views):
    a, b = views[row['a']], views[row['b']]
    delta = np.asarray(a['enu']) - np.asarray(b['enu'])
    distance = float(np.linalg.norm(delta[:2]))
    angle = None
    if geometry.cam2_from_cam1 is not None and distance > .1:
        direction = np.asarray(b['camera_to_enu']) @ geometry.cam2_from_cam1.translation
        length = float(np.linalg.norm(direction[:2]))
        if length > 1e-9:
            cosine = direction[:2] @ delta[:2] / (length*distance)
            angle = float(np.degrees(np.arccos(np.clip(cosine, -1, 1))))
    return distance, angle


def prepare_database(review, work):
    manifest = json.loads((review/'import.json').read_text())
    original_inputs = json.loads((review/'input-identity.json').read_text())['inputs']
    for item in [original_inputs['sfm'], original_inputs['metadata'],
                 *original_inputs['features'], *original_inputs['matches']]:
        if core.identity(Path(item['path'])) != item:
            raise ValueError('Original SfM inputs changed; repeat match review in a new directory')
    source = review/'database.db'
    inputs = {'database': core.identity(source), 'review': core.identity(review/'pair-review.json'),
              'manifest': core.identity(review/'import.json'), 'profile': PROFILE}
    checkpoint = work/'database-prepared.json'
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if saved['inputs'] != inputs or not (work/'database.db').is_file():
            raise ValueError('GPS SfM inputs changed; use a new work directory')
        return manifest, saved
    pending = work/'database-pending.db'
    for suffix in ('', '-wal', '-shm'):
        pending.with_name(pending.name+suffix).unlink(missing_ok=True)
    with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(pending) as dst:
        src.backup(dst)
    rows = list(json.loads((review/'pair-review.json').read_text()).values())
    views = {r['id']: r for r in manifest['views']}
    accepted = []
    with colmap.Database.open(pending) as db, colmap.DatabaseTransaction(db):
        for prior in db.read_all_pose_priors():
            prior.position_covariance = np.diag(PROFILE['position_covariance_m2'])
            db.update_pose_prior(prior)
        for row in rows:
            keep = row['inliers'] >= 30 and row['rotation_error_deg'] is not None
            reason = None if keep else 'insufficient_two_view_geometry'
            if keep and row['rotation_error_deg'] > PROFILE['max_relative_rotation_error_deg']:
                keep, reason = False, 'relative_rotation_disagrees_with_gimbal'
            if keep:
                geometry = db.read_two_view_geometry(row['a'], row['b'])
                baseline, angle = horizontal_error(row, geometry, views)
                row.update(gps_horizontal_distance_m=baseline, horizontal_direction_error_deg=angle)
                if baseline >= PROFILE['min_horizontal_baseline_m'] and (
                        angle is None or angle > PROFILE['max_horizontal_direction_error_deg']):
                    keep, reason = False, 'horizontal_direction_disagrees_with_gps'
            row.update(selected=keep, rejected_reason=reason)
            if keep:
                accepted.append(row)
            elif db.exists_two_view_geometry(row['a'], row['b']):
                db.delete_two_view_geometry(row['a'], row['b'])
    graph = core.component_sizes(accepted, len(views), lambda r: True)
    if graph['components'] != [len(views)]:
        raise RuntimeError(f'Reviewed image graph has disconnected components: {graph["components"]}')
    pending.replace(work/'database.db')
    core.atomic_json(work/'selected-pairs.json', rows)
    result = {'inputs': inputs, 'graph': graph, 'gps_position_covariance_m2': PROFILE['position_covariance_m2'],
              'note': 'Vertical GPS is weakly weighted because different flights show altitude bias.'}
    core.atomic_json(checkpoint, result)
    print(f'Selected {graph["pairs"]} pairs / {graph["inliers"]} inliers; connected images: {len(views)}', flush=True)
    return manifest, result


def options(manifest, work):
    opt = colmap.IncrementalPipelineOptions()
    opt.use_prior_position = True
    opt.use_robust_loss_on_prior_position = True
    opt.extract_colors = False
    opt.num_threads = PROFILE['threads']
    opt.random_seed = 0
    opt.ba_refine_focal_length = False
    opt.ba_refine_principal_point = False
    opt.ba_refine_extra_params = False
    opt.ba_refine_sensor_from_rig = False
    opt.mapper.abs_pose_refine_focal_length = False
    opt.mapper.abs_pose_refine_extra_params = False
    opt.ba_global_max_num_iterations = PROFILE['global_ba_iterations']
    opt.ba_local_max_num_iterations = PROFILE['local_ba_iterations']
    opt.min_model_size = min(10, len(manifest['views']))
    opt.max_num_models = 5
    opt.snapshot_path = work/'snapshots'
    # COLMAP creates each numbered snapshot, but requires its parent to exist.
    (work/'snapshots').mkdir(parents=True, exist_ok=True)
    opt.snapshot_frames_freq = PROFILE['snapshot_frames']
    return opt


def latest_snapshot(work):
    candidates = sorted((work/'snapshots').glob('**/cameras.bin'),
                        key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for camera_file in candidates:
        folder = camera_file.parent
        if not all((folder/n).is_file() for n in ('images.bin', 'points3D.bin')):
            continue
        try:
            model = colmap.Reconstruction(folder)
            count = model.num_reg_images()
            del model
            if count >= 3:
                return str(folder)
        except Exception:
            continue
    return ''


def export_alicevision(model, manifest, review, work):
    inputs = json.loads((review/'input-identity.json').read_text())['inputs']
    source = Path(inputs['sfm']['path'])
    if core.identity(source) != inputs['sfm']:
        raise ValueError('Source camera metadata changed during reconstruction')
    header = json.loads(source.read_text())
    header.pop('structure', None)
    # Intrinsics were fixed throughout mapping; preserve the exact AV model.
    for camera in model.cameras.values():
        spec = next(s for s in manifest['cameras'] if s['camera_id'] == camera.camera_id)
        np.testing.assert_allclose(camera.params, spec['params'], atol=1e-10, rtol=0)
    by_id = {v['id']: v for v in manifest['views']}
    for view in header['views']:
        view['poseId'] = view['viewId']
    registered = set(model.reg_image_ids())
    header['poses'] = []
    for image_id in sorted(registered):
        image = model.image(image_id)
        world_to_camera = image.cam_from_world().rotation.matrix() @ ENU_TO_SFM.T
        centre = ENU_TO_SFM @ image.projection_center()
        header['poses'].append({'poseId': by_id[image_id]['view_id'], 'pose': {
            'transform': {'rotation': world_to_camera.T.reshape(-1).tolist(), 'center': centre.tolist()},
            'locked': 'false', 'rotationOnly': 'false', 'removable': 'true'}})
    core.atomic_json(work/'cameras.sfm', header)
    from functools import lru_cache
    @lru_cache(maxsize=32)
    def features(image_id):
        return np.load(review/'features'/f'{image_id}.npy', mmap_mode='r')
    pending, target = work/'sfm-pending.json', work/'sfm.json'
    points = observations = 0
    try:
        with pending.open('w', buffering=1048576) as f:
            f.write(json.dumps(header, ensure_ascii=False, separators=(',', ':'))[:-1])
            f.write(',"structure":[')
            for point_id, point in model.points3D.items():
                track = []
                for element in point.track.elements:
                    if element.image_id not in registered:
                        continue
                    feature = features(element.image_id)[element.point2D_idx]
                    track.append({'observationId': by_id[element.image_id]['view_id'],
                                  'featureId': str(element.point2D_idx), 'x': feature[:2].astype(float).tolist(),
                                  'scale': float(feature[2])})
                if len(track) < 2:
                    continue
                landmark = {'landmarkId': str(point_id), 'descType': 'dspsift', 'color': [128,128,128],
                            'X': (ENU_TO_SFM @ point.xyz).tolist(), 'observations': track}
                if points:
                    f.write(',')
                f.write(json.dumps(landmark, separators=(',', ':')))
                points += 1
                observations += len(track)
                if points % 100000 == 0:
                    core.reserve(work)
                    print(f'Exported {points} points / {observations} observations', flush=True)
            f.write(']}\n')
            f.flush()
            os.fsync(f.fileno())
        if points != model.num_points3D():
            raise RuntimeError('Incomplete mapped point export')
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)
    return {'registered': len(registered), 'landmarks': points, 'observations': observations,
            'sfm_json_bytes': target.stat().st_size,
            'unregistered': [v['name'] for v in manifest['views'] if v['id'] not in registered],
            'sfm_frame_rotation_from_enu': ENU_TO_SFM.tolist(), 'accepted': False}


def map_scene(args):
    review, work = Path(args['review']), Path(args['work'])
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    manifest, database = prepare_database(review, work)
    checkpoint = work/'mapping-complete.json'
    exported = work/'export-complete.json'
    if exported.exists():
        saved = json.loads(exported.read_text())
        try:
            reusable = all(core.identity(Path(item['path'])) == item
                           for item in saved['inputs'] + saved['outputs'])
        except OSError:
            reusable = False
        if reusable:
            print('Reusing completed GPS SfM and its exact exported frame', flush=True)
            return saved['result']
    if checkpoint.exists():
        report = json.loads(checkpoint.read_text())
        model = colmap.Reconstruction(report['selected_model'])
    else:
        opt = options(manifest, work)
        core.atomic_json(work/'mapping-options.json', {'profile': PROFILE, 'options': opt.summary()})
        image_dir = work/'images'
        image_dir.mkdir(exist_ok=True)  # extract_colors=False: no image pixels are loaded.
        attempt = work/f'attempt-{time.time_ns()}'
        attempt.mkdir()
        resume = latest_snapshot(work)
        print(f'Starting pose-prior SfM; resume={resume or "none"}', flush=True)
        models = colmap.incremental_mapping(work/'database.db', image_dir, attempt/'models',
                                           options=opt, input_path=resume)
        if not models:
            raise RuntimeError('No valid model from the reviewed matches and position priors')
        selected = max(models, key=lambda k: models[k].num_reg_images())
        model = models[selected]
        report = {'selected_model': str(attempt/'models'/str(selected)), 'resume_input': resume,
                  'models': {str(k): {'registered': v.num_reg_images(), 'points': v.num_points3D()}
                             for k, v in models.items()}}
        # Release additional independent models before streaming the selected one.
        models.clear()
        core.atomic_json(checkpoint, report)
    result = export_alicevision(model, manifest, review, work)
    result.update(mapping=report, graph=database['graph'], seconds=time.monotonic()-started,
                  note='Candidate only. GPS was used as a soft constraint, not as independent validation.')
    core.atomic_json(work/'map-summary.json', result)
    core.atomic_json(exported, {
        'inputs': [core.identity(p) for p in sorted(Path(report['selected_model']).glob('*.bin'))],
        'outputs': [core.identity(work/n) for n in ('cameras.sfm', 'sfm.json')], 'result': result})
    return result
