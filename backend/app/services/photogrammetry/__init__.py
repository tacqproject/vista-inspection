"""
Photogrammetry pipeline — public API.

    result = await run_reconstruction(inspection_id, on_progress, params)

Flow: the AliceVision (Meshroom) engine produces the raw mesh, then every
registered post-processing step runs in order (mesh refine → camera harvest
→ EXR→JPG → LOD levels → mobile variant). See base.py for the architecture
and how to add engines or processing steps.
"""

from pathlib import Path

from app.services.storage import get_photos_dir, get_models_dir, get_relative_path

from .base import ENGINES, POSTPROCESS_STEPS, ProgressCallback, ReconstructionContext
from . import engines, postprocess  # noqa: F401  — imports populate the registries
from .postprocess import generate_lods  # noqa: F401  — re-export
from .engines.meshroom import QUALITY_PRESETS  # noqa: F401  — re-export


async def _prepare_photos(ctx: ReconstructionContext) -> None:
    """Exposure balance + global tone curve + culling of blurred/duplicate
    photos (app/services/photo_prep.py) into a prepared copy that the
    reconstruction uses; originals stay for the app. Best effort."""
    import asyncio
    import json
    import logging
    from app.config import settings
    from app.services import photo_prep
    base = Path(settings.work_dir) / ctx.inspection_id if settings.work_dir else ctx.models_dir
    out = base / "photos_prep"
    await ctx.report(1, "Příprava fotek: vyvážení expozice, stíny, vyřazení rozmazaných a duplicitních…")
    try:
        rep = await asyncio.to_thread(photo_prep.prepare, ctx.photos_dir, out,
                                      float(ctx.params.get("shadow_lift", 1.0)),
                                      bool(ctx.params.get("photo_cull", True)))
        ctx.models_dir.mkdir(parents=True, exist_ok=True)
        (ctx.models_dir / "photo_prep.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        if rep["n_used"] >= 3:
            ctx.photos_dir = out.resolve()
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("photo prep failed, using originals: %s", e)


async def run_reconstruction(
    inspection_id: str,
    on_progress: ProgressCallback | None = None,
    params: dict | None = None,
) -> dict:
    """
    Run 3D reconstruction for an inspection and return a dict with
    model_path / point_cloud_path / orthophoto_path (upload-relative strings)
    and engine_used.
    """
    spec = ENGINES["meshroom"]

    ctx = ReconstructionContext(
        inspection_id=inspection_id,
        photos_dir=get_photos_dir(inspection_id).resolve(),
        models_dir=get_models_dir(inspection_id).resolve(),
        params=params or {},
        progress=on_progress,
    )

    if ctx.params.get("photo_prep", True):
        await _prepare_photos(ctx)

    await spec.run(ctx)

    for step in POSTPROCESS_STEPS:
        await step.run(ctx)

    await ctx.report(100, "Rekonstrukce dokončena!")

    # Finalize: absolute Paths → upload-relative strings for the DB/API
    out = dict(ctx.result)
    for key in ("model_path", "point_cloud_path", "orthophoto_path"):
        val = out.get(key)
        out[key] = get_relative_path(Path(val)) if val else None
    return out


async def check_engine_availability() -> dict:
    """Availability status of the AliceVision engine (for /config/engines)."""
    info = await ENGINES["meshroom"].check()
    return {
        "current": "meshroom",
        "available": ["meshroom"] if info.get("available") else [],
        "meshroom": info,
    }
