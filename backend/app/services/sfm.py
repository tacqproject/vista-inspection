"""
Parse AliceVision cameras.sfm and project 3D model points into source photos.

Used by the "show me this spot on the photos" feature: the viewer sends a 3D
point in original model coordinates, we return the photos that see it and the
normalized (u, v) position of the point in each image.
"""

import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path

# trimesh ray-casting goes through rtree/libspatialindex, which is NOT
# thread-safe: concurrent intersects on the shared cached mesh (raycasts run in
# asyncio.to_thread executor threads) segfaults the whole worker. Serialise every
# rtree access — raycasts are fast (ms) so this costs nothing in practice.
_RAY_LOCK = threading.Lock()

# At most one loaded mesh resides in memory (a 2M-face gltf ≈ 500 MB); keyed by
# path AND mtime, so a re-bake that overwrites the same path is picked up instead
# of silently ray-casting against stale geometry. {path: (mtime_ns, mesh)}.
_mesh_cache: dict = {}
HUGE_LOD0_BYTES = 600_000_000      # ≈ 20 M faces


def _get_mesh(mesh_path: Path):
    """Return the raycasting mesh for `mesh_path`, loading + caching on demand.

    MUST be called while holding `_RAY_LOCK` (trimesh's rtree/embree accelerator
    is built lazily on first ray and is not thread-safe). The cache reloads when
    the file's mtime changes (e.g. after canonical re-baking writes the same path).
    """
    import trimesh

    # a huge lod0 (merged full-resolution regions: tens of millions of faces,
    # 5+ GB with its BVH) → raycast its 25 % lod1 instead; picking a photo for a
    # clicked point does not need sub-millimetre geometry
    mesh_path = Path(mesh_path)
    if mesh_path.name.endswith("_lod0.gltf"):
        bin_ = mesh_path.with_suffix(".bin")
        lod1 = mesh_path.with_name(mesh_path.name.replace("_lod0", "_lod1"))
        if bin_.exists() and bin_.stat().st_size > HUGE_LOD0_BYTES and lod1.exists():
            mesh_path = lod1
    key = str(mesh_path)
    mtime = Path(mesh_path).stat().st_mtime_ns
    entry = _mesh_cache.get(key)
    if entry is None or entry[0] != mtime:
        # skip_materials: the gltf references several 8K texture atlases; decoding
        # them takes ~30 s and we only need geometry for raycasting (31 s → 0.24 s).
        # process=False keeps the clean mesh as-is.
        loaded = trimesh.load(mesh_path, force="mesh", process=False, skip_materials=True)
        _mesh_cache.clear()                       # keep at most one mesh resident
        _mesh_cache[key] = (mtime, loaded)
    return _mesh_cache[key][1]


def mesh_to_sfm(x: float, y: float, z: float) -> tuple[float, float, float]:
    """
    AliceVision saves OBJ meshes with y/z negated relative to its SfM frame
    (graphics vs CV convention); the viewer therefore works in the "mesh"
    frame. This involution converts between the two frames (both directions).
    """
    return x, -y, -z


@dataclass
class Camera:
    filename: str
    width: int
    height: int
    # rotation stored column-major; camera looks along −Z: X_cam = Rᵀ(X − C), y/z flipped
    r: list[float]
    c: list[float]
    fx: float
    fy: float
    ppx: float  # principal point offset from image center, px
    ppy: float
    dist: tuple[float, float, float] = (0.0, 0.0, 0.0)  # radial k1, k2, k3


def load_cameras(sfm_path: Path) -> list[Camera]:
    data = json.loads(sfm_path.read_text())

    intrinsics = {i["intrinsicId"]: i for i in data.get("intrinsics", [])}
    poses = {p["poseId"]: p["pose"]["transform"] for p in data.get("poses", [])}

    cameras: list[Camera] = []
    for view in data.get("views", []):
        pose = poses.get(view.get("poseId"))
        intr = intrinsics.get(view.get("intrinsicId"))
        if not pose or not intr:
            continue  # view was not reconstructed

        width = int(intr["width"])
        height = int(intr["height"])

        if "pxFocalLength" in intr:
            pxf = intr["pxFocalLength"]
            if isinstance(pxf, list):
                fx, fy = float(pxf[0]), float(pxf[1])
            else:
                fx = fy = float(pxf)
        else:
            # mm focal length + sensor width → px
            fx = float(intr["focalLength"]) / float(intr.get("sensorWidth", 36.0)) * width
            fy = fx / float(intr.get("pixelRatio", 1.0))

        if not (fx > 0 and fy > 0):
            continue  # degenerate intrinsic — unprojection would divide by zero

        pp = intr.get("principalPoint", [0.0, 0.0])
        dp = [float(v) for v in intr.get("distortionParams", [])[:3]]
        while len(dp) < 3:
            dp.append(0.0)

        cameras.append(Camera(
            filename=Path(view["path"]).name,
            width=width,
            height=height,
            r=[float(x) for x in pose["rotation"]],
            c=[float(x) for x in pose["center"]],
            fx=fx, fy=fy,
            ppx=float(pp[0]), ppy=float(pp[1]),
            dist=(dp[0], dp[1], dp[2]),
        ))
    return cameras


