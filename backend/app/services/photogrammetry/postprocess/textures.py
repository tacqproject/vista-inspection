"""
Convert EXR texture maps to JPEG — browsers cannot render EXR.
Rewrites .mtl references and removes the originals. No-op when the model
directory contains no EXR files (e.g. ODM/mock output).
"""

import asyncio

from ..base import ReconstructionContext, postprocess_step


@postprocess_step("convert-exr-textures", order=20)
async def run(ctx: ReconstructionContext) -> None:
    if not ctx.obj_file or not ctx.params.get("web_postprocess", True):
        return
    model_dir = ctx.obj_file.parent
    if not any(model_dir.glob("*.exr")):
        return

    await ctx.report(97, "Konverze textur pro web…")
    await asyncio.to_thread(_convert, model_dir)


def _convert(model_dir) -> None:
    import OpenEXR
    import numpy as np
    from PIL import Image

    converted: list[str] = []          # exact filenames actually converted
    for exr in sorted(model_dir.glob("*.exr")):
        with OpenEXR.File(str(exr)) as f:
            ch = f.channels()
            if "RGB" in ch:
                rgb = np.asarray(ch["RGB"].pixels, dtype=np.float32)
            elif all(k in ch for k in "RGB"):
                rgb = np.stack(
                    [np.asarray(ch[k].pixels, dtype=np.float32) for k in "RGB"], axis=-1
                )
            else:
                continue
        # Linear EXR → sRGB 8-bit
        rgb = np.clip(rgb, 0.0, 1.0) ** (1.0 / 2.2)
        Image.fromarray((rgb * 255).astype("uint8")).save(
            exr.with_suffix(".jpg"), format="JPEG", quality=90,
        )
        exr.unlink()
        converted.append(exr.name)

    # Rewrite ONLY the references we actually converted — a blanket
    # ".exr"→".jpg" would repoint a skipped EXR (one with no usable RGB) at a
    # .jpg that was never written, leaving the material pointing at a missing file.
    if converted:
        for mtl in model_dir.glob("*.mtl"):
            text = mtl.read_text()
            for exr_name in converted:
                text = text.replace(exr_name, exr_name[:-4] + ".jpg")
            mtl.write_text(text)
