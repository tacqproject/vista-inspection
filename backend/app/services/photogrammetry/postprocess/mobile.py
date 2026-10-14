"""
Mobile/low-GPU model variant.

The full model ships 8K texture tiles — decoded that's ~2 GB of GPU memory,
which kills the tab on iPads/phones (iOS Safari has hard limits and older
devices cap textures at 4096 px). This step downsizes every texture to 2048 px
(texture_XXXX_m.jpg) and writes *_mobile.gltf copies of each LOD that
reference them; geometry buffers (.bin) are shared with the full variant.
The frontend picks the mobile variant on constrained devices.
"""

import asyncio
import json

from ..base import ReconstructionContext, postprocess_step

MOBILE_TEXTURE_SIZE = 2048


def _build(model_dir) -> int:
    from PIL import Image

    textures = [
        t for t in sorted(model_dir.glob("texture_*.jpg"))
        if not t.stem.endswith("_m")
    ]
    for tex in textures:
        dest = tex.with_stem(tex.stem + "_m")
        if dest.exists():
            continue
        with Image.open(tex) as img:
            img.thumbnail((MOBILE_TEXTURE_SIZE, MOBILE_TEXTURE_SIZE), Image.LANCZOS)
            img.save(dest, format="JPEG", quality=85, optimize=True)

    made = 0
    for gltf_path in sorted(model_dir.glob("model_*.gltf")):   # incl. crop variants
        if gltf_path.stem.endswith("_mobile"):
            continue
        data = json.loads(gltf_path.read_text())
        for image in data.get("images", []):
            uri = image.get("uri", "")
            if uri.startswith("texture_") and uri.endswith(".jpg") and not uri.endswith("_m.jpg"):
                image["uri"] = uri[:-4] + "_m.jpg"
        out = gltf_path.with_stem(gltf_path.stem + "_mobile")
        out.write_text(json.dumps(data))
        made += 1
    return made


@postprocess_step("mobile-variant", order=40)
async def run(ctx: ReconstructionContext) -> None:
    if not ctx.obj_file or not ctx.params.get("web_postprocess", True):
        return
    await ctx.report(99, "Generování mobilní varianty…")
    await asyncio.to_thread(_build, ctx.obj_file.parent)
