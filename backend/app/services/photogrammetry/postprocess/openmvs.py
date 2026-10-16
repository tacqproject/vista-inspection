"""
"Maximální přesnost" (quality = "max"): AliceVision SfM → OpenMVS dense
reconstruction, meshing and photometric RefineMesh → PyMeshLab hole closing →
OpenMVS texturing.

Measured on a real scan against the SfM landmarks (app/services/mesh_quality.py),
same SfM, same reference points:

                       AliceVision (standard)   this path
  median deviation     0.056 % of diagonal      0.027 %   (≈ 2× more accurate)
  p90 deviation        0.42 %                   0.25 %
  significant holes    6                        0–1 (only true scan edges stay open)

The meshroom engine stops at StructureFromMotion for this preset; this step
produces the textured mesh in the app's mesh frame (SfM frame with y,z negated,
like AliceVision's OBJ output), so camera harvest, photo projection, LODs and
streamed tiles work unchanged. Runs before refine-mesh (which it replaces).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from app.config import settings
from ..base import ReconstructionContext, postprocess_step

CONTAINER_CACHE = "/tmp/MeshroomCache"


def uses_openmvs(params: dict) -> bool:
    return params.get("quality") == "max"


async def _export_sfm_json(cache_dir: Path, log: Path) -> Path:
    """Newest SfM of the cache → sfm.json with landmarks + observations."""
    sfm_files = sorted(cache_dir.glob("StructureFromMotion/*/sfm.abc"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not sfm_files:
        raise RuntimeError("SfM výstup nenalezen — rekonstrukce pozic kamer selhala.")
    rel = sfm_files[0].relative_to(cache_dir)
    out = cache_dir / "sfm.json"
    env = {**os.environ, "VISTA_MESHROOM_CACHE": str(cache_dir),
           "VISTA_MESHROOM_BIN": "aliceVision_convertSfMFormat"}
    with open(log, "a") as fh:
        proc = await asyncio.create_subprocess_exec(
            settings.meshroom_path, "--input", f"{CONTAINER_CACHE}/{rel}",
            "--output", f"{CONTAINER_CACHE}/sfm.json", "--describerTypes", "dspsift",
            env=env, stdout=fh, stderr=asyncio.subprocess.STDOUT)
        await proc.wait()
    if not out.exists():
        raise RuntimeError("Export SfM (sfm.json) selhal.")
    return out


def _to_app_frame(obj_in: Path, out_dir: Path) -> Path:
    """OpenMVS OBJ (SfM frame) → texturedMesh.obj in the app's mesh frame
    (y,z negated: a 180° rotation about X, so winding/normals stay valid),
    textures renamed texture_1001.jpg… like AliceVision's output (the mobile
    variant and texture tagging expect that naming)."""
    src_dir = obj_in.parent
    mtl_in = obj_in.with_suffix(".mtl")
    rename: dict[str, str] = {}
    mtl_lines = []
    if mtl_in.exists():
        for line in mtl_in.read_text().splitlines():
            m = re.match(r"^(\s*map_Kd\s+)(.+)$", line)
            if m:
                src = m.group(2).strip()
                if src not in rename:
                    rename[src] = f"texture_{1001 + len(rename)}{Path(src).suffix.lower()}"
                    shutil.copy2(src_dir / src, out_dir / rename[src])
                line = m.group(1) + rename[src]
            mtl_lines.append(line)
        (out_dir / "texturedMesh.mtl").write_text("\n".join(mtl_lines) + "\n")
    obj_out = out_dir / "texturedMesh.obj"
    with open(obj_in) as fi, open(obj_out, "w") as fo:
        for line in fi:
            if line.startswith("v ") or line.startswith("vn "):
                p = line.split()
                fo.write(f"{p[0]} {p[1]} {-float(p[2]):.9g} {-float(p[3]):.9g}\n")
            elif line.startswith("mtllib"):
                fo.write("mtllib texturedMesh.mtl\n")
            else:
                fo.write(line)
    return obj_out


async def _ai_denoise(ctx: ReconstructionContext, work: Path, mesh: Path, log: Path) -> Path:
    """AI step: DSINE predicts surface normals in every photo; they steer the
    removal of MVS noise (app/services/normal_prior.py) — flat walls stay flat,
    edges and cracks the network sees stay sharp. Best effort: on any failure
    the photometrically refined mesh is used as is."""
    from app.services.photogrammetry import openmvs_bridge as O
    import sys
    await ctx.report(80, "AI: odhad normál povrchu z fotek (DSINE)…")
    try:
        with open(log, "a") as fh:
            await asyncio.to_thread(O.predict_normals, work, work / "normals", 1344, fh)
            await ctx.report(83, "AI: odšumění povrchu podle normál…")
            out = work / "mesh_ai.ply"
            # own process: the mesh + sparse operators need several GB of RAM
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.services.normal_prior", str(mesh), str(out),
                str(work), str(work / "normals"),
                cwd=str(Path(__file__).resolve().parents[4]), stdout=fh, stderr=asyncio.subprocess.STDOUT)
            if await proc.wait() != 0 or not out.exists():
                raise RuntimeError(f"normal_prior rc={proc.returncode}")
        return out
    except Exception as e:  # noqa: BLE001
        with open(log, "a") as fh:
            fh.write(f"\nAI odšumění přeskočeno: {e}\n")
        return mesh


# Depth-map fusion keeps every depth sample in RAM: 195 photos × 3 Mpx needed
# ~26 GB; 728 drone photos at the same level ran a 62 GB machine out of memory
# three times. Budget the total depth-map pixels and step the resolution level
# down for big sets — RefineMesh still refines against the full images.
DENSE_PX_BUDGET = 300e6  # isolated workers have a 24 GiB RAM cap


def _dense_level(work: Path, n_views: int) -> int:
    from PIL import Image
    img = next((p for p in (work / "images").iterdir()), None)
    if img is None or n_views == 0:
        return 1
    with Image.open(img) as image:
        w, h = image.size
    px = h * w
    level = 1
    while n_views * px / 4 ** level > DENSE_PX_BUDGET and level < 3:
        level += 1
    return level


@postprocess_step("openmvs-max-accuracy", order=4)
async def run(ctx: ReconstructionContext) -> None:
    if not uses_openmvs(ctx.params):
        return
    from app.services.photogrammetry import openmvs_bridge as O
    from .refine import _clean_mesh

    cache_dir = ctx.models_dir / "meshroom_cache"
    out_dir = ctx.models_dir / "meshroom_out"
    out_dir.mkdir(exist_ok=True)
    # depth maps need tens of GB → big scratch disk when configured
    work = (Path(settings.work_dir) / ctx.inspection_id / "openmvs") if settings.work_dir \
        else ctx.models_dir / "openmvs"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    log = ctx.models_dir / "openmvs.log"

    await ctx.report(58, "Příprava scény pro OpenMVS (odstranění zkreslení snímků)…")
    sfm_json = await _export_sfm_json(cache_dir, log)
    exp = await asyncio.to_thread(O.export_colmap, sfm_json, work)

    await ctx.report(62, "OpenMVS: hustá rekonstrukce, síť a fotometrické zpřesnění…")
    dense_level = _dense_level(work, int(exp.get("views", 0)))
    if dense_level > 1:
        await ctx.report(62, f"OpenMVS: {exp.get('views')} fotek — hloubkové mapy ve sníženém rozlišení "
                             f"(úroveň {dense_level}) kvůli paměti; zpřesnění sítě v plném…")
    refined = await asyncio.to_thread(O.run_openmvs, work, dense_level, True, 30, False,
                                      False, 3, 1)       # refine at level 1 regardless

    if ctx.params.get("ai_normals", True):
        refined = await _ai_denoise(ctx, work, refined, log)

    await ctx.report(86, "Uzavírání děr sítě (PyMeshLab)…")
    closed = work / "mesh_closed.ply"
    stats = await asyncio.to_thread(
        _clean_mesh, str(refined), str(closed), None, False,
        bool(ctx.params.get("keep_largest_mesh", True)),
    )
    (out_dir / "refine_stats.txt").write_text(str(stats))
    await finish_texturing(ctx, work, out_dir)


async def finish_texturing(ctx: ReconstructionContext, work: Path, out_dir: Path) -> None:
    """Final stage can also resume a saved mesh after an exporter failure."""
    from app.services.photogrammetry import openmvs_bridge as O
    await ctx.report(92, "OpenMVS: texturování…")
    with open(work / "openmvs.log", "a") as lf:
        textured = await asyncio.to_thread(O.texture_mesh, work.resolve(),
                                           "scene_dense.mvs", "mesh_closed.ply", lf)

    for old in out_dir.glob("texture*"):          # stale textures of a previous run
        old.unlink(missing_ok=True)
    ctx.obj_file = await asyncio.to_thread(_to_app_frame, textured, out_dir)
    ctx.result.update({"model_path": ctx.obj_file, "engine_used": "meshroom+openmvs"})
    shutil.move(str(work / "openmvs.log"), str(ctx.models_dir / "openmvs_tools.log"))  # may cross disks
    shutil.rmtree(work, ignore_errors=True)       # depth maps + dense cloud: GBs
