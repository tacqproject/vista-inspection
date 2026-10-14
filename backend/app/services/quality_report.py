"""
Quality report of a finished reconstruction — objective numbers + visual proof,
stored next to the model (quality/report.json + render_N.jpg) and shown in the app,
so every reconstruction (and every pipeline change) can be judged at a glance.

  metrics  mesh_quality.evaluate(): holes, surface roughness (flat-area noise),
           deviation from the SfM landmarks, registered photos, size
  renders  the model seen from the exact pose of 3 source photos (photo | textured
           | bare geometry) — lumpy walls, texture ghosting or misalignment are
           obvious here, even where the numbers look fine

Runs in its own process (Open3D offscreen rendering uses EGL; keep it out of the
API worker):  python -m app.services.quality_report <model_dir> <photos_dir>
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def build_report(model_dir: Path, photos_dir: Path, n_renders: int = 3) -> dict:
    from app.services import mesh_quality, mesh_render, sfm

    model_dir, photos_dir = Path(model_dir), Path(photos_dir)
    mesh = model_dir / "texturedMesh.obj"
    if not mesh.exists():
        raise FileNotFoundError(mesh)
    out = model_dir / "quality"
    out.mkdir(exist_ok=True)
    t0 = time.time()
    metrics = mesh_quality.evaluate(mesh, model_dir / "sfm.json" if (model_dir / "sfm.json").exists() else None)

    renders = []
    cams_path = model_dir / "cameras.sfm"
    if cams_path.exists():
        cams = sorted(sfm.load_cameras(cams_path), key=lambda c: c.filename)
        picks = [cams[int(i)] for i in
                 [len(cams) * (k + 1) / (n_renders + 1) for k in range(n_renders)]] if cams else []
        for k, cam in enumerate(picks, 1):
            dst = out / f"render_{k}.jpg"
            try:
                mesh_render.render_from_photo(mesh, cams_path, cam.filename, dst, photos_dir, width=900)
                renders.append({"file": dst.name, "photo": cam.filename})
            except Exception as e:  # noqa: BLE001
                renders.append({"file": None, "photo": cam.filename, "error": str(e)[:200]})

    report = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seconds": round(time.time() - t0, 1),
        "metrics": metrics,
        "renders": renders,
        "notes": {
            "rough_median": "šum ploch (lokální odchylka od roviny / poloměr); nižší = hladší",
            "rough_p25": "šum nejrovnějších ploch (stěny, pilíře)",
            "lm_median_rel": "odchylka od SfM bodů, podíl úhlopříčky; nižší = přesnější",
            "significant_holes": "díry s obvodem > 1 % úhlopříčky (mimo okraj skenu)",
        },
    }
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report


if __name__ == "__main__":
    r = build_report(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(r["metrics"], indent=1))
