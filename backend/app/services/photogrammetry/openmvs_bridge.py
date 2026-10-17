"""
AliceVision SfM → OpenMVS (densify → mesh → photometric refine → texture).

OpenMVS' RefineMesh moves every vertex to maximise photo-consistency across the
source images — measurably more accurate surfaces than depth-map fusion alone —
and its meshing/texturing close small gaps well. The bundled OpenMVS only
imports COLMAP scenes with PINHOLE cameras and undistorted images, so we write
that scene ourselves from AliceVision's sfm.json (with landmarks):

  • images undistorted with OpenCV — AliceVision "radial3" (x_d = x_u·(1+k1r²+k2r⁴+k3r⁶)
    on normalized coordinates) is exactly OpenCV's radial model with p1=p2=0;
  • K: principal point = image centre + AliceVision's offset;
  • pose: AliceVision stores R (row-major) with X_cam = Rᵀ(X−C) → COLMAP
    world-to-camera rotation Rᵀ and t = −Rᵀ·C (both use CV axes: +Z forward);
  • landmarks + their (undistorted) 2D observations → points3D / POINTS2D, which
    OpenMVS uses to pick neighbouring views and depth ranges.

Output meshes are in the SfM frame (AliceVision OBJs are in the y,z-negated
"mesh" frame — `to_mesh_frame()` converts).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

def _default_image() -> str:
    """Prefer the CUDA build (GPU PatchMatch densify + RefineMesh, ~10× faster,
    makes full-resolution reconstruction practical); fall back to the CPU build."""
    env = os.environ.get("VISTA_OPENMVS_IMAGE")
    if env:
        return env
    try:
        r = subprocess.run(["docker", "image", "inspect", "vista-openmvs:2.3-cuda"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return "vista-openmvs:2.3-cuda"
    except OSError:
        pass
    return "vista-openmvs:2.3"


OPENMVS_IMAGE = _default_image()


def _quat_from_R(R: np.ndarray) -> tuple[float, float, float, float]:
    """Rotation matrix → (qw, qx, qy, qz), numerically stable branch."""
    t = np.trace(R)
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        return (0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s)
    i = int(np.argmax(np.diag(R)))
    if i == 0:
        s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        return ((R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s)
    if i == 1:
        s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        return ((R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s)
    s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
    return ((R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s)


def export_colmap(sfm_json: Path, out_dir: Path, max_side: int = 0, quality: int = 95,
                  interp: int | None = None) -> dict:
    """Write a COLMAP (TXT, PINHOLE, undistorted) scene into out_dir/{images,sparse}.
    max_side > 0 downsizes the undistorted images (intrinsics scaled to match).
    quality / interp: JPEG quality and remap interpolation of the undistortion
    (texturing wants ~98 + Lanczos so the photo's fine detail survives)."""
    import cv2

    from .runtime import sfm_header, sfm_items, Checkpoints, identity
    # OpenCV 5 merged the iterative overload into undistortPoints.
    undistort_points = getattr(cv2, 'undistortPointsIter', cv2.undistortPoints)
    undistort_criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 1e-12)
    data = sfm_header(Path(sfm_json))
    cv2.setNumThreads(2)
    out_dir = Path(out_dir)
    img_dir, sp_dir = out_dir / "images", out_dir / "sparse"
    img_dir.mkdir(parents=True, exist_ok=True); sp_dir.mkdir(parents=True, exist_ok=True)

    intr = {i["intrinsicId"]: i for i in data.get("intrinsics", [])}
    poses = {p["poseId"]: p["pose"]["transform"] for p in data.get("poses", [])}

    views = []                                  # (colmap id, view, K, dist, R_w2c, t, scale, name)
    for v in data.get("views", []):
        pose, it = poses.get(v.get("poseId")), intr.get(v.get("intrinsicId"))
        if not pose or not it:
            continue
        w, h = int(it["width"]), int(it["height"])
        pxf = it.get("pxFocalLength")
        if isinstance(pxf, list):
            fx, fy = float(pxf[0]), float(pxf[1])
        elif pxf is not None:
            fx = fy = float(pxf)
        else:
            fx = float(it["focalLength"]) / float(it.get("sensorWidth", 36.0)) * w
            fy = fx / float(it.get("pixelRatio", 1.0))
        pp = [float(x) for x in it.get("principalPoint", [0, 0])]
        K = np.array([[fx, 0, w / 2 + pp[0]], [0, fy, h / 2 + pp[1]], [0, 0, 1]], float)
        dp = [float(x) for x in it.get("distortionParams", [])[:3]] + [0.0] * 3
        dist = np.array([dp[0], dp[1], 0.0, 0.0, dp[2]], float)
        M = np.array([float(x) for x in pose["rotation"]], float).reshape(3, 3)
        C = np.array([float(x) for x in pose["center"]], float)
        R = M.T                                  # world → camera
        t = -R @ C
        s = min(1.0, max_side / max(w, h)) if max_side else 1.0
        from app.services.raw_images import is_raw
        name = f"{len(views) + 1:05d}" + ('.png' if is_raw(v['path']) else '.jpg')
        views.append({"id": len(views) + 1, "view": v, "K": K, "dist": dist, "R": R, "t": t,
                      "w": w, "h": h, "s": s, "name": name})
    by_view = {vw["view"]["viewId"]: vw for vw in views}
    cp = Checkpoints(out_dir)
    inputs = {'version': 3, 'sfm': identity(Path(sfm_json)), 'max_side': max_side,
              'quality': quality, 'interp': interp,
              'sources': [identity(Path(vw['view']['path'])) for vw in views]}
    if cp.valid('export', inputs):
        return cp.data['export']['summary']
    image_cp = Checkpoints(out_dir / 'image-checkpoints')
    def image_inputs(vw):
        return [inputs['sfm'], identity(Path(vw['view']['path'])), max_side, quality, interp]
    from .runtime import require_space
    missing_bytes = sum(int(v['w'] * v['h'] * v['s'] ** 2 * 2.2) for v in views
                        if not image_cp.valid(v['name'], image_inputs(v)))
    require_space(out_dir, missing_bytes, 'Rozvinutí zbývajících snímků')

    def undistort(vw):
        from app.services.raw_images import read_bgr
        image_key = image_inputs(vw)
        target = img_dir / vw['name']
        if image_cp.valid(vw['name'], image_key):
            return
        prepared = vw['view'].get('undistortedPath')
        if prepared:
            # Region scenes use the exact same intrinsics and full-size PNGs.
            # Hard links avoid decoding/storing overlapping photos repeatedly.
            target.unlink(missing_ok=True)
            os.link(prepared, target)
            image_cp.save(vw['name'], image_key, [target])
            return
        img = read_bgr(vw["view"]["path"])
        if img is None:
            raise FileNotFoundError(vw["view"]["path"])
        if img.shape[:2] != (vw['h'], vw['w']):
            raise ValueError(f"Source dimensions {img.shape[:2]} do not match SfM {(vw['h'], vw['w'])}")
        if interp is None:
            und = cv2.undistort(img, vw["K"], vw["dist"], None, vw["K"])
        else:
            mx, my = cv2.initUndistortRectifyMap(vw["K"], vw["dist"], None, vw["K"],
                                                 (img.shape[1], img.shape[0]), cv2.CV_32FC1)
            und = cv2.remap(img, mx, my, interp, borderMode=cv2.BORDER_CONSTANT)
        if vw["s"] < 1.0:
            und = cv2.resize(und, (round(vw["w"] * vw["s"]), round(vw["h"] * vw["s"])),
                             interpolation=cv2.INTER_AREA)
        options = [cv2.IMWRITE_PNG_COMPRESSION, 2] if vw['name'].endswith('.png') else [cv2.IMWRITE_JPEG_QUALITY, quality]
        from app.config import settings
        if shutil.disk_usage(out_dir).free < settings.disk_reserve_gb * 1e9 + und.nbytes:
            raise RuntimeError('Nedostatek místa pro rozvinuté RAW snímky; uvolněte místo na SSD.')
        temp = target.with_name('pending-' + target.name)
        if not cv2.imwrite(str(temp), und, options):
            raise RuntimeError(f"Cannot write OpenMVS image {vw['name']}")
        temp.replace(target)
        image_cp.save(vw['name'], image_key, [target])

    # A single decoder also bounds 50 MP RAWs; each image is resumable.
    for vw in views:
        undistort(vw)

    # Stream millions of landmarks. Spool 2D observations per image on SSD,
    # keeping only one track and a bounded number of file buffers in RAM.
    from collections import OrderedDict
    spool = out_dir / 'observation-spool'
    spool.mkdir(exist_ok=True)
    for old in spool.glob('*.txt'):
        old.unlink()
    handles = OrderedDict()
    counts = {vw['id']: 0 for vw in views}
    n_points = 0
    points_tmp = sp_dir / 'points3D.pending'
    def append(vid, value):
        if vid not in handles:
            if len(handles) >= 32:
                _, fh = handles.popitem(last=False)
                fh.close()
            handles[vid] = (spool / f'{vid}.txt').open('a', buffering=65536)
        handles.move_to_end(vid)
        handles[vid].write(value)
    try:
        with points_tmp.open('w', buffering=1048576) as fp:
            for landmark in sfm_items(Path(sfm_json), 'structure'):
                observations = [(by_view[ob['observationId']], ob) for ob in landmark.get('observations', [])
                                if ob.get('observationId') in by_view]
                if len({vw['id'] for vw, _ in observations}) < 2:
                    continue
                n_points += 1
                track = []
                for vw, ob in observations:
                    xy = np.array([[[float(ob['x'][0]), float(ob['x'][1])]]], float)
                    # The five-iteration default leaves up to 0.24 px of error
                    # at the Corvallis DNG corners. Match the image calibration
                    # to subpixel precision without resampling the image again.
                    u = undistort_points(
                        xy, vw['K'], vw['dist'], R=None, P=vw['K'], criteria=undistort_criteria,
                    )[0, 0] * vw['s']
                    vid = vw['id']
                    track.append(f'{vid} {counts[vid]}')
                    append(vid, f'{u[0]} {u[1]} {n_points} ')
                    counts[vid] += 1
                xyz = ' '.join(str(float(x)) for x in landmark['X'])
                rgb = ' '.join(str(int(float(c))) for c in landmark.get('color', [128, 128, 128])[:3])
                fp.write(f"{n_points} {xyz} {rgb} 0 " + ' '.join(track) + '\n')
    finally:
        for fh in handles.values():
            fh.close()

    cameras_tmp, images_tmp = sp_dir / 'cameras.pending', sp_dir / 'images.pending'
    with cameras_tmp.open('w') as fc, images_tmp.open('w') as fi:
        for vw in views:
            K, scale = vw['K'], vw['s']
            fc.write(f"{vw['id']} PINHOLE {round(vw['w'] * scale)} {round(vw['h'] * scale)} "
                     f"{K[0, 0] * scale} {K[1, 1] * scale} {K[0, 2] * scale} {K[1, 2] * scale}\n")
            q = _quat_from_R(vw['R'])
            fi.write(f"{vw['id']} {q[0]} {q[1]} {q[2]} {q[3]} {vw['t'][0]} {vw['t'][1]} {vw['t'][2]} "
                     f"{vw['id']} {vw['name']}\n")
            obs = spool / f"{vw['id']}.txt"
            if obs.exists():
                with obs.open() as fh:
                    shutil.copyfileobj(fh, fi, length=1048576)
            fi.write('\n')
    # COLMAP permits an empty structure; keep a nonempty comment in that case.
    if n_points == 0:
        points_tmp.write_text('# No landmarks\n')
    for pending, final in ((points_tmp, 'points3D.txt'), (cameras_tmp, 'cameras.txt'), (images_tmp, 'images.txt')):
        pending.replace(sp_dir / final)
    summary = {'views': len(views), 'points': n_points,
               'dimensions': [[round(v['w'] * v['s']), round(v['h'] * v['s'])] for v in views]}
    outputs = [sp_dir / x for x in ('cameras.txt', 'images.txt', 'points3D.txt')]
    outputs += [img_dir / v['name'] for v in views]
    cp.save('export', inputs, outputs, summary=summary)
    shutil.rmtree(spool)
    return summary


