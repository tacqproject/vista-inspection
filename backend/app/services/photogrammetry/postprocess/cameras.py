"""Keep the current graph's camera frame and SfM, retaining restart checkpoints."""
import os
import shutil
from ..base import ReconstructionContext, postprocess_step
from ..runtime import current_sfm
from .openmvs import _export_sfm_json


@postprocess_step('harvest-cameras', order=10)
async def run(ctx: ReconstructionContext) -> None:
    cache = ctx.models_dir / 'meshroom_cache'
    if not cache.exists() or not ctx.obj_file:
        return
    sfm = current_sfm(cache)
    out = ctx.obj_file.parent
    shutil.copy2(sfm / 'cameras.sfm', out / 'cameras.sfm')
    source = await _export_sfm_json(cache, ctx.models_dir / 'sfm_convert.log')
    target = out / 'sfm.json'
    if target.exists() and os.path.samefile(source, target):
        return
    temp = out / 'sfm-pending.json'
    temp.unlink(missing_ok=True)
    try:
        os.link(source, temp)  # same SSD: avoid a second multi-GB copy
    except OSError:
        shutil.copy2(source, temp)
    temp.replace(target)
