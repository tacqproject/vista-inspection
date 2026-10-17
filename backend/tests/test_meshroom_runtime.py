"""Regressions from the 798-DNG OOM and false 94% progress on failure."""
import json
from pathlib import Path
import tempfile
import unittest

from app.services.photogrammetry.engines.meshroom import build_overrides, _cache_progress


class MeshroomRuntimeTests(unittest.TestCase):
    def test_all_quality_presets_bound_extraction_concurrency(self):
        for quality in ("draft", "standard", "high", "max", "unknown"):
            with self.subTest(quality=quality):
                overrides = dict(item.split("=", 1) for item in build_overrides({"quality": quality}))
                threads = int(overrides["FeatureExtraction:maxThreads"])
                self.assertGreater(threads, 0)  # zero enables host-based automatic sizing
                self.assertLessEqual(threads, 8)  # 24 GiB worker, ~1.5 GiB per image
                self.assertEqual(overrides['StructureFromMotion:computeStructureColor'], 'False')
                self.assertEqual(overrides["FeatureExtraction:describerPreset"],
                                 "high" if quality in {"high", "max"} else "normal")

    def test_progress_uses_current_graph_and_executed_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            def status(stage, uid, filename, value):
                p = cache / stage / uid / filename
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"status": value}))

            graph = {
                "CameraInit_1": {"nodeType": "CameraInit", "uid": "camera",
                                 "parallelization": {"blockSize": 0, "size": 798, "split": 1}},
                "FeatureExtraction_1": {"nodeType": "FeatureExtraction", "uid": "features",
                                        "parallelization": {"blockSize": 40, "size": 798, "split": 20}},
                "Texturing_1": {"nodeType": "Texturing", "uid": "texture",
                                "parallelization": {"blockSize": 0, "size": 1, "split": 1}},
            }
            project = cache / "project.mg"
            project.write_text(json.dumps({"graph": graph}))
            status("CameraInit", "camera", "status", "SUCCESS")
            status("FeatureExtraction", "features", "0.status", "RUNNING")
            status("Texturing", "texture", "status", "SUBMITTED")
            # An earlier graph's completed texture must not count for this run.
            status("Texturing", "old-texture", "status", "SUCCESS")
            pct, message = _cache_progress(cache, None)
            self.assertEqual(pct, 15)
            self.assertIn("0/20", message)
            self.assertIsNone(_cache_progress(cache, project.stat().st_mtime_ns))

            # Failure cleanup mentions every node in stdout; it is not progress.
            status("FeatureExtraction", "features", "0.status", "ERROR")
            status("Texturing", "texture", "status", "NONE")
            self.assertEqual(_cache_progress(cache, None)[0], 15)
            for i in range(20):
                status("FeatureExtraction", "features", f"{i}.status", "SUCCESS")
            pct, message = _cache_progress(cache, None)
            self.assertEqual(pct, 27)
            self.assertIn("20/20", message)

            # Polling may catch a status or project file between writes.
            (cache / "FeatureExtraction/features/0.status").write_text("{")
            self.assertLess(_cache_progress(cache, None)[0], 27)
            project.write_text("{")
            self.assertIsNone(_cache_progress(cache, None))


if __name__ == "__main__":
    unittest.main()
