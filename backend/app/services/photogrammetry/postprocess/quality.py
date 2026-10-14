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
    from app.services import tiles
    await ctx.report(99, "Vyhodnocení kvality modelu…")
    model_dir = ctx.obj_file.parent
    try:
        with open(model_dir / "quality_report.log", "w") as fh:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.services.quality_report", str(model_dir), str(ctx.photos_dir),
                cwd=tiles.BACKEND_DIR, stdout=fh, stderr=asyncio.subprocess.STDOUT)
            if await proc.wait() != 0:
                log.warning("quality report failed, see %s", model_dir / "quality_report.log")
    except Exception as e:  # noqa: BLE001
        log.warning("quality report could not run: %s", e)
