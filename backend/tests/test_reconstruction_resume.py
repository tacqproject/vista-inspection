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


if __name__ == '__main__':
    unittest.main()
