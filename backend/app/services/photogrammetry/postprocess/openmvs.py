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
from ..runtime import (Checkpoints, identity, fingerprint, current_sfm, run_python_stage,
                       sfm_header, require_space, atomic_json)

CONTAINER_CACHE = "/tmp/MeshroomCache"
EMPTY_COLOR = 0x808080


def uses_openmvs(params: dict) -> bool:
    return params.get("quality") == "max"


async def _export_sfm_json(cache_dir: Path, log: Path) -> Path:
    """Export the graph's completed SfM once; never pick another run's frame."""
    folder = current_sfm(cache_dir)
    inputs = identity(folder / 'sfm.abc')
    cp = Checkpoints(cache_dir / 'exports')
    out = cache_dir / 'sfm.json'
    if cp.valid('sfm-json', inputs):
        return out
    if os.environ.get('VISTA_JOB_SOCKET'):
        # Native convertSfMFormat constructs a second, multi-GB JSON tree.
        # The RAW worker keeps native SfM and serializes small batches instead.
        await asyncio.to_thread(run_python_stage, 'export-sfm', {
            'sfm': str(folder / 'sfm.abc'), 'output': str(out),
            'result': str(cache_dir / 'sfm-export-result.json')}, log)
        cp.save('sfm-json', inputs, [out])
        return out
    temp = cache_dir / 'sfm-pending.json'
    temp.unlink(missing_ok=True)
    rel = (folder / 'sfm.abc').relative_to(cache_dir)
    env = {**os.environ, 'VISTA_MESHROOM_CACHE': str(cache_dir),
           'VISTA_MESHROOM_BIN': 'aliceVision_convertSfMFormat'}
    with open(log, 'a') as fh:
        proc = await asyncio.create_subprocess_exec(
            settings.meshroom_path, '--input', f'{CONTAINER_CACHE}/{rel}',
            '--output', f'{CONTAINER_CACHE}/{temp.name}', '--describerTypes', 'dspsift',
            env=env, stdout=fh, stderr=asyncio.subprocess.STDOUT)
        rc = await proc.wait()
    if rc or not temp.is_file() or temp.stat().st_size == 0:
        raise RuntimeError(f'Export SfM selhal (kód {rc}). Detaily: {log}')
    temp.replace(out)
    cp.save('sfm-json', inputs, [out])
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
    await ctx.report(80, 'AI: odhad normál povrchu z fotek (DSINE)…')
    try:
        with open(log, 'a') as fh:
            await asyncio.to_thread(O.predict_normals, work, work / 'normals', 1344, fh)
        await ctx.report(83, 'AI: odšumění povrchu podle normál…')
        out = work / 'mesh_ai.ply'
        await asyncio.to_thread(run_python_stage, 'normal-prior', {
            'mesh': str(mesh), 'output': str(out), 'work': str(work),
            'normals': str(work / 'normals'), 'result': str(work / 'normal-result.json')}, log)
        return out
    except Exception as exc:
        with open(log, 'a') as fh:
            fh.write(f'\nAI odšumění přeskočeno: {exc}\n')
        atomic_json(work / 'ai-warning.json', {'warning': str(exc)})
        await ctx.report(84, 'AI odšumění selhalo; pokračuji s fotometricky zpřesněným modelem.')
        return mesh


# Depth-map fusion keeps every depth sample in RAM: 195 photos × 3 Mpx needed
# ~26 GB; 728 drone photos at the same level ran a 62 GB machine out of memory
# three times. Budget the total depth-map pixels and step the resolution level
# down for big sets — RefineMesh still refines against the full images.
DENSE_PX_BUDGET = 300e6  # isolated workers have a 24 GiB RAM cap


def dense_pixels(dimensions, level):
    # OpenMVS scales the larger dimension, clamped by min/max resolution.
    total = 0
    for w, h in dimensions:
        side = min(max(w, h), max(640, min(2560, max(w, h) / 2 ** level)))
        total += w * h * (side / max(w, h)) ** 2
    return total


def _dense_level(work: Path, n_views: int) -> int:
    from PIL import Image
    dimensions = []
    for p in (work / 'images').iterdir():
        with Image.open(p) as im:
            dimensions.append(im.size)
    level = 1
    while dense_pixels(dimensions, level) > DENSE_PX_BUDGET and level < 6:
        level += 1
    if dense_pixels(dimensions, level) > DENSE_PX_BUDGET:
        raise RuntimeError('Scéna přesahuje paměťový rozpočet i při minimálním rozlišení map; je nutné ji rozdělit.')
    return level


