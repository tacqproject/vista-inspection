import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import settings
from app.services.photogrammetry.runtime import Checkpoints, sfm_header, current_sfm, recover_interrupted_statuses
from app.services.photogrammetry.openmvs_bridge import export_colmap
from app.services.photogrammetry.postprocess.openmvs import dense_pixels


class ResumeTests(unittest.TestCase):
    def test_reliable_region_points_require_distinct_views(self):
        from app.services.photogrammetry.regional import make_plan, write_region_scene
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'disk_reserve_gb', 0):
            root = Path(temp)
            header = {'views': [{'viewId': str(i), 'poseId': str(i)} for i in range(3)],
                      'poses': [{'poseId': str(i)} for i in range(3)], 'intrinsics': []}
            good = [{'X': [i / 120, 0, 0], 'observations': [
                {'observationId': str(v)} for v in range(3)]} for i in range(120)]
            repeated = [{'X': [i / 120, 0, 0], 'observations': [
                {'observationId': '0'}, {'observationId': '0'}, {'observationId': '1'}]}
                for i in range(120)]
            sfm = root / 'sfm.json'
            sfm.write_text(json.dumps({**header, 'structure': good + repeated}))
            plan = make_plan(sfm, root / 'regions')
            self.assertEqual(plan['reliable_points'], 120)
            self.assertEqual(plan['source_reliable_points'], 120)
            landmarks = root / 'regions/C000/landmarks.jsonl'
            # Context preserves all source observations, while a region with
            # only one selected view cannot triangulate duplicate features.
            self.assertEqual(len(landmarks.read_text().splitlines()), 240)
            self.assertEqual(write_region_scene(root/'single.json', header, landmarks, {'0'}), 0)
            self.assertEqual(write_region_scene(root/'pair.json', header, landmarks, {'0', '1'}), 240)

    def test_reviewed_planning_focus_excludes_far_seeds_and_preserves_source(self):
        from app.services.photogrammetry.regional import make_plan
        from app.services.photogrammetry.runtime import identity
        import numpy as np
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'disk_reserve_gb', 0):
            root = Path(temp)
            sfm = root/'sfm.json'
            near = np.column_stack([np.linspace(-2,2,240), np.linspace(-5,5,240), np.zeros(240)])
            far = np.array([[10000+i,0,0] for i in range(120)])
            data = {'views': [{'viewId': str(i), 'poseId': str(i)} for i in range(3)],
                    'poses': [{'poseId': str(i)} for i in range(3)], 'intrinsics': [],
                    'structure': [{'landmarkId': str(j), 'X': p.tolist(),
                                   'observations': [{'observationId': str(i)} for i in range(3)]}
                                  for j, p in enumerate(np.r_[near,far])]}
            original = json.dumps(data)
            sfm.write_text(original)
            focus = {'version': 1, 'accepted': True, 'sfm': identity(sfm),
                     'origin': [0,0,0], 'axes': [[1,0,0],[0,0,1]],
                     'lower': [-3,-3], 'upper': [3,3]}
            target = root/'regional-focus.json'
            target.write_text(json.dumps(focus))
            work = root/'plan'
            plan = make_plan(sfm, work)
            self.assertEqual(plan['source_reliable_points'], 360)
            self.assertEqual(plan['reliable_points'], 240)
            self.assertEqual(plan['outside_planning_focus'], 120)
            self.assertLess(max(r['radius'] for r in plan['regions']), 7)
            # Two horizontal axes leave all heights available, including footings.
            kept = [json.loads(s) for s in (work/'C000/landmarks.jsonl').read_text().splitlines()]
            self.assertEqual({lm['landmarkId'] for lm in kept}, {str(i) for i in range(240)})
            self.assertEqual(sfm.read_text(), original)
            before = (work/'plan.json').stat().st_mtime_ns
            self.assertEqual(make_plan(sfm,work), plan)
            self.assertEqual((work/'plan.json').stat().st_mtime_ns, before)
            # A reviewed focus change invalidates the old partition checkpoint.
            target.write_text(json.dumps({**focus, 'lower': [0,-3]}))
            self.assertEqual(make_plan(sfm,work)['reliable_points'], 120)
            for change in ({'accepted': False}, {'sfm': {**identity(sfm), 'size': 1}},
                           {'axes': [[1,0,0],[1,0,0]]}, {'upper': [-4,3]}):
                with self.subTest(change=change):
                    target.write_text(json.dumps({**focus, **change}))
                    with self.assertRaisesRegex(RuntimeError, 'Neplatná oblast'):
                        make_plan(sfm,work)

    def test_reviewed_sfm_frame_is_used_consistently_and_stale_selection_fails(self):
        import asyncio
        from app.services.photogrammetry.runtime import identity
        from app.services.photogrammetry.postprocess.openmvs import _export_sfm_json
        for change in ('none', 'native', 'camera', 'rejected', 'outside', 'symlink', 'untracked-focus', 'focus-changed'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                cache = base / 'cache'
                native = cache / 'StructureFromMotion/current'
                native.mkdir(parents=True)
                (cache/'project.mg').write_text(json.dumps({'graph': {
                    'sfm': {'nodeType': 'StructureFromMotion', 'uid': 'current'}}}))
                (native/'status').write_text('{"status":"SUCCESS"}')
                for name in ('sfm.abc', 'cameras.sfm'):
                    (native/name).write_text('native frame')
                folder = cache / 'approved-sfm/candidate'
                folder.mkdir(parents=True)
                for name in ('sfm.json', 'cameras.sfm'):
                    (folder/name).write_text('accepted frame')
                (folder/'quality-decision.json').write_text(json.dumps({'accepted': change != 'rejected'}))
                record = {'version': 1, 'accepted': True,
                          'native': [identity(native/n) for n in ('sfm.abc', 'cameras.sfm')],
                          'outputs': {n: identity(folder/n) for n in
                                      ('sfm.json', 'cameras.sfm', 'quality-decision.json')}}
                if change in ('untracked-focus', 'focus-changed'):
                    focus_path = folder/'regional-focus.json'
                    focus_path.write_text('reviewed focus')
                    if change == 'focus-changed':
                        record['outputs']['regional-focus.json'] = identity(focus_path)
                        focus_path.write_text('changed focus')
                if change in ('outside', 'symlink'):
                    outside = base/'outside'
                    folder.parent.rename(outside)
                    if change == 'symlink':
                        (cache/'approved-sfm').symlink_to(outside, target_is_directory=True)
                    record['outputs'] = {n: identity(outside/'candidate'/n) for n in record['outputs']}
                (cache/'approved-sfm.json').write_text(json.dumps(record))
                if change == 'native':
                    (native/'sfm.abc').write_text('new native frame')
                elif change == 'camera':
                    (folder/'cameras.sfm').write_text('inconsistent camera frame')
                if change == 'none':
                    self.assertEqual(current_sfm(cache), folder)
                    with patch('app.services.photogrammetry.postprocess.openmvs.run_python_stage',
                               side_effect=AssertionError('accepted JSON must not be replaced')):
                        self.assertEqual(asyncio.run(_export_sfm_json(cache, base/'log')), folder/'sfm.json')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Neplatný schválený SfM kandidát'):
                        current_sfm(cache)

    def test_streamed_region_selection_and_tracks_match_reference(self):
        import numpy as np
        from app.services.hires import select_views
        from app.services.photogrammetry.regional import select_region_views, write_region_scene
        header = {
            'intrinsics': [],
            'views': [{'viewId': str(i), 'poseId': str(i)} for i in range(5)],
            'poses': [{'poseId': str(i), 'pose': {'transform': {
                'rotation': [1,0,0,0,1,0,0,0,1], 'center': [0,0,10 if i == 3 else 0]}}}
                for i in range(4)],
        }
        landmarks = [
            {'landmarkId': str(i), 'X': [i / 20, 0, 5], 'color': [10,20,30],
             'observations': [{'observationId': str(j), 'featureId': str(i+j), 'x': [i,j], 'scale': 2}
                              for j in (0,1,2,3,4) if j != 2 or i % 2 == 0]}
            for i in range(20)]
        landmarks += [{'landmarkId': 'outside', 'X': [100,0,5],
                       'observations': [{'observationId': '2'}]}]
        centre = np.array([0,0,5], float)
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'disk_reserve_gb', 0):
            root = Path(temp)
            source = root / 'landmarks.jsonl'
            source.write_text(''.join(json.dumps(lm) + '\n' for lm in landmarks))
            for max_views in (1,2,4):
                expected = select_views({**header, 'structure': landmarks}, centre, 2,
                                        max_views=max_views, min_obs=3)
                actual = select_region_views(header, source, centre, 2, max_views, min_obs=3)
                self.assertEqual(actual, expected)
            selected, _ = select_region_views(header, source, centre, 2, 2, min_obs=3)
            target = root / 'sub.json'
            count = write_region_scene(target, header, source, selected)
            expected_tracks = []
            for lm in landmarks:
                obs = [o for o in lm['observations'] if o['observationId'] in selected]
                if len(obs) >= 2:
                    expected_tracks.append({**lm, 'observations': obs})
            self.assertEqual(json.loads(target.read_text()), {**header, 'structure': expected_tracks})
            self.assertEqual(count, len(expected_tracks))

    def test_streamed_region_failure_preserves_complete_scene(self):
        from app.services.photogrammetry.regional import write_region_scene
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'disk_reserve_gb', 0):
            root = Path(temp)
            source = root / 'landmarks.jsonl'
            source.write_text('{invalid JSON\n')
            target = root / 'sub.json'
            target.write_text('previous complete scene')
            with self.assertRaises(ValueError):
                write_region_scene(target, {'views': []}, source, {'1','2'})
            self.assertEqual(target.read_text(), 'previous complete scene')
            self.assertFalse(target.with_name('sub.json.tmp').exists())

    def test_refinement_keeps_reference_views_and_only_reduces_after_oom(self):
        from app.services.photogrammetry.regional import refine_region
        from app.services.photogrammetry.openmvs_bridge import OpenMVSError
        for oom in (False, True):
            with self.subTest(oom=oom), tempfile.TemporaryDirectory() as temp:
                work = Path(temp)
                (work/'mesh.ply').write_text('input mesh')
                (work/'scene_dense.mvs').write_text('camera scene')
                output = work/'mesh_refined.ply'
                output.write_text('invalid partial from an interrupted attempt')
                calls = []
                def native(folder, *args, **kwargs):
                    neighbors = int(args[args.index('--max-views')+1])
                    calls.append(neighbors)
                    self.assertEqual(args[args.index('--resolution-level')+1], '0')
                    self.assertFalse(output.exists())
                    if oom and neighbors == 8:
                        output.write_text('partial output before OOM')
                        raise OpenMVSError('RefineMesh', 137)
                    output.write_text('complete refined mesh')
                cp = Checkpoints(work/'stages')
                with patch('app.services.photogrammetry.openmvs_bridge._mvs', side_effect=native):
                    used = refine_region(work, cp, io.StringIO())
                self.assertEqual(calls, [8,4] if oom else [8])
                self.assertEqual(used, 4 if oom else 8)
                with patch('app.services.photogrammetry.openmvs_bridge._mvs', side_effect=AssertionError('must resume')):
                    self.assertEqual(refine_region(work, Checkpoints(work/'stages'), io.StringIO()), used)

    def test_refinement_does_not_retry_geometry_error_with_cuda_banner(self):
        from app.services.photogrammetry.regional import refine_region
        from app.services.photogrammetry.openmvs_bridge import OpenMVSError
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work/'mesh.ply').write_text('mesh')
            (work/'scene_dense.mvs').write_text('scene')
            error = OpenMVSError('RefineMesh', 1, 'CUDA device initialized; invalid mesh')
            with patch('app.services.photogrammetry.openmvs_bridge._mvs', side_effect=error) as native:
                with self.assertRaises(OpenMVSError):
                    refine_region(work, Checkpoints(work/'stages'), io.StringIO())
            self.assertEqual(native.call_count, 1)

    def test_regions_cover_uneven_spatial_cloud_in_shared_frame(self):
        import numpy as np
        from scipy.spatial import cKDTree
        from app.services.photogrammetry.regional import partition
        rng = np.random.default_rng(8)
        points = np.r_[rng.normal(size=(1000,3)), rng.normal(size=(100,3))*10+[30,0,0]]
        regions = partition(points, max_points=150, max_regions=32)
        centres = np.array([r['centre'] for r in regions])
        distances, owners = cKDTree(centres).query(points)
        radii = np.array([r['radius'] for r in regions])
        self.assertTrue(np.all(distances < radii[owners]))
        self.assertEqual(sum(r['seed_points'] for r in regions), len(points))
        self.assertEqual(regions, partition(points, max_points=150, max_regions=32))

    def test_checkpoint_rejects_changed_input_or_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'mesh.ply'
            output.write_text('completed mesh')
            cp = Checkpoints(root)
            cp.save('mesh', {'level': 3}, [output])
            self.assertTrue(Checkpoints(root).valid('mesh', {'level': 3}))
            self.assertFalse(cp.valid('mesh', {'level': 2}))
            output.write_text('partial')
            self.assertFalse(cp.valid('mesh', {'level': 3}))
            output.unlink()
            self.assertFalse(cp.valid('mesh', {'level': 3}))

    def test_recovery_preserves_success_and_selects_graph_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'project.mg').write_text(json.dumps({'graph': {'sfm': {'nodeType': 'StructureFromMotion', 'uid': 'current'}}}))
            for uid, status in [('current', 'SUCCESS'), ('foreign', 'RUNNING')]:
                folder = root / 'StructureFromMotion' / uid
                folder.mkdir(parents=True)
                for name in ('sfm.abc', 'cameras.sfm'):
                    (folder / name).write_text(uid)
                (folder / 'status').write_text(json.dumps({'status': status}))
            saved = (root / 'StructureFromMotion/current/status').read_bytes()
            self.assertEqual(recover_interrupted_statuses(root), 1)
            self.assertEqual(current_sfm(root).name, 'current')
            self.assertEqual((current_sfm(root) / 'status').read_bytes(), saved)
            self.assertEqual(recover_interrupted_statuses(root), 0)

    def test_corvallis_dense_budget_and_min_resolution(self):
        dims = [(5472, 3648)] * 798
        self.assertGreater(dense_pixels(dims, 2), 300e6)
        self.assertLess(dense_pixels(dims, 3), 300e6)
        self.assertEqual(dense_pixels([(1000, 500)], 6), 640 * 320)

    def test_streaming_export_tracks_and_resume(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'source.jpg'
            Image.new('RGB', (32, 24), 'gray').save(src)
            data = {
                'intrinsics': [{'intrinsicId': 'i', 'width': 32, 'height': 24, 'pxFocalLength': 30}],
                'poses': [{'poseId': str(i), 'pose': {'transform': {
                    'rotation': [1,0,0,0,1,0,0,0,1], 'center': [i,0,0]}}} for i in range(2)],
                'views': [{'viewId': str(i), 'poseId': str(i), 'intrinsicId': 'i', 'path': str(src)} for i in range(2)],
                'structure': [
                    {'X': [1,2,3], 'observations': [{'observationId': '0', 'x': [4,5]}]},
                    {'X': [1,2,3], 'observations': [{'observationId': '0', 'x': [4+i,5]} for i in range(2)]},
                    {'X': [2,3,4], 'observations': [{'observationId': str(i), 'x': [6+i,8]} for i in range(2)]},
                ],
            }
            sfm = root / 'sfm.json'; sfm.write_text(json.dumps(data))
            self.assertEqual(sfm_header(sfm), {k: data[k] for k in ('views','intrinsics','poses')})
            with patch.object(settings, 'disk_reserve_gb', 0):
                summary = export_colmap(sfm, root / 'scene')
                self.assertEqual(summary['points'], 1)
                sparse = root / 'scene/sparse'
                point = (sparse / 'points3D.txt').read_text().split()
                self.assertEqual(point[-4:], ['1', '0', '2', '0'])
                lines = (sparse / 'images.txt').read_text().splitlines()
                self.assertEqual(lines[1].split(), ['6.0','8.0','1'])
                self.assertEqual(lines[3].split(), ['7.0','8.0','1'])
                image = root / 'scene/images/00001.jpg'
                before = image.stat().st_mtime_ns
                with patch('app.services.raw_images.read_bgr', side_effect=AssertionError('must reuse images')):
                    self.assertEqual(export_colmap(sfm, root / 'scene'), summary)
                    # Missing sparse output rebuilds tracks but reuses completed images.
                    (sparse / 'images.txt').unlink()
                    self.assertEqual(export_colmap(sfm, root / 'scene'), summary)
                self.assertEqual(image.stat().st_mtime_ns, before)

    def test_distorted_corner_observations_match_calibrated_image(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'disk_reserve_gb', 0):
            root = Path(temp)
            src = root / 'source.jpg'
            Image.new('RGB', (192, 128), 'gray').save(src)
            scale = 192 / 5472
            f = 4331.781106170078 * scale
            cx, cy = 2733.524624736311 * scale, 1766.4818708660046 * scale
            distortion = [-.1838815068670066, -.017507539183009464, .049485392056877735]
            ideal = np.array([[2., 2.], [190., 2.], [2., 126.], [190., 126.]])
            xy = (ideal - [cx, cy]) / f
            r2 = np.sum(xy**2, axis=1)
            radial = 1 + distortion[0]*r2 + distortion[1]*r2**2 + distortion[2]*r2**3
            observed = xy * radial[:, None] * f + [cx, cy]
            header = {
                'intrinsics': [{'intrinsicId': 'i', 'width': 192, 'height': 128,
                    'pxFocalLength': f, 'principalPoint': [cx-96, cy-64], 'distortionParams': distortion}],
                'poses': [{'poseId': str(i), 'pose': {'transform': {
                    'rotation': [1,0,0,0,1,0,0,0,1], 'center': [0,0,0]}}} for i in range(2)],
                'views': [{'viewId': str(i), 'poseId': str(i), 'intrinsicId': 'i', 'path': str(src)} for i in range(2)],
                'structure': [{'X': [*ray, 1.], 'observations': [
                    {'observationId': str(i), 'x': uv.tolist()} for i in range(2)]}
                    for ray, uv in zip(xy, observed)],
            }
            sfm = root/'sfm.json'; sfm.write_text(json.dumps(header))
            result = export_colmap(sfm, root/'scene')
            self.assertEqual(result['points'], 4)
            lines = (root/'scene/sparse/images.txt').read_text().splitlines()
            for row in (lines[1], lines[3]):
                exported = np.array(row.split(), float).reshape(-1, 3)[:, :2]
                np.testing.assert_allclose(exported, ideal, atol=1e-8, rtol=0)

    def test_native_non_square_calibration_in_export_viewer_and_metrics(self):
        import numpy as np
        from PIL import Image
        from app.services.sfm import load_cameras, _project_into_camera
        from app.services.mesh_quality import reprojection_rms
        from app.services.raw_images import read_bgr
        from app.services.photogrammetry.runtime import identity
        with tempfile.TemporaryDirectory() as temp, patch.object(settings, 'disk_reserve_gb', 0):
            root = Path(temp)
            src = root/'source.jpg'
            Image.new('RGB', (192, 128), 'gray').save(src)
            scale = 192 / 5472
            fx, fy = np.array([4334.781106170077, 4338.781106170077]) * scale
            cx, cy = np.array([2734.524624736311, 1764.4818708660045]) * scale
            distortion = [-.1838815068670066, -.017507539183009464, .049485392056877735]
            ideal = np.array([[2., 2.], [190., 2.], [2., 126.], [190., 126.]])
            xy = (ideal - [cx, cy]) / [fx, fy]
            r2 = np.sum(xy**2, axis=1)
            radial = 1 + distortion[0]*r2 + distortion[1]*r2**2 + distortion[2]*r2**3
            observed = xy * radial[:, None] * [fx, fy] + [cx, cy]
            data = {
                'intrinsics': [{'intrinsicId': 'i', 'width': 192, 'height': 128,
                    'sensorWidth': '13.199999999999999', 'focalLength': '10.466357931550624',
                    'pixelRatio': '1.0009227686247655', 'principalPoint': [cx-96, cy-64],
                    'distortionParams': distortion}],
                'poses': [{'poseId': str(i), 'pose': {'transform': {
                    'rotation': [1,0,0,0,1,0,0,0,1], 'center': [0,0,0]}}} for i in range(2)],
                'views': [{'viewId': str(i), 'poseId': str(i), 'intrinsicId': 'i', 'path': str(src)} for i in range(2)],
                'structure': [{'X': [*ray, 1.], 'observations': [
                    {'observationId': str(i), 'x': uv.tolist()} for i in range(2)]}
                    for ray, uv in zip(xy, observed)],
            }
            sfm = root/'sfm.json'; sfm.write_text(json.dumps(data))
            for cam in load_cameras(sfm):
                np.testing.assert_allclose([cam.fx, cam.fy], [fx, fy], atol=1e-12, rtol=0)
                for ray, uv in zip(xy, observed):
                    np.testing.assert_allclose(np.array(_project_into_camera(cam, *ray, 1.))*[192,128],
                                               uv, atol=1e-10, rtol=0)
            self.assertLess(reprojection_rms(sfm)['reproj_rms_px'], 1e-10)
            # An old checkpoint lacks the interpreted K/distortion. It must not
            # silently reuse images undistorted with the former swapped scales.
            scene = root/'scene'; (scene/'images').mkdir(parents=True)
            cp = Checkpoints(scene/'image-checkpoints')
            for i in range(1, 3):
                target = scene/'images'/f'{i:05d}.jpg'
                Image.new('RGB', (192,128), 'black').save(target)
                cp.save(target.name, [identity(sfm), identity(src), 0, 95, None], [target])
            with patch('app.services.raw_images.read_bgr', wraps=read_bgr) as decoder:
                export_colmap(sfm, scene)
                self.assertEqual(decoder.call_count, 2)
            lines = (scene/'sparse/images.txt').read_text().splitlines()
            for row in (lines[1], lines[3]):
                exported = np.array(row.split(), float).reshape(-1, 3)[:, :2]
                np.testing.assert_allclose(exported, ideal, atol=1e-8, rtol=0)


if __name__ == '__main__':
    unittest.main()
