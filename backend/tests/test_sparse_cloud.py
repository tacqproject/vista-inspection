import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from ijson.common import IncompleteJSONError

from app.services.sparse_cloud import build_pointcloud


class SparseCloudTests(unittest.TestCase):
    def test_viewer_binary_coordinates_colors_and_cache_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root/'sfm.json', root/'pointcloud.bin'
            source.write_text(json.dumps({'structure': [
                {'X': ['1', '2', '3'], 'color': [10, 20, 30],
                 'observations': [{'unused': 'track'}] * 500},
                {'X': [-4, 5, -6]},
            ]}))
            build_pointcloud(source, output)
            data = output.read_bytes()
            self.assertEqual(struct.unpack('<I', data[:4]), (2,))
            self.assertEqual(struct.unpack('<6f', data[4:28]), (1, -2, -3, -4, -5, 6))
            self.assertEqual(data[28:], bytes([10, 20, 30, 200, 200, 200]))
            before = output.stat().st_mtime_ns
            build_pointcloud(source, output)
            self.assertEqual(output.stat().st_mtime_ns, before)
            output.write_bytes(data[:-1])  # a truncated cache must be rebuilt
            build_pointcloud(source, output)
            self.assertEqual(output.read_bytes(), data)
            # A new SfM export can replace the file while retaining its time.
            replacement = root/'replacement.json'
            replacement.write_bytes(source.read_bytes().replace(b'10, 20, 30', b'90, 80, 70'))
            stamp = source.stat().st_mtime_ns
            os.utime(replacement, ns=(stamp, stamp))
            replacement.replace(source)
            build_pointcloud(source, output)
            self.assertEqual(output.read_bytes()[28:31], bytes([90, 80, 70]))

    def test_failed_rebuild_preserves_previous_cloud(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root/'sfm.json', root/'pointcloud.bin'
            source.write_text('{"structure":[{"X":[1,2,3]}]}')
            build_pointcloud(source, output)
            original = output.read_bytes()
            source.write_text('{"structure":[{"X":[4,5,6]},')
            with self.assertRaises(IncompleteJSONError):
                build_pointcloud(source, output)
            self.assertEqual(output.read_bytes(), original)
            self.assertFalse(list(root.glob('.pointcloud-*.tmp')))


if __name__ == '__main__':
    unittest.main()
