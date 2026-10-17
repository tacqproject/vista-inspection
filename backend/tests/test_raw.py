"""Regression coverage for source preservation, geometry and job isolation.

Run: python -m unittest discover -s tests -v
Optional real-DNG roundtrip: VISTA_TEST_DNG=/.../photo.DNG
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image

from app.config import Settings, settings
from app.services import storage, raw_images

repo = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('job_broker', repo / 'scripts/raw/job_broker.py')
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class RawIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'runtime'
        self.source = self.base / 'source'
        self.source.mkdir()
        self.root.mkdir()
        self.patches = [patch.object(settings, 'upload_dir', str(self.root / 'uploads')),
                        patch.object(settings, 'work_dir', str(self.root / 'scratch')),
                        patch.object(settings, 'raw_source_root', str(self.source)),
                        patch.object(settings, 'raw_source_roots', []),
                        patch.object(settings, 'disk_reserve_gb', 0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_upload_and_zip_replace_link_without_writing_original(self):
        original = self.source / 'frame.DNG'
        original.write_bytes(b'original RAW data')
        dest = storage.get_photos_dir('inspection') / original.name
        dest.symlink_to(original)
        storage.save_uploaded_photos('inspection', [(original.name, b'new upload')])
        self.assertEqual(original.read_bytes(), b'original RAW data')
        self.assertEqual(dest.read_bytes(), b'new upload')
        self.assertFalse(dest.is_symlink())
        dest.unlink()
        dest.symlink_to(original)
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('../frame.DNG', b'zipped RAW data')
        storage.extract_zip('inspection', payload.getvalue())
        self.assertEqual(original.read_bytes(), b'original RAW data')
        self.assertEqual(dest.read_bytes(), b'zipped RAW data')

    def test_photo_identity_and_source_boundaries(self):
        original = self.source / 'frame.DNG'
        original.write_bytes(b'raw')
        link = storage.get_photos_dir('inspection') / original.name
        link.symlink_to(original)
        self.assertEqual(raw_images.source_photo('inspection', original.name), link)
        self.assertEqual(storage.get_relative_path(link), 'inspections/inspection/photos/frame.DNG')
        with self.assertRaises(ValueError):
            raw_images.source_photo('inspection', '../frame.DNG')
        outside = self.base / 'outside.DNG'
        outside.write_bytes(b'private')
        bad = link.with_name('bad.DNG')
        bad.symlink_to(outside)
        with self.assertRaises(ValueError):
            raw_images.source_photo('inspection', bad.name)

    def test_zip_preserves_disk_reserve_without_spooling_archive(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('frame.DNG', b'RAW data')
        from types import SimpleNamespace
        with patch.object(storage.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ValueError, 'Nedostatek'):
                storage.extract_zip('inspection', payload.getvalue())
        self.assertEqual(list(storage.get_photos_dir('inspection').iterdir()), [])
        self.assertFalse((storage.get_inspection_dir('inspection') / 'upload.zip').exists())

    def test_broker_rejects_escape_privilege_and_source_writes(self):
        pinned = {'vista-meshroom:latest': 'sha256:test'}
        def validate(args):
            return broker.validate(args, self.root, [self.source], '1000:1000', pinned)
        allowed = ['run', '--rm', '-v', f'{self.root}:{self.root}', '-v', f'{self.source}:/in:ro', 'vista-meshroom:latest', 'job']
        command, name = validate(allowed)
        self.assertIn('--read-only', command)
        self.assertIn('--memory', command)
        self.assertTrue(name.startswith('vista-raw-job-'))
        for bad in (
            ['run', '--privileged', 'vista-meshroom:latest'],
            ['run', '--network', 'host', 'vista-meshroom:latest'],
            ['run', '-v', '/home/epr/VISTA:/in', 'vista-meshroom:latest'],
            ['run', '-v', f'{self.source}:/in:rw', 'vista-meshroom:latest'],
            ['run', '-v', f'{self.root}:/etc', 'vista-meshroom:latest'],
            ['run', 'unapproved:image'], ['build', '.'], ['kill', 'production'],
        ):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                validate(bad)
        escape = self.root / 'escape'
        escape.symlink_to(self.base)
        with self.assertRaises(ValueError):
            validate(['run', '-v', f'{escape}:/in', 'vista-meshroom:latest'])

    def test_settings_reject_production_storage_and_symlinks(self):
        config = dict(_env_file=None, isolated_data_root=str(self.root),
                      database_url=f'sqlite+aiosqlite:///{self.root}/vista.db',
                      upload_dir=str(self.root / 'uploads'), work_dir=str(self.root / 'scratch'),
                      raw_source_root=str(self.source))
        Settings(**config)
        with self.assertRaises(ValueError):
            Settings(**{**config, 'upload_dir': '/home/epr/VISTA/backend/uploads'})
        with self.assertRaises(ValueError):
            Settings(**{**config, 'database_url': 'sqlite+aiosqlite:////home/epr/VISTA/backend/vista.db'})
        (self.root / 'escape').symlink_to(self.base)
        with self.assertRaises(ValueError):
            Settings(**{**config, 'work_dir': str(self.root / 'escape')})

    def test_python_worker_is_fixed_and_cannot_write_sources(self):
        repo = Path(broker.__file__).resolve().parents[2]
        payload = {'mesh': str(self.root / 'mesh.obj'), 'result': str(self.root / 'result.json')}
        def validate(stage, data):
            return broker.validate(['python-stage', stage, json.dumps(data)], self.root,
                                   [self.source, repo], '1000:1000', {'ubuntu:22.04': 'sha256:test'})
        command, name = validate('lods', payload)
        self.assertIn('--read-only', command)
        self.assertIn('24g', command)
        self.assertIn('app.services.photogrammetry.worker', command)
        with self.assertRaises(ValueError):
            validate('shell', payload)
        with self.assertRaises(ValueError):
            validate('lods', {**payload, 'result': str(self.source / 'write.json')})
        with self.assertRaises(ValueError):
            validate('lods', {**payload, 'code': 'arbitrary'})

    def test_additional_readonly_dataset_preserves_source_boundaries(self):
        second = self.base / 'second-dataset'
        second.mkdir()
        original = second / 'frame.DNG'
        original.write_bytes(b'second dataset RAW')
        link = storage.get_photos_dir('second-inspection') / original.name
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            raw_images.source_photo('second-inspection', original.name)
        with patch.object(settings, 'raw_source_roots', [str(second)]):
            self.assertEqual(raw_images.source_photo('second-inspection', original.name), link)
            outside = self.base / 'private.DNG'
            outside.write_bytes(b'not a dataset')
            bad = link.with_name('bad.DNG')
            bad.symlink_to(outside)
            with self.assertRaises(ValueError):
                raw_images.source_photo('second-inspection', bad.name)
            command, _ = broker.validate(
                ['run', '--rm', '-v', f'{second}:{second}:ro', 'vista-meshroom:latest', 'job'],
                self.root, [self.source, second], '1000:1000', {'vista-meshroom:latest': 'sha256:test'})
            self.assertIn(f'{second}:{second}:ro', command)
            with self.assertRaises(ValueError):
                broker.validate(
                    ['run', '--rm', '-v', f'{second}:{second}:rw', 'vista-meshroom:latest', 'job'],
                    self.root, [self.source, second], '1000:1000', {'vista-meshroom:latest': 'sha256:test'})
        with self.assertRaises(ValueError):
            Settings(_env_file=None, isolated_data_root=str(self.root),
                     database_url=f'sqlite+aiosqlite:///{self.root}/vista.db',
                     upload_dir=str(self.root / 'uploads'), work_dir=str(self.root / 'scratch'),
                     raw_source_roots=[str(self.root / 'disguised-source')])

    def test_png_resolution_budget_uses_real_dimensions(self):
        from app.services.photogrammetry.postprocess.openmvs import _dense_level
        work = self.root / 'scene'
        (work / 'images').mkdir(parents=True)
        Image.new('RGB', (800, 600)).save(work / 'images/00001.png')
        self.assertEqual(_dense_level(work, 20), 1)

    def test_textured_ply_fallback_preserves_geometry_uv_and_pixels(self):
        import pymeshlab
        from app.services.photogrammetry.openmvs_bridge import _textured_ply_to_obj
        pixels = np.array([[[255, 0, 0], [0, 255, 0]], [[0, 0, 255], [255, 255, 0]]], dtype=np.uint8)
        texture = self.root / 'atlas.png'
        Image.fromarray(pixels).save(texture)
        ply = self.root / 'textured.ply'
        ply.write_text('''ply
format ascii 1.0
comment TextureFile atlas.png
element vertex 3
property float x
property float y
property float z
element face 1
property list uchar int vertex_indices
property list uchar float texcoord
end_header
0 0 0
1 0 0
0 1 0
3 0 1 2 6 0 0 1 0 0 1
''')
        original = pymeshlab.MeshSet()
        original.load_new_mesh(str(ply))
        obj = self.root / 'textured.obj'
        _textured_ply_to_obj(ply, obj)
        converted = pymeshlab.MeshSet()
        converted.load_new_mesh(str(obj))
        a, b = original.current_mesh(), converted.current_mesh()
        np.testing.assert_allclose(a.vertex_matrix(), b.vertex_matrix(), atol=1e-7)
        np.testing.assert_array_equal(a.face_matrix(), b.face_matrix())
        np.testing.assert_allclose(a.wedge_tex_coord_matrix(), b.wedge_tex_coord_matrix(), atol=1e-7)
        self.assertEqual(b.texture_number(), 1)
        self.assertIn('mtllib textured.mtl', obj.read_text())
        self.assertIn('map_Kd atlas.png', obj.with_suffix('.mtl').read_text())
        with Image.open(texture) as im:
            np.testing.assert_array_equal(np.asarray(im.convert('RGB')), pixels)

    @unittest.skipUnless(os.environ.get('VISTA_TEST_DNG'), 'set VISTA_TEST_DNG for real RAW integration')
    def test_real_raw_display_and_colmap_geometry(self):
        from app.services.photogrammetry.openmvs_bridge import export_colmap
        source = Path(os.environ['VISTA_TEST_DNG'])
        rgb = raw_images.decode_rgb(source)
        h, w = rgb.shape[:2]
        preview = raw_images.display_file(source)
        with Image.open(preview) as im:
            self.assertEqual(im.size, (w, h))
        sfm = {'intrinsics': [{'intrinsicId': '1', 'width': str(w), 'height': str(h),
                              'pxFocalLength': [4000, 4000], 'principalPoint': [0, 0], 'distortionParams': [0, 0, 0]}],
               'poses': [{'poseId': '1', 'pose': {'transform': {'rotation': np.eye(3).ravel().tolist(), 'center': [0, 0, 0]}}}],
               'views': [{'viewId': '1', 'poseId': '1', 'intrinsicId': '1', 'path': str(source)}]}
        scene = self.root / 'scene.json'
        scene.write_text(json.dumps(sfm))
        with patch.object(settings, 'disk_reserve_gb', 0):
            result = export_colmap(scene, self.root / 'colmap')
        self.assertEqual(result['views'], 1)
        with Image.open(self.root / 'colmap/images/00001.png') as im:
            self.assertEqual(im.size, (w, h))
            # Zero distortion: lossless PNG must preserve the RAW-decoded RGB pixels.
            np.testing.assert_array_equal(np.asarray(im), rgb)
        sfm['intrinsics'][0]['width'] = w - 8
        scene.write_text(json.dumps(sfm))
        with self.assertRaises(ValueError):
            export_colmap(scene, self.root / 'bad-colmap')


if __name__ == '__main__':
    unittest.main()