MIN_VIEW_COS = 0.26        # ≤ ~75° from the surface normal; flatter views are smeared
FRAME_MARGIN = 0.02        # the outermost 2 % of a photo: soft, often cut off


def project_point(
    cameras: list[Camera],
    x: float,
    y: float,
    z: float,
    normal: tuple[float, float, float] | None = None,
) -> list[dict]:
    """
    Project a world-space point into every reconstructed camera; returns the
    photos that have it in frame, best first.

    Ranking = how well the photo shows the spot: its resolution there (focal
    length / depth → pixels per unit of surface), reduced for oblique views
    (√cos of the angle to the surface normal) and near the frame edge. With
    `normal`, cameras behind the surface or flatter than ~75° are dropped (only
    if nothing else sees it are they returned, flagged `oblique`). Occlusion is
    verified separately by `filter_by_backprojection`.
    """
    results = []       # every camera that has the point in frame (in front of lens)
    angle_ok = []      # subset seeing it at a reasonable angle to the surface
    n_len = math.sqrt(sum(c * c for c in normal)) if normal is not None else 0.0
    for cam in cameras:
        dx, dy, dz = x - cam.c[0], y - cam.c[1], z - cam.c[2]

        r = cam.r
        # AliceVision: X_cam = Rᵀ(X − C) with CV axes (+Z forward, +Y down).
        # Verified against 24k SfM landmark observations (median error 0.5 px);
        # see docs/sfm-convention note. No axis flips!
        xc = r[0] * dx + r[3] * dy + r[6] * dz
        yc = r[1] * dx + r[4] * dy + r[7] * dz
        zc = r[2] * dx + r[5] * dy + r[8] * dz
        if zc <= 0:
            continue  # behind the camera

        # Radial lens distortion (AliceVision radial3, self-calibrated in the
        # bundle adjustment) on normalized coords
        xn, yn = xc / zc, yc / zc
        k1, k2, k3 = cam.dist
        r2 = xn * xn + yn * yn
        factor = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        u = cam.fx * xn * factor + cam.width / 2 + cam.ppx
        v = cam.fy * yn * factor + cam.height / 2 + cam.ppy
        un, vn = u / cam.width, v / cam.height
        if not (FRAME_MARGIN <= un < 1 - FRAME_MARGIN and FRAME_MARGIN <= vn < 1 - FRAME_MARGIN):
            continue

        centrality = ((un - 0.5) ** 2 + (vn - 0.5) ** 2) ** 0.5
        distance = (dx * dx + dy * dy + dz * dz) ** 0.5
        cosang = None
        if normal is not None and n_len > 0:
            # point→camera vs the surface normal (both normalized → true cosine;
            # a non-unit normal, e.g. alignment-scaled, must not skew the test)
            cosang = -sum(nc * d for nc, d in zip(normal, (dx, dy, dz))) / ((distance or 1.0) * n_len)
        res = cam.fx / zc                                  # px per unit of fronto-parallel surface
        score = res * math.sqrt(max(cosang, 0.05) if cosang is not None else 1.0) \
            * (1.0 - 0.6 * max(0.0, centrality - 0.35))
        entry = {
            "filename": cam.filename,
            "u": round(un, 4),
            "v": round(vn, 4),
            "distance": round(distance, 3),
            "centrality": round(centrality, 4),
            "cos": round(cosang, 3) if cosang is not None else None,
            "score": score,
            "camera_center": [cam.c[0], cam.c[1], cam.c[2]],
        }
        results.append(entry)
        if cosang is not None and cosang >= MIN_VIEW_COS:
            angle_ok.append(entry)

    # Use the angle-filtered set; only if nothing sees the surface at a usable
    # angle return the oblique views (flagged) rather than nothing.
    if normal is not None and n_len > 0:
        if angle_ok:
            out = angle_ok
        else:
            out = results
            for e in out:
                e["oblique"] = True
    else:
        out = results
    out.sort(key=lambda p: -p["score"])
    return out


