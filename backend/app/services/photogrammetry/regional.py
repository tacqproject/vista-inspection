"""Sequential full-resolution regions, using the published v2 mesh/AI/bake methods.

Global SfM fixes a common coordinate frame. Only a bounded spatial subset is
resident for dense reconstruction, refinement, AI smoothing and texture baking.
No production model or source photo is ever a writable input.
"""
from __future__ import annotations
import asyncio
from array import array
import json
import os
from pathlib import Path
import shutil

import numpy as np

from .runtime import (Checkpoints, atomic_json, fingerprint, identity, require_space,
                      run_python_stage, sfm_header, sfm_items)

VERSION = 1
MAX_VIEWS = 32


def partition(points: np.ndarray, max_points=40000, max_regions=128):
    """Deterministic spatial median partition; every supplied point is covered."""
    cells = [np.arange(len(points))]
    while len(cells) < max_regions:
        candidates = [i for i, c in enumerate(cells) if len(c) > max_points]
        if not candidates:
            break
        i = max(candidates, key=lambda j: len(cells[j]))
        ids = cells.pop(i)
        axis = int(np.argmax(np.ptp(points[ids], axis=0)))
        ordered = ids[np.argsort(points[ids, axis], kind='stable')]
        mid = len(ordered) // 2
        cells.extend((ordered[:mid], ordered[mid:]))
    result = []
    for ids in cells:
        p = points[ids]
        centre = (p.min(0) + p.max(0)) / 2
        radius = max(float(np.linalg.norm(p - centre, axis=1).max()) * 1.15, 1e-6)
        result.append({'centre': centre.tolist(), 'radius': radius, 'seed_points': len(ids)})
    # Cropping uses nearest-centre cells. Grow each sphere to cover every
    # point assigned by THAT partition, including unequal-sized neighbours.
    from scipy.spatial import cKDTree
    centres = np.array([r['centre'] for r in result])
    distances, owners = cKDTree(centres).query(points)
    radius = np.array([r['radius'] for r in result])
    np.maximum.at(radius, owners, distances * 1.15)
    for r, value in zip(result, radius):
        r['radius'] = float(value)
    return sorted(result, key=lambda c: tuple(c['centre']))


