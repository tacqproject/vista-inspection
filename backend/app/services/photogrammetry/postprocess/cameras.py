"""
Harvest camera poses from the Meshroom cache, then drop the heavy computation
cache. The poses power the "show this spot on source photos" feature.

Besides cameras.sfm we export the full SfM as JSON (sfm.json) including the
3D landmarks with their 2D observations — exact 3D↔pixel ground truth used
to verify/calibrate the projection (see app/services/sfm.py).
"""

import asyncio
import os
import shutil

from app.config import settings
from ..base import ReconstructionContext, postprocess_step


@postprocess_step("harvest-cameras", order=10)
async def run(ctx: ReconstructionContext) -> None:
    cache_dir = ctx.models_dir / "meshroom_cache"
    if not cache_dir.exists():
        return

    # The cache may hold SfM results from several (aborted) runs — each run has
    # its own coordinate frame, and pairing the mesh with a foreign bundle
    # scatters the photo projections. Take the newest one (= current run).
    sfm_files = sorted(
        cache_dir.glob("**/StructureFromMotion/*/cameras.sfm"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if sfm_files and ctx.obj_file:
        out_dir = ctx.obj_file.parent
        shutil.copy2(sfm_files[0], out_dir / "cameras.sfm")

        # Full SfM incl. landmarks+observations (ground truth for projection).
        # Output must go into the mounted cache dir — the Docker wrapper only
        # bind-mounts existing directories, so a host file path would fail.
        abc = sfm_files[0].parent / "sfm.abc"
        if abc.exists():
            rel = abc.relative_to(cache_dir)
            cmd = [
                settings.meshroom_path,
                "--input", f"/tmp/MeshroomCache/{rel}",
                "--output", "/tmp/MeshroomCache/sfm.json",
                "--describerTypes", "dspsift",
            ]
            env = {
                **os.environ,
                "VISTA_MESHROOM_CACHE": str(cache_dir),
                "VISTA_MESHROOM_BIN": "aliceVision_convertSfMFormat",
            }
            with open(ctx.models_dir / "sfm_convert.log", "w") as log:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT,
                )
                await proc.wait()  # best effort — cameras.sfm already saved
            if (cache_dir / "sfm.json").exists():
                shutil.copy2(cache_dir / "sfm.json", out_dir / "sfm.json")

    shutil.rmtree(cache_dir, ignore_errors=True)