def _project_into_camera(cam: "Camera", x: float, y: float, z: float):
    """
    World point → normalized (u, v) in the given camera (same math as
    project_point). Returns None only when the point is behind the camera; a
    point outside the frame still returns its (possibly <0 or >1) coords so a
    polygon outline can extend past the image edge.
    """
    dx, dy, dz = x - cam.c[0], y - cam.c[1], z - cam.c[2]
    r = cam.r
    xc = r[0] * dx + r[3] * dy + r[6] * dz
    yc = r[1] * dx + r[4] * dy + r[7] * dz
    zc = r[2] * dx + r[5] * dy + r[8] * dz
    if zc <= 0:
        return None
    xn, yn = xc / zc, yc / zc
    k1, k2, k3 = cam.dist
    r2 = xn * xn + yn * yn
    factor = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    u = cam.fx * xn * factor + cam.width / 2 + cam.ppx
    v = cam.fy * yn * factor + cam.height / 2 + cam.ppy
    return (u / cam.width, v / cam.height)


def project_outline(
    cameras: list[Camera],
    filenames: list[str],
    outline_world: list[tuple[float, float, float]],
) -> dict[str, list]:
    """
    For each named camera, project every outline vertex → list of {u, v} (or
    None for a vertex behind that camera). Returns {filename: [...]}. Lets the
    photos show a defect's polygon boundary, matching the 3D outline.
    """
    by_name = {cam.filename: cam for cam in cameras}
    out: dict[str, list] = {}
    for fn in filenames:
        cam = by_name.get(fn)
        if not cam:
            continue
        verts = []
        behind = 0
        for (x, y, z) in outline_world:
            uv = _project_into_camera(cam, x, y, z)
            if uv is None:
                behind += 1
                verts.append(None)
            else:
                verts.append({"u": round(uv[0], 4), "v": round(uv[1], 4)})
        # skip photos where the whole outline is behind the lens
        if behind < len(outline_world):
            out[fn] = verts
    return out


def rank_by_outline(cameras: list[Camera], matches: list[dict],
                    outline_world: list[tuple[float, float, float]],
                    min_px: float = 40.0, min_inside: float = 0.7) -> list[dict]:
    """Photos of a DEFECT: keep those where its outline is reasonably large
    (longer side ≥ min_px pixels) and mostly in frame (≥ min_inside of the
    outline vertices), best first by visible size × √cos. Each match gets
    `defect_px` (longer side, px). If nothing qualifies the input order stays."""
    by_name = {c.filename: c for c in cameras}
    outl = project_outline(cameras, [m["filename"] for m in matches], outline_world)
    good = []
    for m in matches:
        cam = by_name.get(m["filename"])
        uv = [p for p in (outl.get(m["filename"]) or []) if p is not None]
        if not cam or len(uv) < 2:
            continue
        us = [p["u"] for p in uv]; vs = [p["v"] for p in uv]
        inside = sum(1 for p in uv if 0 <= p["u"] <= 1 and 0 <= p["v"] <= 1) / len(uv)
        side = max((max(us) - min(us)) * cam.width, (max(vs) - min(vs)) * cam.height)
        m["defect_px"] = round(side)
        if side >= min_px and inside >= min_inside:
            m["_rank"] = min(side, 1500.0) * inside * math.sqrt(max(m.get("cos") or 1.0, 0.05))
            good.append(m)
    if not good:
        return matches
    good.sort(key=lambda m: -m.pop("_rank"))
    return good


def pixel_to_mesh(
    cameras: list[Camera],
    mesh_path: Path,
    filename: str,
    pixels: list[tuple[float, float]],
) -> list[dict]:
    """
    Back-project normalized image pixels (u, v ∈ 0–1) of a source photo onto
    the mesh: ray from the camera through the pixel → nearest intersection.
    Returns per-pixel {hit, point (mesh frame)} — enables measuring in photo
    space at full image resolution with 3D-accurate results.
    """
    import numpy as np

    cam = next((c for c in cameras if c.filename == filename), None)
    if cam is None:
        raise ValueError(f"Kamera pro snímek '{filename}' nenalezena")

    r = cam.r
    M = np.array(r).reshape(3, 3)
    c_mesh = np.array(mesh_to_sfm(*cam.c))
    k1, k2, k3 = cam.dist

    with _RAY_LOCK:                       # rtree is not thread-safe — serialise
        mesh = _get_mesh(mesh_path)

        out = []
        for u, v in pixels:
            xn = (u * cam.width - cam.width / 2 - cam.ppx) / cam.fx
            yn = (v * cam.height - cam.height / 2 - cam.ppy) / cam.fy
            # undo radial distortion (fixed-point, 3 iterations is plenty)
            xu, yu = xn, yn
            for _ in range(3):
                r2 = xu * xu + yu * yu
                f = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
                xu, yu = xn / f, yn / f
            # camera ray → world (SfM frame; forward is X_cam = Mᵀ(X−C) ⇒ d = M·d_cam)
            d_sfm = M @ np.array([xu, yu, 1.0])
            d_mesh = np.array(mesh_to_sfm(*d_sfm))
            d_mesh /= np.linalg.norm(d_mesh)

            locs, _, _ = mesh.ray.intersects_location(
                ray_origins=np.array([c_mesh]), ray_directions=np.array([d_mesh]),
            )
            if len(locs) == 0:
                out.append({"hit": False, "point": None})
                continue
            dists = np.linalg.norm(locs - c_mesh, axis=1)
            p = locs[int(np.argmin(dists))]
            out.append({"hit": True, "point": [float(p[0]), float(p[1]), float(p[2])]})
    return out


