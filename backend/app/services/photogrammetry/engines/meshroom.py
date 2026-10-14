"""
Meshroom (AliceVision) engine.

Runs the AliceVision pipeline via meshroom_batch — either natively or through
the Docker wrapper (MESHROOM_PATH in .env). Requires an NVIDIA GPU for the
DepthMap stage.

Reconstruction params (ctx.params):
  quality            draft | standard | high  → QUALITY_PRESETS
  keep_largest_mesh  bool — drop floating fragments (default True)
  max_points         int  — mesh density override
  texture_size       int  — texture side override (4096/8192)
"""

import asyncio
import os
import shutil
from pathlib import Path

from app.config import settings
from ..base import ReconstructionContext, engine

# Quality presets → "NodeType:param" overrides for meshroom_batch --paramOverrides
# NB: Texturing:downscale is the single biggest texture-sharpness lever —
# AliceVision defaults it to 2 (textures sampled at half image resolution),
# which looks blurry. We force 1 (full-res) for standard/high. fillHoles paints
# over small untextured gaps instead of leaving black patches.
QUALITY_PRESETS = {
    "draft": {
        "FeatureExtraction:describerPreset": "normal",
        "DepthMap:downscale": 4,
        "Meshing:maxPoints": 300000,
        "Texturing:textureSide": 4096,
        "Texturing:downscale": 2,
    },
    "standard": {
        "FeatureExtraction:describerPreset": "normal",
        "DepthMap:downscale": 2,
        "Meshing:maxPoints": 2000000,
        "Texturing:textureSide": 8192,
        "Texturing:downscale": 1,
        "Texturing:fillHoles": "True",
    },
    "high": {
        "FeatureExtraction:describerPreset": "high",
        "DepthMap:downscale": 1,
        "Meshing:maxPoints": 5000000,
        "Texturing:textureSide": 8192,
        "Texturing:downscale": 1,
        "Texturing:fillHoles": "True",
    },
    # Maximum accuracy: AliceVision only computes the camera poses (SfM with the
    # richer "high" feature preset); dense reconstruction, meshing, photometric
    # refinement and texturing are done by OpenMVS (postprocess/openmvs.py) —
    # measured ≈ 2× lower deviation from the SfM landmarks than AliceVision's
    # depth-map fusion, holes closed by PyMeshLab afterwards.
    "max": {
        "FeatureExtraction:describerPreset": "high",
    },
}

# Pipeline stage names (matched in stdout) → approximate progress %
_STAGES = [
    ("CameraInit",          5,  "Inicializace kamer…"),
    ("FeatureExtraction",   15, "Extrakce příznaků snímků…"),
    ("ImageMatching",       28, "Vyhledávání shod mezi snímky…"),
    ("FeatureMatching",     38, "Shoda příznaků…"),
    ("StructureFromMotion", 52, "Rekonstrukce struktury scény…"),
    ("PrepareDenseScene",   57, "Příprava husté scény…"),
    ("DepthMap",            67, "Výpočet hloubkových map (GPU)…"),
    ("DepthMapFilter",      74, "Filtrování hloubkových map…"),
    ("Meshing",             82, "Generování 3D sítě…"),
    ("MeshFiltering",       87, "Filtrování sítě…"),
    ("Texturing",           94, "Texturování modelu…"),
]


def _find_binary() -> str | None:
    meshroom_bin = settings.meshroom_path or "meshroom_batch"
    exe = shutil.which(meshroom_bin)
    if not exe:
        candidate = Path(meshroom_bin)
        if candidate.is_absolute() and candidate.is_file():
            exe = str(candidate)
    return exe


def build_overrides(params: dict | None) -> list[str]:
    """Translate user-facing params into --paramOverrides arguments."""
    params = params or {}
    overrides = dict(QUALITY_PRESETS.get(params.get("quality", "standard"),
                                         QUALITY_PRESETS["standard"]))
    if params.get("keep_largest_mesh", True):
        overrides["MeshFiltering:keepLargestMeshOnly"] = "True"
    if params.get("max_points"):
        overrides["Meshing:maxPoints"] = int(params["max_points"])
    if params.get("texture_size"):
        overrides["Texturing:textureSide"] = int(params["texture_size"])
    if params.get("texture_downscale"):
        overrides["Texturing:downscale"] = int(params["texture_downscale"])
    return [f"{k}={v}" for k, v in overrides.items()]


