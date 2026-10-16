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

    data = json.loads(Path(sfm_json).read_text())
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

    def undistort(vw):
        from app.services.raw_images import read_bgr
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
        if not cv2.imwrite(str(img_dir / vw["name"]), und, options):
            raise RuntimeError(f"Cannot write OpenMVS image {vw['name']}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(undistort, views))

    # landmarks + undistorted observations
    pts2d: dict[int, list] = {vw["id"]: [] for vw in views}
    p3_lines = []
    for L in data.get("structure", []):
        X = [float(x) for x in L["X"]]
        rgb = [int(float(c)) for c in L.get("color", [128, 128, 128])][:3]
        track = []
        for ob in L.get("observations", []):
            vw = by_view.get(ob.get("observationId"))
            if vw is None:
                continue
            xy = np.array([[[float(ob["x"][0]), float(ob["x"][1])]]], float)
            u = cv2.undistortPoints(xy, vw["K"], vw["dist"], P=vw["K"])[0, 0] * vw["s"]
            idx = len(pts2d[vw["id"]])
            pts2d[vw["id"]].append((u[0], u[1], len(p3_lines) + 1))
            track.append(f"{vw['id']} {idx}")
        if len(track) >= 2:
            p3_lines.append(f"{len(p3_lines) + 1} {X[0]} {X[1]} {X[2]} {rgb[0]} {rgb[1]} {rgb[2]} 0 "
                            + " ".join(track))
        else:                                   # keep indices consistent: drop its 2D refs
            for vw_id in [int(tr.split()[0]) for tr in track]:
                pts2d[vw_id][-1] = (pts2d[vw_id][-1][0], pts2d[vw_id][-1][1], -1)

    with open(sp_dir / "cameras.txt", "w") as fc, open(sp_dir / "images.txt", "w") as fi:
        for vw in views:
            K, s = vw["K"], vw["s"]
            fc.write(f"{vw['id']} PINHOLE {round(vw['w'] * s)} {round(vw['h'] * s)} "
                     f"{K[0, 0] * s} {K[1, 1] * s} {K[0, 2] * s} {K[1, 2] * s}\n")
            q = _quat_from_R(vw["R"])
            fi.write(f"{vw['id']} {q[0]} {q[1]} {q[2]} {q[3]} {vw['t'][0]} {vw['t'][1]} {vw['t'][2]} "
                     f"{vw['id']} {vw['name']}\n")
            fi.write(" ".join(f"{x} {y} {pid}" for x, y, pid in pts2d[vw["id"]]) + "\n")
    with open(sp_dir / "points3D.txt", "w") as fp:
        fp.write("\n".join(p3_lines) + "\n")
    return {"views": len(views), "points": len(p3_lines)}


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
    def __init__(self, tool: str, code: int):
        self.code = code
        super().__init__(f"OpenMVS {tool} selhal (kód {code})")


def _mvs(work: Path, *args: str, log) -> None:
    """Run one OpenMVS tool in the container (work dir mounted at the same path)."""
    uid = f"{os.getuid()}:{os.getgid()}"
    gpu = ["--gpus", "all"] if "cuda" in OPENMVS_IMAGE else []
    cmd = ["docker", "run", "--rm", *gpu, "-u", uid, "-v", f"{work}:{work}", "-w", str(work),
           OPENMVS_IMAGE, *args]
    log.write(f"\n$ {' '.join(args)}\n"); log.flush()
    if gpu and args[0] in GPU_TOOLS:
        with gpu_lock():
            r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    else:
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise OpenMVSError(args[0], r.returncode)


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
        _textured_ply_to_obj(ply, obj)
    if not obj.is_file():
        raise RuntimeError('OpenMVS nevytvořil texturovaný model.')
    return obj


def _densify(work: Path, log, *args: str, attempts: int = 3) -> None:
    """DensifyPointCloud with resume. The CUDA build's geometric-consistency pass
    races on large sets (~200 photos): a neighbour's depth map is read while
    being rewritten ("error: opening file 'depthNNNN.dmap'", empty
    *.geo.dmap.tmp left behind) and the tool exits 1. Finished depth maps are
    reused by a re-run, so dropping the half-written temporaries and running
    again completes the rest."""
    # depth + normal + confidence per pixel (+ geometric pass copies): check the
    # disk up front — a full disk makes OpenMVS exit 1 half an hour in, silently
    # Measured: 73 MB per 3 Mpx map, kept twice (photometric + geometric pass)
    # → ~50 B/px. When the disk can't hold that, cap the depth-map resolution so
    # it fits (RefineMesh still refines against the full images afterwards).
    level = int(args[args.index("--resolution-level") + 1]) if "--resolution-level" in args else 1
    h = w = 0
    n = sum(1 for _ in (work / "images").iterdir())
    for f in (work / "images").iterdir():
        try:
            from PIL import Image
            with Image.open(f) as image:
                w, h = image.size
            break
        except Exception:  # noqa: BLE001
            continue
    max_res = int(args[args.index("--max-resolution") + 1]) if "--max-resolution" in args else 2560
    side = min(max(h, w) / 2 ** level, max_res)
    px = side * side * (min(h, w) / max(h, w)) if h else 0
    need = px * 50 * n
    from app.config import settings
    free = max(0, shutil.disk_usage(work).free - (settings.disk_reserve_gb + 3) * 1e9)
    if need and free < need:
        side = int(side * (free / need) ** 0.5)
        if side < 1024:
            raise RuntimeError(f"Nedostatek místa na disku pro hloubkové mapy OpenMVS: "
                               f"potřeba ~{need / 1e9:.0f} GB, volných {free / 1e9:.0f} GB.")
        log.write(f"\nMálo místa na disku ({free / 1e9:.0f} GB, potřeba {need / 1e9:.0f} GB): "
                  f"hloubkové mapy max. {side} px\n")
        log.flush()
        args = (*[a for i, a in enumerate(args) if a != "--max-resolution"
                  and (i == 0 or args[i - 1] != "--max-resolution")],
                "--max-resolution", str(side))
    for k in range(attempts):
        try:
            _mvs(work, "DensifyPointCloud", "scene.mvs", *args, log=log)
            return
        except RuntimeError:
            if k == attempts - 1:
                raise
            for tmp in work.glob("*.tmp"):
                tmp.unlink(missing_ok=True)
            log.write(f"\nDensifyPointCloud selhal — pokus {k + 2}/{attempts} (pokračuje z hotových map)\n")
            log.flush()


MESH_POINTS = 15e6


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
    work = Path(work).resolve()
    scene = "scene.mvs"
    with open(work / "openmvs.log", "a") as log:
        if not (work / "scene.mvs").exists():
            _mvs(work, "InterfaceCOLMAP", "-i", str(work), "-o", "scene.mvs",
                 "--image-folder", str(work / "images") + "/", log=log)
        if not skip_dense:
            _densify(work, log, "--resolution-level", str(resolution_level),
                     "--number-views-fuse", str(fuse_views))
            # depth maps are only needed for the fusion above — ~60 MB each
            # (≈12 GB for 200 photos); free the disk before meshing
            for dm in work.glob("*.dmap"):
                dm.unlink(missing_ok=True)
            # Delaunay meshing needs ~5 cells per point and the graph cut holds
            # them all: 44 M points (728 drone photos) ran 62 GB out of memory.
            # Thin the cloud to ~MESH_POINTS via a larger min. point distance
            # (px); RefineMesh restores the detail from the photos afterwards.
            n_pts = _ply_vertex_count(work / "scene_dense.ply")
            dist = 2.5 * max(1.0, (n_pts / MESH_POINTS) ** 0.5) if n_pts else 2.5
            if dist > 2.5:
                log.write(f"\nMračno má {n_pts / 1e6:.0f} M bodů — síťování s min. vzdáleností {dist:.1f} px\n")
            _mvs(work, "ReconstructMesh", "scene_dense.mvs", "--close-holes", str(close_holes),
                 "--min-point-distance", f"{dist:.2f}", "-o", "mesh.ply", log=log)
        if (work / "scene_dense.mvs").exists():
            scene = "scene_dense.mvs"
        mesh = input_mesh or "mesh.ply"
        if refine:
            lvl = resolution_level if refine_level is None else refine_level
            while True:
                try:
                    _mvs(work, "RefineMesh", scene, "--mesh-file", mesh,
                         "--resolution-level", str(lvl), "--close-holes", str(close_holes),
                         "--decimate", str(refine_decimate), *refine_args,
                         "-o", "mesh_refined.ply", log=log)
                    break
                except RuntimeError:
                    # big sets can exhaust RAM at full image resolution — a
                    # coarser refinement beats losing hours of work
                    if lvl >= 3:
                        raise
                    lvl += 1
                    log.write(f"\nRefineMesh selhal — zkouším úroveň rozlišení {lvl}\n")
                    log.flush()
            mesh = "mesh_refined.ply"
        if not texture:
            return work / mesh
        return texture_mesh(work, scene, mesh, log)


DSINE_IMAGE = os.environ.get("VISTA_DSINE_IMAGE", "vista-dsine")


def predict_normals(work: Path, out_dir: Path, width: int = 1344, log=None) -> Path:
    """DSINE surface normals for every undistorted image of the COLMAP scene in
    `work` (GPU container, weights baked in → offline). One .npy per image."""
    work, out_dir = Path(work).resolve(), Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    uid = f"{os.getuid()}:{os.getgid()}"
    cmd = ["docker", "run", "--rm", "--gpus", "all", "-u", uid, "-e", "HOME=/tmp",
           "-v", f"{work}:{work}", "-v", f"{out_dir}:{out_dir}", DSINE_IMAGE,
           "python", "/dsine/infer_normals.py", str(work), str(out_dir), str(width)]
    with gpu_lock():
        r = subprocess.run(cmd, stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise RuntimeError(f"DSINE (AI normály) selhal (kód {r.returncode})")
    return out_dir