def surface_normal(mesh_path: Path, x: float, y: float, z: float):
    """Outward surface normal (mesh frame) at the closest face to (x,y,z), so the
    photo-visibility angle filter works even for saved defects (whose stored
    coords carry no normal). Returns (nx,ny,nz) or None."""
    import numpy as np

    with _RAY_LOCK:                       # trimesh proximity uses rtree — serialise
        mesh = _get_mesh(mesh_path)
        try:
            _, _, tri = mesh.nearest.on_surface(np.array([[x, y, z]]))
            n = mesh.face_normals[int(tri[0])]
            return (float(n[0]), float(n[1]), float(n[2]))
        except Exception:
            return None


def filter_by_backprojection(
    results: list[dict], cameras: list["Camera"], mesh_path: Path,
    x: float, y: float, z: float, tol_rel: float = 0.02,
) -> list[dict]:
    """
    Keep only photos whose marker actually lands on the clicked point. For each
    candidate we cast a ray from the camera through its computed (u, v) pixel and
    take the nearest mesh hit; if that hit isn't within tolerance of the clicked
    point, the camera doesn't really see the point (occluded or mis-calibrated)
    and its marker would sit on some *other* surface — so we drop it.

    It verifies the marker directly, catching both occlusion and pose/calibration
    drift. The tolerance is `tol_rel` of the camera's distance to the point
    (10 cm at 5 m): it once was 2 % of the whole model's diagonal — ~80 cm on a
    bridge, so photos looking at the spot THROUGH another pillar passed. All
    rays are cast in ONE batched query (fast). Returns [] when nothing verifies
    (caller falls back to approximate).
    """
    import numpy as np

    if not results:
        return results
    cam_by = {c.filename: c for c in cameras}
    pt = np.array([float(x), float(y), float(z)])

    # one camera→marker ray per candidate (same projection math as pixel_to_mesh)
    origins, directions, kept_idx = [], [], []
    for p in results:
        cam = cam_by.get(p["filename"])
        if cam is None:
            continue
        M = np.array(cam.r).reshape(3, 3)
        c_mesh = np.array(mesh_to_sfm(*cam.c))
        k1, k2, k3 = cam.dist
        xn = (p["u"] * cam.width - cam.width / 2 - cam.ppx) / cam.fx
        yn = (p["v"] * cam.height - cam.height / 2 - cam.ppy) / cam.fy
        xu, yu = xn, yn
        for _ in range(3):
            r2 = xu * xu + yu * yu
            f = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
            xu, yu = xn / f, yn / f
        d = np.array(mesh_to_sfm(*(M @ np.array([xu, yu, 1.0]))))
        nrm = np.linalg.norm(d)
        if nrm == 0:
            continue
        origins.append(c_mesh)
        directions.append(d / nrm)
        kept_idx.append(p)

    if not origins:
        return []

    with _RAY_LOCK:                       # rtree is not thread-safe — serialise
        mesh = _get_mesh(mesh_path)
        origins = np.array(origins)
        locations, index_ray, _ = mesh.ray.intersects_location(
            ray_origins=origins, ray_directions=np.array(directions),
        )

    # nearest hit per ray (closest to the camera = what the pixel actually sees)
    best: dict[int, tuple[float, "np.ndarray"]] = {}
    for loc, ri in zip(locations, index_ray):
        d_origin = float(np.linalg.norm(loc - origins[ri]))
        if ri not in best or d_origin < best[ri][0]:
            best[ri] = (d_origin, loc)

    kept = []
    for ri, p in enumerate(kept_idx):
        b = best.get(ri)
        tol = tol_rel * max(float(p.get("distance") or np.linalg.norm(origins[ri] - pt)), 1e-9)
        if b is not None and float(np.linalg.norm(b[1] - pt)) <= tol:
            p.pop("camera_center", None)
            kept.append(p)
    return kept