# One VRAM-heavy job at a time: two concurrent CUDA jobs (two reconstructions,
# or a reconstruction + another GPU tool) run the 12 GB card out of memory and
# fail half-way (seen: PatchMatch left *.geo.dmap.tmp and aborted).
GPU_LOCK = Path(os.environ.get("VISTA_GPU_LOCK", "/tmp/vista-gpu.lock"))
GPU_TOOLS = {"DensifyPointCloud"}


class gpu_lock:
    def __enter__(self):
        import fcntl
        self.f = open(GPU_LOCK, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


class OpenMVSError(RuntimeError):
    def __init__(self, tool: str, code: int, detail: str = ''):
        self.code = code
        super().__init__(f"OpenMVS {tool} selhal (kód {code})" + (f': {detail}' if detail else ''))


def _mvs(work: Path, *args: str, log) -> None:
    """Run one OpenMVS tool in the container (work dir mounted at the same path)."""
    uid = f"{os.getuid()}:{os.getgid()}"
    gpu = ["--gpus", "all"] if "cuda" in OPENMVS_IMAGE else []
    cmd = ["docker", "run", "--rm", *gpu, "-u", uid, "-v", f"{work}:{work}", "-w", str(work),
           OPENMVS_IMAGE, *args, *([] if '--max-threads' in args else ['--max-threads', '8'])]
    log.write(f"\n$ {' '.join(args)}\n"); log.flush()
    if gpu and args[0] in GPU_TOOLS:
        with gpu_lock():
            r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    else:
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        log.flush()
        detail = ''
        try:
            with open(log.name, 'rb') as fh:
                fh.seek(max(0, os.fstat(fh.fileno()).st_size - 16384))
                tail = fh.read().decode(errors='replace').replace('\r', '\n').splitlines()
            detail = ' | '.join(line for line in tail if any(k in line.lower() for k in
                ('error', 'memory', 'bad_alloc', 'cuda', 'terminate', 'assertion')))[-900:]
        except (OSError, TypeError):
            pass
        raise OpenMVSError(args[0], r.returncode, detail)


def _textured_ply_to_obj(ply: Path, obj: Path) -> None:
    """Preserve geometry and per-corner UVs while avoiding OpenMVS 2.3 SaveOBJ.

    Its single-atlas OBJ exporter can index an empty faceTexindices vector:
    https://github.com/cdcseacave/openMVS/issues/1132
    PyMeshLab reads PLY TextureFile/texcoord and exports the same textured mesh.
    """
    import pymeshlab
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(ply))
    mesh = ms.current_mesh()
    if not mesh.has_wedge_tex_coord() or mesh.texture_number() == 0:
        raise RuntimeError('OpenMVS PLY neobsahuje UV souřadnice nebo textury.')
    ms.save_current_mesh(str(obj), save_wedge_texcoord=True)
    # MeshLab writes name.obj.mtl; the rest of VISTA uses name.mtl.
    material = Path(str(obj) + '.mtl')
    if not material.exists():
        raise RuntimeError('Převod texturovaného PLY nevytvořil materiál.')
    material.replace(obj.with_suffix('.mtl'))
    temp = obj.with_suffix('.obj.tmp')
    try:
        with obj.open() as src, temp.open('w') as dst:
            for line in src:
                dst.write(f'mtllib {obj.with_suffix(".mtl").name}\n' if line.startswith('mtllib ') else line)
        temp.replace(obj)
    finally:
        temp.unlink(missing_ok=True)


