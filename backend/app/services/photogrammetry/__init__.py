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


async def _run_reconstruction_locked(
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

    from app.services.raw_images import is_raw
    from app.services.storage import ALLOWED_IMAGE_EXTENSIONS
    import json
    sources = sorted(p for p in ctx.photos_dir.iterdir() if p.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS)
    has_raw = any(is_raw(p) for p in sources)
    (ctx.models_dir / 'source_manifest.json').write_text(json.dumps({
        'input': 'originals' if has_raw else 'photos',
        'raw': has_raw,
        'photos': [{'filename': p.name, 'source': str(p.resolve()), 'bytes': p.stat().st_size} for p in sources],
    }, indent=2))
    if has_raw:
        await ctx.report(1, 'RAW: rekonstrukce z původních DNG, bez JPEG předzpracování…')
    elif ctx.params.get("photo_prep", True):
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


async def run_reconstruction(inspection_id, on_progress=None, params=None):
    """The RAW pipeline owns one lock across all stages, including recovery."""
    import fcntl
    from app.config import settings
    from .runtime import atomic_json
    root = Path(settings.isolated_data_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'reconstruction.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('V RAW instanci už běží jiná rekonstrukce.')
        atomic_json(get_models_dir(inspection_id) / 'run-parameters.json', params or {})
        try:
            return await _run_reconstruction_locked(inspection_id, on_progress, params)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def check_engine_availability() -> dict:
    """Availability status of the AliceVision engine (for /config/engines)."""
    info = await ENGINES["meshroom"].check()
    return {
        "current": "meshroom",
        "available": ["meshroom"] if info.get("available") else [],
        "meshroom": info,
    }
