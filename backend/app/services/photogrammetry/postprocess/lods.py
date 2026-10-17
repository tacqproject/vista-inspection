"""
Generate glTF LOD levels via gltfpack (meshoptimizer) so heavy meshes stay
smooth in the browser. Textures remain external files, shared by all levels.

`generate_lods` is also used standalone by the model-crop endpoint.
"""

import asyncio
import shutil
from pathlib import Path

from app.config import settings
from ..base import ReconstructionContext, postprocess_step

# (suffix, simplification ratio) — lod0 keeps the FULL mesh (ratio None → no
# -si) so well-covered areas stay crisp up close; only the farther levels are
# decimated to lighten distant/mobile rendering. Quality up close beats the
# extra GPU cost for an inspection tool.
LOD_LEVELS = [("lod0", None), ("lod1", 0.25), ("lod2", 0.06)]


def _find_gltfpack() -> str | None:
    exe = shutil.which(settings.gltfpack_path)
    if not exe:
        candidate = Path(settings.gltfpack_path)
        if candidate.is_absolute() and candidate.is_file():
            exe = str(candidate)
    return exe


async def generate_lods(obj_file: Path, prefix: str = "model") -> Path | None:
    """
    Write {prefix}_lod0/1/2.gltf next to the OBJ.

    Returns the LOD0 path, or None if gltfpack is unavailable or LOD0 itself
    fails. LOD0 is the level the viewer actually loads; the farther, simplified
    levels only lighten distant rendering, so if one of THEM fails we keep the
    levels already produced rather than discarding a perfectly good model (which
    would otherwise fall back to the raw .obj the glTF viewer cannot open).
    """
    exe = _find_gltfpack()
    if not exe:
        return None

    out_dir = obj_file.parent
    produced: list[Path] = []
    for suffix, ratio in LOD_LEVELS:
        out = out_dir / f"{prefix}_{suffix}.gltf"
        cmd = [
            exe, "-i", str(obj_file), "-o", str(out),
            "-noq",                       # keep full float precision (accuracy)
        ]
        if ratio is not None:
            cmd += ["-si", str(ratio)]    # simplify only the farther levels
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode == 0 and out.is_file():
            produced.append(out)
            continue
        if suffix == "lod0":
            return None                   # nothing to show without the base level
        # a farther level failed — stop (coarser levels would fail the same way)
        # and keep the contiguous levels we already have
        import logging
        logging.getLogger(__name__).warning(
            "gltfpack %s failed (rc=%s): %s", suffix, proc.returncode,
            (err or b"").decode("utf-8", "replace")[:400],
        )
        break

    return produced[0] if produced else None


@postprocess_step("generate-lods", order=30)
async def run(ctx: ReconstructionContext) -> None:
    if ctx.params.get('_regional_plan'):
        return  # generated incrementally from regions, without loading one huge OBJ
    if not ctx.obj_file or not ctx.params.get("web_postprocess", True):
        return
    await ctx.report(98, "Generování LOD úrovní…")
    from ..runtime import run_python_stage
    result = await asyncio.to_thread(run_python_stage, 'lods', {
        'mesh': str(ctx.obj_file), 'result': str(ctx.models_dir / 'lod-result.json')},
        ctx.models_dir / 'lod.log')
    lod0 = Path(result['model'])
    if lod0:
        ctx.result["model_path"] = lod0
