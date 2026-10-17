"""
Streamed hierarchical tiles (OGC 3D Tiles) for the viewer — see
app/services/tiles.py. Runs the tiler in a SEPARATE process (pymeshlab, xatlas,
embree and ~2 GB of decoded atlases stay out of the API worker) and waits for
it, so a finished reconstruction already streams. Best effort: if tiling fails
the model is still complete and the viewer falls back to the glTF LODs.
"""

import asyncio
import logging
from pathlib import Path

from ..base import ReconstructionContext, postprocess_step

log = logging.getLogger(__name__)


@postprocess_step("generate-tiles", order=35)
async def run(ctx: ReconstructionContext) -> None:
    lod0 = ctx.result.get("model_path")
    if not lod0 or not ctx.params.get("web_postprocess", True):
        return
    lod0 = Path(lod0)
    if not lod0.name.endswith("_lod0.gltf") or not lod0.exists():
        return
    from app.services import tiles

    await ctx.report(99, "Příprava streamovaných dlaždic (detail při přiblížení)…")
    log_file = lod0.parent / "tiles_build.log"
    try:
        from ..runtime import run_python_stage
        if ctx.params.get('_regional_plan'):
            await asyncio.to_thread(run_python_stage, 'region-tiles', {
                'plan': ctx.params['_regional_plan'], 'output': str(lod0.parent),
                'result': str(ctx.models_dir / 'tiles-result.json')}, log_file)
            return
        await asyncio.to_thread(run_python_stage, 'tiles', {
            'mesh': str(lod0), 'result': str(ctx.models_dir / 'tiles-result.json')}, log_file)
    except Exception as e:  # noqa: BLE001 — never fail a reconstruction over tiles
        log.warning("tile build could not start: %s", e)