def texture_mesh(work: Path, scene: str, mesh: str, log) -> Path:
    """Native OBJ first; retry only the known exporter crash via textured PLY."""
    options = ('TextureMesh', scene, '--mesh-file', mesh, '--empty-color', str(0x808080))
    obj = work / 'textured.obj'
    try:
        _mvs(work, *options, '--export-type', 'obj', '-o', obj.name, log=log)
    except OpenMVSError as exc:
        if exc.code != 139:
            raise
        log.write('\nExport OBJ spadl; opakuji texturování do PLY a převádím se zachováním UV.\n')
        log.flush()
        ply = work / 'textured.ply'
        _mvs(work, *options, '--export-type', 'ply', '-o', ply.name, log=log)
        from .runtime import run_python_stage
        run_python_stage('texture-ply', {'mesh': str(ply), 'output': str(obj),
                         'result': str(work / 'texture-convert-result.json')}, work / 'texture-convert.log')
    if not obj.is_file():
        raise RuntimeError('OpenMVS nevytvořil texturovaný model.')
    return obj


def _densify(work: Path, log, *args: str, attempts: int = 3) -> None:
    """Retry only the known incomplete geometric-map race, retaining good maps."""
    for k in range(attempts):
        offset = log.tell()
        try:
            _mvs(work, 'DensifyPointCloud', 'scene.mvs', *args, log=log)
            return
        except OpenMVSError as exc:
            log.flush()
            with (work / 'openmvs.log').open() as reader:
                reader.seek(offset)
                tail = reader.read()[-65536:].lower()
            retryable = exc.code == 1 and '.dmap' in tail and ('opening file' in tail or 'failed to load' in tail)
            if not retryable or k == attempts - 1:
                raise
            for tmp in work.glob('*.dmap.tmp'):
                tmp.unlink(missing_ok=True)
            log.write(f'\nPokračování hloubkových map po nedokončeném zápisu ({k + 2}/{attempts})\n')
            log.flush()