@postprocess_step("openmvs-max-accuracy", order=4)
async def run(ctx: ReconstructionContext) -> None:
    if not uses_openmvs(ctx.params):
        return
    from app.services.photogrammetry import openmvs_bridge as O
    cache_dir = ctx.models_dir / 'meshroom_cache'
    out_dir = ctx.models_dir / 'meshroom_out'
    out_dir.mkdir(exist_ok=True)
    log = ctx.models_dir / 'openmvs.log'
    await ctx.report(58, 'Export SfM; hotové kroky se při opakování použijí znovu…')
    sfm_json = await _export_sfm_json(cache_dir, log)
    # A changed SfM frame gets a separate work directory. Retrying the same
    # frame retains undistorted images, maps, meshes and their checkpoints.
    signature = fingerprint(identity(sfm_json))[:16]
    base = Path(settings.work_dir) / ctx.inspection_id if settings.work_dir else ctx.models_dir
    work = base / 'openmvs' / signature
    work.mkdir(parents=True, exist_ok=True)
    atomic_json(ctx.models_dir / 'openmvs-work.json', {'work': str(work)})
    exp = await asyncio.to_thread(run_python_stage, 'export-colmap', {
        'sfm': str(sfm_json), 'work': str(work), 'result': str(work / 'export-result.json')}, log)
    if ctx.params.get('regional'):
        from ..regional import run_regions
        await run_regions(ctx, sfm_json, work, out_dir)
        return
    dense_level = _dense_level(work, int(exp['views']))
    atomic_json(ctx.models_dir / 'resource-plan.json', {
        'registered_views': exp['views'], 'dense_level': dense_level,
        'depth_pixels': int(dense_pixels(exp['dimensions'], dense_level)),
        'worker_ram_gib': 24, 'threads': 8, 'disk_reserve_gb': settings.disk_reserve_gb,
        'refine_level': dense_level, 'work': str(work)})
    await ctx.report(62, f"OpenMVS: {exp['views']} kamer, hloubkové mapy a zpřesnění na úrovni {dense_level}; textury z plných snímků…")
    refined = await asyncio.to_thread(O.run_openmvs, work, dense_level, True, 30, False,
                                      False, 3, dense_level)
    if ctx.params.get('ai_normals', False):
        refined = await _ai_denoise(ctx, work, refined, log)
    await ctx.report(86, 'Čištění sítě v samostatném paměťově omezeném procesu…')
    closed = work / 'mesh_closed.ply'
    cp = Checkpoints(work / 'postprocess')
    clean_inputs = [identity(refined), bool(ctx.params.get('keep_largest_mesh', True))]
    if not cp.valid('clean', clean_inputs):
        stats = await asyncio.to_thread(run_python_stage, 'clean-mesh', {
            'mesh': str(refined), 'output': str(closed), 'keep_largest': clean_inputs[1],
            'result': str(work / 'clean-result.json')}, log)
        (out_dir / 'refine_stats.txt').write_text(str(stats))
        cp.save('clean', clean_inputs, [closed])
    await finish_texturing(ctx, work, out_dir)


async def finish_texturing(ctx: ReconstructionContext, work: Path, out_dir: Path) -> None:
    from app.services.photogrammetry import openmvs_bridge as O
    await ctx.report(92, 'OpenMVS: texturování…')
    cp = Checkpoints(work / 'postprocess')
    inputs = [identity(work / 'scene_dense.mvs'), identity(work / 'mesh_closed.ply'), O.OPENMVS_IMAGE]
    textured = work / 'textured.obj'
    if not cp.valid('texture', inputs):
        with open(work / 'openmvs.log', 'a') as lf:
            textured = await asyncio.to_thread(O.texture_mesh, work.resolve(),
                                               'scene_dense.mvs', 'mesh_closed.ply', lf)
        # Include every referenced texture so incomplete exports cannot resume.
        material = textured.with_suffix('.mtl')
        outputs = [textured, material]
        for line in material.read_text().splitlines():
            if line.strip().startswith('map_Kd '):
                outputs.append(work / line.strip().split(None, 1)[1])
        cp.save('texture', inputs, outputs)
    ctx.obj_file = await asyncio.to_thread(_to_app_frame, textured, out_dir)
    ctx.result.update({'model_path': ctx.obj_file, 'engine_used': 'meshroom+openmvs'})
    shutil.copy2(work / 'openmvs.log', ctx.models_dir / 'openmvs_tools.log')
    # Retain all successful stages until the user explicitly cleans them up.