def make_plan(sfm: Path, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    cp = Checkpoints(work)
    key = [identity(sfm), VERSION]
    target = work / 'plan.json'
    if cp.valid('plan', key):
        return json.loads(target.read_text())
    header = sfm_header(sfm)
    poses = {p['poseId'] for p in header['poses']}
    views = {v['viewId'] for v in header['views'] if v.get('poseId') in poses}
    xyz = array('d')
    for lm in sfm_items(sfm, 'structure'):
        if sum(o.get('observationId') in views for o in lm.get('observations', [])) >= 3:
            xyz.extend(float(x) for x in lm['X'])
    points = np.frombuffer(xyz, dtype=np.float64).reshape(-1, 3)
    if len(points) < 100:
        raise RuntimeError('Nedostatek spolehlivých SfM bodů pro prostorové části.')
    regions = partition(points)
    centres = np.array([r['centre'] for r in regions])
    radii = np.array([r['radius'] for r in regions])
    # Spool one landmark at a time; a large JSON is never loaded as a Python tree.
    streams = []
    try:
        for i, r in enumerate(regions):
            folder = work / f'C{i:03d}'
            folder.mkdir(exist_ok=True)
            r.update(index=i, folder=str(folder), points=0)
            streams.append((folder / 'landmarks.jsonl').open('w', buffering=65536))
        for lm in sfm_items(sfm, 'structure'):
            point = np.asarray(lm['X'], float)
            selected = np.flatnonzero(np.linalg.norm(centres - point, axis=1) < radii * 1.15)
            if not len(selected):
                continue
            line = json.dumps(lm, separators=(',', ':')) + '\n'
            for i in selected:
                streams[i].write(line)
                regions[i]['points'] += 1
    finally:
        for f in streams:
            f.close()
    result = {'version': VERSION, 'sfm': str(sfm), 'regions': regions,
              'reliable_points': len(points), 'max_views': MAX_VIEWS,
              'dense_level': 0, 'refine_level': 0, 'texture': 'photo-resolution',
              'ai_preset': 'normal_prior.PRESET'}
    atomic_json(target, result)
    cp.save('plan', key, [target, *[Path(r['folder']) / 'landmarks.jsonl' for r in regions]])
    return result


def prepare_region(sfm, global_work, plan_path, work, index, max_views):
    from app.services.hires import select_views
    from app.services.raw_images import is_raw
    from .openmvs_bridge import export_colmap
    plan = json.loads(plan_path.read_text())
    region = plan['regions'][index]
    work.mkdir(parents=True, exist_ok=True)
    cp = Checkpoints(work / 'prepare')
    key = [identity(sfm), identity(plan_path), index, max_views]
    sub_path = work / 'sub_sfm.json'
    header = sfm_header(sfm)
    if not cp.valid('sub-scene', key):
        data = {**header, 'structure': []}
        with (Path(region['folder']) / 'landmarks.jsonl').open() as stream:
            for line in stream:
                data['structure'].append(json.loads(line))
        selected, ranked = select_views(data, np.asarray(region['centre']), region['radius'] * 1.15,
                                        max_views=max_views, min_obs=10)
        if len(selected) < 3:
            raise RuntimeError(f'Část {index}: pouze {len(selected)} vhodných kamer.')
        poses = {p['poseId'] for p in header['poses']}
        registered = [v for v in header['views'] if v.get('poseId') in poses]
        prepared = {v['viewId']: global_work / 'images' /
                    (f'{i+1:05d}' + ('.png' if is_raw(v['path']) else '.jpg')) for i, v in enumerate(registered)}
        sub = {k: v for k, v in header.items() if k not in ('views', 'poses')}
        sub['views'] = [{**v, 'undistortedPath': str(prepared[v['viewId']])}
                        for v in header['views'] if v['viewId'] in selected]
        pose_ids = {v['poseId'] for v in sub['views']}
        sub['poses'] = [p for p in header['poses'] if p['poseId'] in pose_ids]
        sub['structure'] = []
        for lm in data['structure']:
            obs = [o for o in lm.get('observations', []) if o.get('observationId') in selected]
            if len(obs) >= 2:
                sub['structure'].append({**lm, 'observations': obs})
        atomic_json(sub_path, sub)
        cp.save('sub-scene', key, [sub_path])
    result = export_colmap(sub_path, work)
    return {**result, 'index': index, 'max_views': max_views}


def crop_region(mesh, output, plan_path, index, wide):
    from app.services.hires import crop_mesh, fill_small_holes
    plan = json.loads(plan_path.read_text())
    region = plan['regions'][index]
    centres = np.array([r['centre'] for r in plan['regions']])
    others = np.delete(centres, index, axis=0)
    radius = region['radius']
    stats = crop_mesh(mesh, output, centres[index], radius, others,
                      margin=radius * (0.12 if wide else 0.03))
    if not stats['faces']:
        raise RuntimeError(f'Část {index}: ořez sítě neobsahuje žádné plochy.')
    if not wide:
        holes = output.with_name('mesh_holes.ply')
        stats['holes'] = fill_small_holes(output, holes, max_perimeter=radius * 0.2)
        holes.replace(output)
    return stats


def bake_region(work, mesh, output, plan_path, index):
    from app.services.hires_texture import bake
    region = json.loads(plan_path.read_text())['regions'][index]
    return bake(work, mesh, output, patch_size=region['radius'] / 28)


def merge_regions(plan_path, output):
    """Stream OBJ geometry; build glTF one region at a time (no WASM 4 GB limit)."""
    from app.services.regions_gltf import write_lods
    plan = json.loads(plan_path.read_text())
    dirs = [Path(r['folder']) / 'out' for r in plan['regions']]
    output.mkdir(parents=True, exist_ok=True)
    vo = to = 0
    with (output / 'texturedMesh.obj').open('w') as fo, (output / 'texturedMesh.mtl').open('w') as fm:
        fo.write('mtllib texturedMesh.mtl\n')
        for k, d in enumerate(dirs):
            for line in (d / 'texturedMesh.mtl').read_text().splitlines():
                if line.startswith('newmtl '):
                    line = f'newmtl r{k}_' + line.split()[1]
                elif line.strip().startswith('map_Kd '):
                    src = line.strip().split(None, 1)[1]
                    dst = f'texture_r{k}_{src}'
                    dest = output / dst
                    if not dest.exists():
                        os.link(d / src, dest)
                    line = f'map_Kd {dst}'
                fm.write(line + '\n')
            nv = nt = 0
            with (d / 'texturedMesh.obj').open() as fi:
                for line in fi:
                    if line.startswith('v '):
                        nv += 1; fo.write(line)
                    elif line.startswith('vt '):
                        nt += 1; fo.write(line)
                    elif line.startswith('usemtl '):
                        fo.write(f'usemtl r{k}_{line.split()[1]}\n')
                    elif line.startswith('f '):
                        vertices = [x.split('/') for x in line.split()[1:]]
                        fo.write('f ' + ' '.join(f'{int(v[0])+vo}/{int(v[1])+to}' for v in vertices) + '\n')
            vo += nv; to += nt
    lod = write_lods(dirs, output)
    return {'model': str(lod), 'obj': str(output / 'texturedMesh.obj'), 'regions': len(dirs)}


async def run_regions(ctx, sfm, global_work, output):
    from . import openmvs_bridge as O
    root = global_work / 'regions'
    root.mkdir(exist_ok=True)
    log = ctx.models_dir / 'regions.log'
    async def cpu(stage, payload, result_path):
        return await asyncio.to_thread(run_python_stage, stage,
                                       {**payload, 'result': str(result_path)}, log)
    plan = await cpu('region-plan', {'sfm': str(sfm), 'work': str(root)}, root / 'plan-result.json')
    plan_path = root / 'plan.json'
    final_cp = Checkpoints(root / 'completed')
    for i, region in enumerate(plan['regions']):
        region_root = Path(region['folder'])
        key = [identity(plan_path), i, bool(ctx.params.get('ai_normals')), VERSION]
        if final_cp.valid(str(i), key):
            continue
        pct = 62 + int(32 * i / len(plan['regions']))
        await ctx.report(pct, f'Plné rozlišení: část {i+1}/{len(plan["regions"])} — hustá síť, AI a textury…')
        region_out = region_root / 'out'
        region_out.mkdir(exist_ok=True)
        success = False
        # Resource retries keep full pixel resolution and reduce simultaneous
        # camera context. Each attempt has its own valid scene/checkpoints.
        for max_views in (MAX_VIEWS, 24, 16):
            work = region_root / f'work-{max_views}'
            work.mkdir(exist_ok=True)
            try:
                exp = await cpu('region-prepare', {'sfm': str(sfm), 'global_work': str(global_work),
                    'plan': str(plan_path), 'work': str(work), 'region': i, 'max_views': max_views},
                    work / 'prepare-result.json')
                cp = Checkpoints(work / 'stages')
                def stage(name, inputs, files, fn):
                    if not cp.valid(name, inputs):
                        fn()
                        cp.save(name, inputs, [work / f for f in files])
                with (work / 'openmvs.log').open('a') as lf:
                    inputs = [identity(work / 'sparse' / n) for n in ('cameras.txt','images.txt','points3D.txt')]
                    await asyncio.to_thread(stage, 'interface', inputs, ['scene.mvs'], lambda: O._mvs(
                        work, 'InterfaceCOLMAP', '-i', str(work), '-o', 'scene.mvs',
                        '--image-folder', str(work / 'images') + '/', log=lf))
                    dense_key = [identity(work / 'scene.mvs'), 0, 8192, 3, 'bounded-region']
                    if not cp.valid('dense', dense_key):
                        maps = sum(p.stat().st_size for p in work.glob('*.dmap'))
                        require_space(work, max(0, int(sum(w*h for w,h in exp['dimensions'])*50)-maps)+int(6e9),
                                      f'Plné hloubkové mapy části {i+1}')
                    await asyncio.to_thread(stage, 'dense', dense_key, ['scene_dense.mvs','scene_dense.ply'], lambda: O._densify(
                        work, lf, '--resolution-level', '0', '--max-resolution', '8192', '--number-views-fuse', '3',
                        '--estimate-roi', '1', '--tower-mode', '0', '--max-threads', '2'))
                    # Maps are replaceable only after fusion is checkpointed.
                    for p in work.glob('*.dmap'):
                        p.unlink()
                    n = O._ply_vertex_count(work / 'scene_dense.ply')
                    distance = 2.5 * max(1, (n / O.MESH_POINTS)**0.5)
                    await asyncio.to_thread(stage, 'mesh', [identity(work/'scene_dense.mvs'),distance], ['mesh_full.ply'], lambda: O._mvs(
                        work, 'ReconstructMesh', 'scene_dense.mvs', '--close-holes','30',
                        '--min-point-distance',f'{distance:.2f}','--max-threads','4','-o','mesh_full.ply',log=lf))
                    crop_key = [identity(work/'mesh_full.ply'),identity(plan_path),i]
                    if not cp.valid('crop', crop_key):
                        await cpu('region-crop', {'mesh':str(work/'mesh_full.ply'),'output':str(work/'mesh.ply'),
                            'plan':str(plan_path),'region':i,'wide':True}, work/'crop-result.json')
                        cp.save('crop',crop_key,[work/'mesh.ply'])
                    await asyncio.to_thread(stage, 'refine', [identity(work/'mesh.ply'),0,4], ['mesh_refined.ply'], lambda: O._mvs(
                        work,'RefineMesh','scene_dense.mvs','--mesh-file','mesh.ply','--resolution-level','0',
                        '--max-views','4','--max-threads','4','--close-holes','30','--decimate','0','-o','mesh_refined.ply',log=lf))
                mesh = work/'mesh_refined.ply'
                if ctx.params.get('ai_normals'):
                    ai_key = [identity(mesh),identity(work/'sparse/images.txt')]
                    if not cp.valid('ai',ai_key):
                        with (work/'openmvs.log').open('a') as lf:
                            await asyncio.to_thread(O.predict_normals,work,work/'normals',1344,lf)
                        await cpu('normal-prior',{'mesh':str(mesh),'output':str(work/'mesh_ai.ply'),
                            'work':str(work),'normals':str(work/'normals')},work/'ai-result.json')
                        cp.save('ai',ai_key,[work/'mesh_ai.ply'])
                    mesh=work/'mesh_ai.ply'
                cell_key=[identity(mesh),identity(plan_path),i]
                if not cp.valid('cell',cell_key):
                    await cpu('region-crop',{'mesh':str(mesh),'output':str(work/'mesh_final.ply'),
                        'plan':str(plan_path),'region':i,'wide':False},work/'cell-result.json')
                    cp.save('cell',cell_key,[work/'mesh_final.ply'])
                bake=await cpu('region-bake',{'work':str(work),'mesh':str(work/'mesh_final.ply'),
                    'output':str(region_out),'plan':str(plan_path),'region':i},work/'bake-result.json')
                atomic_json(region_root/'result.json',{'views':exp['views'],'dense_level':0,'refine_level':0,
                    'ai_normals':bool(ctx.params.get('ai_normals')),'bake':bake,'work':str(work)})
                outputs=[region_out/'texturedMesh.obj',region_out/'texturedMesh.mtl',*sorted(region_out.glob('texture_*.jpg'))]
                final_cp.save(str(i),key,outputs)
                # Keep SfM, final/refined/AI meshes, textures and diagnostics.
                # Only reproducible dense intermediates of this NEW RAW job go.
                for attempt in region_root.glob('work-*'):
                    for name in ('scene_dense.ply','scene_dense.mvs','mesh_full.ply'):
                        (attempt/name).unlink(missing_ok=True)
                    for p in attempt.glob('*.dmap*'):
                        p.unlink()
                    shutil.rmtree(attempt/'normals',ignore_errors=True)
                success=True
                break
            except Exception as exc:
                with log.open('a') as lf:
                    lf.write(f'\nČást {i+1}, {max_views} kamer: {exc}\n')
                # Retry only resource-related failures, never data/geometry errors.
                resource_error=any(x in str(exc).lower() for x in (
                    'code 137', 'kód 137', 'out of memory', 'out_of_memory',
                    'memory allocation failed', 'nedostatek místa', 'potřeba přibližně'))
                if not resource_error or max_views==16:
                    raise
                atomic_json(region_root/f'retry-{max_views}.json',{'error':str(exc),'next_views':24 if max_views==32 else 16})
                # An unsuccessful dense attempt must not crowd out its retry.
                for p in work.glob('*.dmap*'):
                    p.unlink()
        if not success:
            raise RuntimeError(f'Nedokončená část {i+1}')
    await ctx.report(96,'Spojení všech částí do jednoho modelu a webových úrovní…')
    result=await cpu('region-merge',{'plan':str(plan_path),'output':str(output)},root/'merge-result.json')
    ctx.obj_file=Path(result['obj'])
    ctx.result.update(model_path=Path(result['model']),engine_used='meshroom+openmvs-regions-ai')
    ctx.params['_regional_plan']=str(plan_path)


def region_tiles(plan_path: Path, output: Path):
    """Build small independent tile trees and reference them from one root.

    Avoid loading the whole bridge (tens of millions of triangles) into one
    tiler's simplifier. All child tiles retain the common SfM-derived frame.
    """
    from app.services import regions_gltf, tiles
    plan = json.loads(plan_path.read_text())
    target = tiles.tiles_dir_for(output / 'model_lod0.gltf')
    target.mkdir(exist_ok=True)
    children = []
    for i, region in enumerate(plan['regions']):
        src = Path(region['folder']) / 'out'
        region_out = target / f'C{i:03d}'
        region_out.mkdir(exist_ok=True)
        cp = Checkpoints(region_out)
        key = [identity(src/'texturedMesh.obj'),identity(src/'texturedMesh.mtl')]
        lod = region_out / 'model_lod0.gltf'
        if not cp.valid('lod',key):
            regions_gltf.write_lods([src],region_out)
            cp.save('lod',key,[lod,lod.with_suffix('.bin')])
        tile = tiles.tiles_dir_for(lod) / 'tileset.json'
        if not cp.valid('tiles',key):
            tiles.build_tiles(lod,workers=1)
            assets = [p for p in tile.parent.iterdir() if p.is_file()]
            cp.save('tiles',key,assets)
        data = json.loads(tile.read_text())
        children.append({'boundingVolume':data['root']['boundingVolume'],
                         'geometricError':data['geometricError'],
                         'content':{'uri':str(tile.relative_to(target))}})
    lo,hi=map(np.array,tiles._current_bounds(output/'model_lod0.gltf'))
    centre=(lo+hi)/2;half=np.maximum((hi-lo)/2,1e-6)
    error=float(np.linalg.norm(hi-lo))
    result={'asset':{'version':'1.1','generator':'VISTA RAW regions','gltfUpAxis':'Z'},
            'geometricError':error,'root':{'boundingVolume':{'box':[*centre.tolist(),half[0],0,0,0,half[1],0,0,0,half[2]]},
            'geometricError':error,'refine':'REPLACE','children':children},
            'extras':{'bounds':{'min':lo.tolist(),'max':hi.tolist()},'regions':len(children)}}
    atomic_json(target/'tileset.json',result)
    return {'regions':len(children),'tileset':str(target/'tileset.json')}
