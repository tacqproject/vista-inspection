"""Run with the private PyCOLMAP runtime; uses synthetic geometry only."""
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pycolmap as colmap

import gps_sfm
import gps_map


class MatchReviewTests(unittest.TestCase):
    def test_native_alicevision_non_square_calibration(self):
        # Values serialized by the installed AliceVision after setting unequal
        # scaleX/scaleY. The mm focal is based on scaleY, not scaleX.
        intrinsic = {'type': 'pinhole', 'distortionType': 'radialk3',
                     'width': '5472', 'height': '3648', 'sensorWidth': '13.199999999999999',
                     'focalLength': '10.466357931550624', 'pixelRatio': '1.0009227686247655',
                     'principalPoint': ['-1.4753752636888402', '-59.51812913399548'],
                     'distortionParams': [-.1838815068670066, -.017507539183009464, .049485392056877735]}
        camera = colmap.Camera(camera_id=1, **gps_sfm.camera_parameters(intrinsic))
        np.testing.assert_allclose(camera.params[:4],
                                   [4334.781106170077, 4338.781106170077,
                                    2734.524624736311, 1764.4818708660045], atol=1e-10, rtol=0)

    def test_calibration_relative_pose_import_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            features, matches, work = [root / name for name in ('features', 'matches', 'work')]
            for p in (features, matches, work):
                p.mkdir()
            intrinsic = {'intrinsicId': 'i', 'type': 'pinhole', 'distortionType': 'radialk3',
                         'width': 5472, 'height': 3648, 'pxFocalLength': [4250, 4250],
                         'principalPoint': [-2, -57], 'distortionParams': [-.18, -.017, .049]}
            camera = colmap.Camera(camera_id=1, **gps_sfm.camera_parameters(intrinsic))
            rotation = gps_sfm.drone_rotation({'GimbalYawDegree': 0, 'GimbalPitchDegree': 0,
                                               'GimbalRollDegree': 0})
            rng = np.random.default_rng(4)
            points = rng.uniform([-2, 6, -2], [2, 12, 2], (200, 3))
            centres = np.array([[-1,0,0], [1,0,.2], [0,1,.5], [0,-1,-.4], [1,1,.8], [-1,-1,-.2]])
            views, metadata = [], []
            for i, centre in enumerate(centres, 1):
                vid, name = str(100+i), f'DJI_{i:04d}.DNG'
                cam_points = (points-centre) @ rotation
                pixels = camera.img_from_cam(cam_points)
                xy = cam_points[:, :2] / cam_points[:, 2:]
                r2 = (xy*xy).sum(axis=1)
                factor = 1 - .18*r2 - .017*r2*r2 + .049*r2*r2*r2
                expected = xy*factor[:, None]*4250 + [5472/2-2, 3648/2-57]
                np.testing.assert_allclose(pixels, expected, atol=1e-9)
                values = np.column_stack([pixels, np.full(200, 3), np.zeros(200)])
                np.savetxt(features / f'{vid}.dspsift.feat', values, fmt='%.9g')
                views.append({'viewId': vid, 'intrinsicId': 'i', 'path': str(root/name)})
                metadata.append({'FileName': name, 'GPSLatitude': 44+centre[1]/6378137*180/np.pi,
                                 'GPSLongitude': -123+centre[0]/(6378137*np.cos(np.deg2rad(44)))*180/np.pi,
                                 'GPSAltitude': 74+centre[2], 'GimbalYawDegree': 0,
                                 'GimbalPitchDegree': 0, 'GimbalRollDegree': 0})
            with (matches/'0.matches.txt').open('w') as f:
                for a, b in itertools.combinations(views, 2):
                    f.write(f"{a['viewId']} {b['viewId']}\n1\ndspsift 200\n")
                    f.write(''.join(f'{i} {i}\n' for i in range(200)))
            sfm = root/'cameras.sfm'
            sfm.write_text(json.dumps({'views': views, 'intrinsics': [intrinsic]}))
            meta = root/'metadata.json'
            meta.write_text(json.dumps(metadata))
            args = dict(sfm=str(sfm), features=str(features), matches=str(matches),
                        metadata=str(meta), work=str(work), result=str(work/'result.json'))
            with patch.object(gps_sfm, 'reserve'), patch.dict(gps_map.PROFILE, snapshot_frames=2):
                result = gps_sfm.review(args)
                self.assertEqual(result['views'], 6)
                self.assertEqual(result['pairs'], 15)
                self.assertEqual(result['scenarios']['verified']['components'], [6])
                rows = json.loads((work/'pair-review.json').read_text()).values()
                self.assertLess(max(r['rotation_error_deg'] for r in rows), .01)
                self.assertLess(max(r['direction_error_deg'] for r in rows), .01)
                with patch('gps_sfm.mp.get_context', side_effect=AssertionError('must reuse reviewed pairs')):
                    resumed = gps_sfm.review(args)
                self.assertEqual(resumed['scenarios'], result['scenarios'])
                with colmap.Database.open(work/'database.db') as db:
                    self.assertEqual(len(db.read_all_images()), 6)
                    self.assertEqual(len(db.read_all_pose_priors()), 6)
                    np.testing.assert_array_equal(db.read_matches(1, 2)[:, 0], np.arange(200))
                mapping_work = root/'mapping'
                mapping_args = {'review': str(work), 'work': str(mapping_work),
                                'result': str(mapping_work/'result.json')}
                mapped = gps_map.map_scene(mapping_args)
                self.assertEqual(mapped['registered'], 6)
                self.assertFalse(mapped['accepted'])
                self.assertTrue(gps_map.latest_snapshot(mapping_work))
                output = json.loads((mapping_work/'sfm.json').read_text())
                poses = {p['poseId']: p['pose']['transform'] for p in output['poses']}
                errors = []
                for landmark in output['structure']:
                    for obs in landmark['observations']:
                        pose = poses[obs['observationId']]
                        rot = np.array(pose['rotation']).reshape(3,3).T
                        cam_point = rot @ (np.asarray(landmark['X'])-pose['center'])
                        errors.append(np.linalg.norm(camera.img_from_cam(cam_point)-obs['x']))
                self.assertLess(max(errors), .01)
                before = (mapping_work/'sfm.json').stat().st_mtime_ns
                with patch('pycolmap.incremental_mapping', side_effect=AssertionError('must reuse SfM')):
                    self.assertEqual(gps_map.map_scene(mapping_args), mapped)
                self.assertEqual((mapping_work/'sfm.json').stat().st_mtime_ns, before)
                sfm.write_text(sfm.read_text() + ' ')
                with self.assertRaisesRegex(ValueError, 'Inputs changed'):
                    gps_sfm.review(args)


if __name__ == '__main__':
    unittest.main()