MESH_POINTS = 6e6


def _ply_vertex_count(ply: Path) -> int:
    try:
        with open(ply, "rb") as f:
            for _ in range(40):
                line = f.readline()
                if line.startswith(b"element vertex"):
                    return int(line.split()[2])
                if line.startswith(b"end_header"):
                    break
    except OSError:
        pass
    return 0


def run_openmvs(work: Path, resolution_level: int = 1, refine: bool = True,
                close_holes: int = 30, texture: bool = True, skip_dense: bool = False,
                fuse_views: int = 3, refine_level: int | None = None,
                refine_decimate: float = 0.0, refine_args: tuple = (),
                input_mesh: str | None = None) -> Path:
    """COLMAP scene in `work` → (refined, textured) mesh. OpenMVS 2.x writes meshes
    as .ply next to the scene, so later steps get scene + `--mesh-file` explicitly.

    fuse_views      min. views agreeing on a depth to keep a point (OpenMVS default
                    3; 2 adds noise — the "hairy" surfaces)
    refine_level    image resolution level of RefineMesh (0 = full res)
    refine_decimate RefineMesh pre-decimation (0 = auto, 1 = none)
    input_mesh      refine/texture a mesh made elsewhere (e.g. AliceVision) instead
                    of meshing the dense cloud (skip_dense must then be True)
    Returns the final mesh path (.obj when textured, else .ply)."""
    from .runtime import Checkpoints, identity, require_space
    work = Path(work).resolve()
    cp = Checkpoints(work / 'stages')
    scene = 'scene.mvs'
    def cached(name, inputs, outputs, action):
        if cp.valid(name, inputs):
            log.write(f'\nPokračování: {name} už hotovo.\n'); log.flush()
            return
        action()
        cp.save(name, inputs, [work / p for p in outputs])
    with open(work / 'openmvs.log', 'a') as log:
        sparse_inputs = [identity(work / 'sparse' / n) for n in ('cameras.txt', 'images.txt', 'points3D.txt')]
        cached('interface', sparse_inputs, ['scene.mvs'], lambda: _mvs(
            work, 'InterfaceCOLMAP', '-i', str(work), '-o', 'scene.mvs',
            '--image-folder', str(work / 'images') + '/', log=log))
        if not skip_dense:
            dense_inputs = [identity(work / scene), resolution_level, fuse_views, 640, OPENMVS_IMAGE]
            # Check remaining demand before starting, not after hours of maps.
            if not cp.valid('dense', dense_inputs):
                from .postprocess.openmvs import dense_pixels
                from PIL import Image
                dims = []
                for p in sorted((work / 'images').iterdir()):
                    with Image.open(p) as im:
                        dims.append(im.size)
                existing = sum(p.stat().st_size for p in work.glob('*.dmap'))
                require_space(work, max(0, int(dense_pixels(dims, resolution_level) * 50) - existing)
                              + int(8e9), 'Hloubkové mapy a husté mračno')
            cached('dense', dense_inputs, ['scene_dense.mvs', 'scene_dense.ply'], lambda: _densify(
                work, log, '--resolution-level', str(resolution_level),
                '--min-resolution', '640', '--max-resolution', '2560',
                '--number-views-fuse', str(fuse_views)))
            n_pts = _ply_vertex_count(work / 'scene_dense.ply')
            if not n_pts:
                raise RuntimeError('Husté mračno je prázdné.')
            dist = 2.5 * max(1.0, (n_pts / MESH_POINTS) ** 0.5)
            mesh_inputs = [identity(work / 'scene_dense.mvs'), identity(work / 'scene_dense.ply'),
                           close_holes, dist, OPENMVS_IMAGE]
            cached('mesh', mesh_inputs, ['mesh.ply'], lambda: _mvs(
                work, 'ReconstructMesh', 'scene_dense.mvs', '--close-holes', str(close_holes),
                '--min-point-distance', f'{dist:.2f}', '-o', 'mesh.ply', log=log))
        if (work / 'scene_dense.mvs').exists():
            scene = 'scene_dense.mvs'
        mesh = input_mesh or 'mesh.ply'
        if refine:
            lvl = resolution_level if refine_level is None else refine_level
            refine_inputs = [identity(work / scene), identity(work / mesh), lvl, close_holes,
                             refine_decimate, list(refine_args), OPENMVS_IMAGE]
            if not cp.valid('refine', refine_inputs):
                # Resolution is explicit and stable; errors do not silently
                # downgrade geometry or repeat the same failing OOM workload.
                _mvs(work, 'RefineMesh', scene, '--mesh-file', mesh,
                     '--resolution-level', str(lvl), '--close-holes', str(close_holes),
                     '--decimate', str(refine_decimate), *refine_args,
                     '-o', 'mesh_refined.ply', log=log)
                cp.save('refine', refine_inputs, [work / 'mesh_refined.ply'])
            mesh = 'mesh_refined.ply'
        if not texture:
            return work / mesh
        return texture_mesh(work, scene, mesh, log)


DSINE_IMAGE = os.environ.get("VISTA_DSINE_IMAGE", "vista-dsine:latest")


def predict_normals(work: Path, out_dir: Path, width: int = 1344, log=None) -> Path:
    """DSINE surface normals for every undistorted image of the COLMAP scene in
    `work` (GPU container, weights baked in → offline). One .npy per image."""
    work, out_dir = Path(work).resolve(), Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    uid = f"{os.getuid()}:{os.getgid()}"
    offline = Path(__file__).resolve().parents[4] / 'scripts/raw/dsine_offline.py'
    cmd = ["docker", "run", "--rm", "--gpus", "all", "-u", uid, "-e", "HOME=/tmp",
           "-v", f"{work}:{work}", "-v", f"{out_dir}:{out_dir}", '-v', f'{offline}:{offline}:ro', DSINE_IMAGE,
           "python", str(offline), str(work), str(out_dir), str(width)]
    with gpu_lock():
        r = subprocess.run(cmd, stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise RuntimeError(f"DSINE (AI normály) selhal (kód {r.returncode})")
    return out_dir
