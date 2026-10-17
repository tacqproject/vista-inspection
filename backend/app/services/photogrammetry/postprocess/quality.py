"""
Quality report (metrics + renders from source-photo viewpoints) for every
reconstruction — see app/services/quality_report.py. Separate process, best
effort: a failure never fails the reconstruction.
"""
import asyncio
import logging
import sys

from ..base import ReconstructionContext, postprocess_step

log = logging.getLogger(__name__)


@postprocess_step("quality-report", order=50)
async def run(ctx: ReconstructionContext) -> None:
    if not ctx.obj_file:
        return
    if ctx.params.get('_regional_plan'):
        return  # per-region bake/coverage reports accompany the full-resolution model
    from app.services import tiles
    await ctx.report(99, "Vyhodnocení kvality modelu…")
    model_dir = ctx.obj_file.parent
    try:
        from ..runtime import run_python_stage
        await asyncio.to_thread(run_python_stage, 'quality-report', {
            'model': str(model_dir), 'photos': str(ctx.photos_dir),
            'result': str(ctx.models_dir / 'quality-result.json')}, model_dir / 'quality_report.log')
    except Exception as e:  # noqa: BLE001
        log.warning("quality report could not run: %s", e)
