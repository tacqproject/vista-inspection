"""
AI defect inspection → defects placed in the 3D model (app/services/defects_auto.py).
Detection on the photos was started in parallel with the reconstruction; this
step waits for it, then projects, merges, measures and writes the annotations.
Best effort: a failure never fails the reconstruction.
"""
import asyncio
import logging

from ..base import ReconstructionContext, postprocess_step

log = logging.getLogger(__name__)


@postprocess_step("ai-defects", order=60)
async def run(ctx: ReconstructionContext) -> None:
    if not ctx.params.get("ai_defects") or not ctx.obj_file:
        return
    from app.services import defects_auto as D
    try:
        await ctx.report(99, "AI: dokončuji detekci poruch na fotkách…")
        ok = await asyncio.to_thread(D.wait_detection, ctx.inspection_id)
        if not ok:
            await asyncio.to_thread(D.detect_if_needed, ctx.inspection_id)
        await ctx.report(99, "AI: zanáším poruchy do 3D modelu…")
        summary = await D.annotate(ctx.inspection_id)
        log.info("AI defects for %s: %s", ctx.inspection_id, summary)
    except Exception as e:  # noqa: BLE001
        log.warning("AI defects failed for %s: %s", ctx.inspection_id, e)
        D._set_status(ctx.inspection_id, status="failed", error=str(e)[:300])