async def _check() -> dict:
    exe = _find_binary()
    if not exe:
        return {
            "available": False,
            "path": settings.meshroom_path,
            "hint": "Nastavte MESHROOM_PATH=/cesta/k/meshroom_batch v souboru .env",
        }
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        version = (out or b"").decode(errors="replace").strip().split("\n")[0] or "neznámá"
    except Exception:
        version = "neznámá"
    return {"available": True, "path": exe, "version": version}


@engine("meshroom", check=_check)
async def run(ctx: ReconstructionContext) -> None:
    output_dir = ctx.models_dir / "meshroom_out"
    output_dir.mkdir(exist_ok=True)
    cache_dir = ctx.models_dir / "meshroom_cache"
    cache_dir.mkdir(exist_ok=True)

    exe = _find_binary()
    if not exe:
        raise RuntimeError(
            f"Meshroom nenalezen ('{settings.meshroom_path}'). "
            "Nastavte MESHROOM_PATH=/cesta/k/meshroom_batch v souboru .env"
        )

    # --cache alone is ignored by meshroom_batch; --save makes it take effect.
    # The Docker wrapper additionally mounts VISTA_MESHROOM_CACHE over the
    # container's /tmp/MeshroomCache so cache lands on the host either way.
    cmd = [
        exe, "--input", str(ctx.photos_dir), "--output", str(output_dir),
        "--cache", str(cache_dir), "--save", str(cache_dir / "project.mg"),
    ]

    # Custom node graph imported from Meshroom GUI (uploads/pipelines/*.mg)
    if ctx.params.get("pipeline"):
        from pathlib import Path as _P
        mg = (settings.upload_path / "pipelines" / _P(ctx.params["pipeline"]).name).resolve()
        if not mg.is_file():
            raise RuntimeError(f"Pipeline šablona '{ctx.params['pipeline']}' nebyla nalezena")
        cmd += ["--pipeline", str(mg)]
    # With mesh refinement the pipeline stops before Texturing — the refine
    # post-processing step cleans the mesh (PyMeshLab) and textures it itself.
    from ..postprocess.refine import wants_refine
    refine = wants_refine(ctx.params)
    from ..postprocess.openmvs import uses_openmvs
    sfm_only = uses_openmvs(ctx.params)
    if sfm_only:
        cmd += ["--toNode", "StructureFromMotion"]
    elif refine:
        cmd += ["--toNode", "MeshFiltering"]
    overrides = build_overrides(ctx.params)
    if overrides:
        cmd += ["--paramOverrides", *overrides]

    env = {**os.environ, "VISTA_MESHROOM_CACHE": str(cache_dir)}
    log_file = ctx.models_dir / "meshroom.log"

    await ctx.report(3, "Spouštím Meshroom pipeline…")

    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    reported: set[str] = set()
    assert proc.stdout is not None
    with open(log_file, "w", encoding="utf-8") as lf:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            lf.write(line)
            lf.flush()
            line = line.strip()
            if not line:
                continue
            for stage, pct, msg in _STAGES:
                if stage in line and stage not in reported:
                    reported.add(stage)
                    await ctx.report(pct, f"Meshroom: {msg}")
                    break

    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"Meshroom skončil s chybou (kód {rc}). Detaily: {log_file}")

    if refine or sfm_only:
        # Raw mesh / SfM lives in the cache; refine or OpenMVS produces the OBJ.
        ctx.result = {"model_path": None, "point_cloud_path": None,
                      "orthophoto_path": None, "engine_used": "meshroom"}
        return

    obj_files = sorted(output_dir.glob("**/*.obj"))
    if not obj_files:
        raise RuntimeError(
            "Meshroom nedokončil texturování — soubor .obj nebyl nalezen ve výstupu. "
            "Zkontrolujte, zda máte dostatek GPU paměti a alespoň 20 překrývajících se snímků."
        )
    ctx.obj_file = next((f for f in obj_files if "textured" in f.stem.lower()), obj_files[0])

    ctx.result = {
        "model_path": ctx.obj_file,
        "point_cloud_path": None,
        "orthophoto_path": None,
        "engine_used": "meshroom",
    }
