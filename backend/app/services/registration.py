"""
Spatial registration of one inspection's model onto another (reference) one.

A follow-up inspection often covers only PART of the structure (one facade,
one span of a bridge). Each photogrammetric reconstruction lives in its own
arbitrary frame (rotation, origin, scale), so to compare them the new model
must be mapped onto the reference.

Pipeline (partial → full is the hard case, so a global stage is mandatory):
  1. sample both meshes into point clouds and bring them to METRIC scale using
     each inspection's calibration factor — that removes the scale unknown and
     leaves a plain 6-DOF rigid problem, which registers far more reliably,
  2. voxel downsample → normals → FPFH features,
  3. global RANSAC feature matching (no initial guess needed),
  4. point-to-plane ICP refinement.

Returns the matrix in MESH coordinates: X_ref_mesh = M · X_new_mesh, so the
viewer can drop the new model straight into the reference model's frame.
"""

from pathlib import Path

import numpy as np

# a defect is centimetres; 5 cm voxels keep enough structure for FPFH while
# staying fast on 600k-triangle meshes
VOXEL_M = 0.05
SAMPLE_POINTS = 120_000
MIN_FITNESS = 0.25          # below this the result is not trustworthy


def _cloud(mesh_path: Path, scale_to_m: float, o3d):
    import trimesh

    mesh = trimesh.load(mesh_path, force="mesh")
    pts = mesh.sample(SAMPLE_POINTS) * scale_to_m
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(np.asarray(pts, dtype=np.float64))
    return pc


def _prepare(pc, voxel: float, o3d):
    down = pc.voxel_down_sample(voxel)
    down.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30)
    )
    fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        down, o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 5, max_nn=100)
    )
    return down, fpfh


def _align_clouds(src, dst, voxel: float, o3d):
    """FPFH + RANSAC global registration, refined by point-to-plane ICP."""
    src_d, src_f = _prepare(src, voxel, o3d)
    dst_d, dst_f = _prepare(dst, voxel, o3d)

    dist = voxel * 1.5
    coarse = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
        src_d, dst_d, src_f, dst_f,
        mutual_filter=True,
        max_correspondence_distance=dist,
        estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(False),
        ransac_n=3,
        checkers=[
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnEdgeLength(0.9),
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnDistance(dist),
        ],
        criteria=o3d.pipelines.registration.RANSACConvergenceCriteria(4_000_000, 0.999),
    )
    return o3d.pipelines.registration.registration_icp(
        src_d, dst_d, voxel * 0.8, coarse.transformation,
        o3d.pipelines.registration.TransformationEstimationPointToPlane(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=80),
    )


def register(
    new_mesh: Path,
    ref_mesh: Path,
    new_scale: float | None = None,
    ref_scale: float | None = None,
) -> dict:
    """
    Align `new_mesh` onto `ref_mesh`.

    new_scale/ref_scale: mesh units → metres (inspection calibration factors).
    When both are calibrated the scale is known, which leaves a plain 6-DOF
    rigid problem — the fast path. BUT that path trusts the two calibrations to
    agree; if they don't (a common mistake — one inspection mis-measured), the
    metric fit is impossible and returns garbage. So when the calibrated result
    is weak we fall back to the geometry-only scale search, which ignores the
    calibration and finds the scale from the shapes themselves.
    """
    import open3d as o3d

    if not (_calibrated(new_scale) and _calibrated(ref_scale)):
        return _register_unscaled(new_mesh, ref_mesh, ref_scale, o3d)

    src = _cloud(new_mesh, new_scale, o3d)
    dst = _cloud(ref_mesh, ref_scale, o3d)
    fine = _align_clouds(src, dst, VOXEL_M, o3d)

    T = np.asarray(fine.transformation)          # metric frame: new(m) → ref(m)

    # metric → mesh coordinates: scale in, transform, scale back out
    S_in = np.diag([new_scale, new_scale, new_scale, 1.0])
    S_out = np.diag([1 / ref_scale, 1 / ref_scale, 1 / ref_scale, 1.0])
    M = S_out @ T @ S_in

    # Score with the SAME tight, bbox-relative metric as the uncalibrated path, so
    # the stored fitness is comparable across paths AND scale-independent. The raw
    # ICP fitness (`fine.fitness`) is an inlier ratio at a FIXED 0.04 m threshold:
    # on a large structure that is punishingly tight and reports near-zero for a
    # genuinely good alignment, wrongly failing the MIN_FITNESS gate.
    tv = _REL_VOXEL * (_diag(ref_mesh) * ref_scale)   # tight threshold, in metres
    ev = _score(src.voxel_down_sample(tv), dst.voxel_down_sample(tv), T, tv, o3d)

    metric = {
        "matrix": [float(v) for v in M.flatten()],   # row-major 4×4
        "fitness": float(ev.fitness),                # tight overlap ratio (partial → < 1)
        "rmse": float(ev.inlier_rmse),               # metres
        "metric": True,
        "ok": bool(ev.fitness >= MIN_FITNESS),
    }
    if metric["fitness"] >= 0.5:
        return metric

    # weak metric fit → the two calibrations probably disagree. Retry ignoring
    # them (find the scale from geometry) and keep whichever locked on better.
    alt = _register_unscaled(new_mesh, ref_mesh, ref_scale, o3d)
    return alt if alt["fitness"] > metric["fitness"] else metric


# ─── Uncalibrated fallback ───────────────────────────────────────────────────

def _calibrated(s: float | None) -> bool:
    # 1.0 is the default "never calibrated" value, not a real measurement
    return bool(s) and abs(s - 1.0) > 1e-9


# a partial scan's bounding box is smaller than the reference's, so the naive
# diagonal ratio OVERestimates the scale — the hypotheses walk down from it
_SCALE_HYPOTHESES = (1.0, 0.87, 0.76, 0.66, 0.57, 0.50, 0.43, 1.15)
_REL_VOXEL = 0.008          # voxel as a fraction of the reference's bbox diagonal


def _diag(mesh_path: Path) -> float:
    import trimesh

    lo, hi = trimesh.load(mesh_path, force="mesh").bounds
    return float(np.linalg.norm(hi - lo))


def _score(src_d, dst_d, T, voxel, o3d):
    """
    Fitness at a TIGHT threshold (one voxel). The loose fitness used to drive
    RANSAC saturates on large partial overlaps and happily reports a fit that
    is off by several voxels; scoring tightly is what separates a real lock-on
    from a plausible-looking near miss.
    """
    return o3d.pipelines.registration.evaluate_registration(src_d, dst_d, voxel, T)


def _register_unscaled(new_mesh: Path, ref_mesh: Path, ref_scale, o3d) -> dict:
    """
    Neither model has a metric scale, so the transform has 7 DOF. FPFH features
    are scale-dependent, so a single run cannot find the scale — we register
    under several scale hypotheses (seeded from the bbox-diagonal ratio), keep
    the tightest-fitting one, then re-run the whole registration at the scale a
    scale-enabled ICP derives from it (features are only meaningful once the
    scale is roughly right). Slower and less certain than the calibrated path;
    calibrating both inspections is still the better route.
    """
    voxel = _REL_VOXEL * _diag(ref_mesh)          # in the reference's units
    r0 = _diag(ref_mesh) / max(_diag(new_mesh), 1e-9)

    dst = _cloud(ref_mesh, 1.0, o3d)              # reference units = working units
    dst_d = dst.voxel_down_sample(voxel)
    dst_d.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30)
    )

    def attempt(s: float):
        """Register at scale hypothesis `s`, polish scale, score tightly."""
        src_d = _cloud(new_mesh, s, o3d).voxel_down_sample(voxel)
        coarse = _align_clouds(_cloud(new_mesh, s, o3d), dst, voxel, o3d)

        # scale-enabled ICP: absorbs the residual scale error into the matrix
        fine = o3d.pipelines.registration.registration_icp(
            src_d, dst_d, voxel * 1.5, coarse.transformation,
            o3d.pipelines.registration.TransformationEstimationPointToPoint(True),
            o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=60),
        )
        cands = [
            (_score(src_d, dst_d, T, voxel, o3d), np.asarray(T))
            for T in (coarse.transformation, fine.transformation)
        ]
        ev, T = max(cands, key=lambda c: (c[0].fitness, -c[0].inlier_rmse))
        return ev, T, s

    best = max((attempt(r0 * k) for k in _SCALE_HYPOTHESES),
               key=lambda c: (c[0].fitness, -c[0].inlier_rmse))

    # bootstrap: redo the search at the scale the winner implies, so the FPFH
    # features are computed at (nearly) the true scale
    ev, T, s = best
    implied = float(np.linalg.norm(T[:3, 0]))     # uniform scale baked into T
    if abs(implied - 1.0) > 0.02:
        boot = attempt(s * implied)
        if (boot[0].fitness, -boot[0].inlier_rmse) > (ev.fitness, -ev.inlier_rmse):
            ev, T, s = boot

    M = np.asarray(T) @ np.diag([s, s, s, 1.0])   # new mesh units → ref mesh units

    return {
        "matrix": [float(v) for v in M.flatten()],
        "fitness": float(ev.fitness),
        # without calibration the residual is in the reference's model units;
        # only convert when the reference itself is calibrated
        "rmse": float(ev.inlier_rmse) * (ref_scale if _calibrated(ref_scale) else 1.0),
        "metric": bool(_calibrated(ref_scale)),
        "ok": bool(ev.fitness >= MIN_FITNESS),
    }


def gltf_bounds(mesh_path: Path):
    """
    (lo, hi) of a .gltf straight from its POSITION accessors' min/max — no
    geometry or (slow) texture decoding. Verified to match trimesh's bounds for
    our exports (single identity node, plain float positions). Falls back to
    trimesh for anything else.
    """
    import json

    try:
        g = json.loads(Path(mesh_path).read_text())
        mins, maxs = [], []
        for m in g.get("meshes", []):
            for pr in m.get("primitives", []):
                acc = g["accessors"][pr["attributes"]["POSITION"]]
                if "min" in acc and "max" in acc:
                    mins.append(acc["min"]); maxs.append(acc["max"])
        if mins:
            return np.array(mins, float).min(0), np.array(maxs, float).max(0)
    except Exception:
        pass
    import trimesh
    return trimesh.load(mesh_path, force="mesh").bounds


def gltf_vertices(mesh_path: Path, max_pts: int = 6000):
    """
    Vertex positions of a .gltf read directly from its buffer (no textures), as
    a coverage/point cloud. Subsampled to `max_pts`. Falls back to trimesh
    sampling for non-gltf or exotic layouts.
    """
    import json
    import base64

    try:
        path = Path(mesh_path)
        g = json.loads(path.read_text())
        bufs = []
        for buf in g["buffers"]:
            uri = buf["uri"]
            if uri.startswith("data:"):
                bufs.append(base64.b64decode(uri.split(",", 1)[1]))
            else:
                bufs.append((path.parent / uri).read_bytes())
        chunks = []
        for m in g.get("meshes", []):
            for pr in m.get("primitives", []):
                acc = g["accessors"][pr["attributes"]["POSITION"]]
                if acc.get("componentType") != 5126 or acc.get("type") != "VEC3":
                    raise ValueError("unexpected POSITION format")
                bv = g["bufferViews"][acc["bufferView"]]
                data = bufs[bv["buffer"]]
                off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
                n = acc["count"]
                stride = bv.get("byteStride") or 12
                if stride == 12:
                    arr = np.frombuffer(data, np.float32, count=n * 3, offset=off).reshape(n, 3)
                else:
                    raw = np.frombuffer(data, np.uint8, count=stride * n, offset=off).reshape(n, stride)[:, :12]
                    arr = raw.copy().view(np.float32).reshape(n, 3)
                chunks.append(np.asarray(arr, float))
        verts = np.concatenate(chunks)
    except Exception:
        import trimesh
        mesh = trimesh.load(mesh_path, force="mesh")
        verts = mesh.sample(max_pts) if len(mesh.faces) else np.asarray(mesh.vertices)
        return np.asarray(verts, float)

    if len(verts) > max_pts:
        verts = verts[np.linspace(0, len(verts) - 1, max_pts).astype(int)]
    return verts


def display_transform(mesh_path: Path) -> tuple[float, list[float]]:
    """
    The viewer normalizes every model to a 5-unit box centred at the origin —
    annotations/measurements are stored in those SCENE coordinates. Mirror that
    here so backend maths can convert between mesh and scene space.
    Returns (scale, offset) with: X_scene = scale · X_mesh + offset
    """
    lo, hi = gltf_bounds(mesh_path)
    center = (lo + hi) / 2
    max_dim = float((hi - lo).max())
    s = 5.0 / max_dim if max_dim > 0 else 1.0
    off = (-center * s).tolist()
    return s, [float(v) for v in off]


# ─── Manual alignment from point correspondences ─────────────────────────────

def _umeyama(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """
    Similarity transform (scale+rotation+translation) that best maps src → dst
    in the least-squares sense (Umeyama 1991). src/dst: (N,3). Returns a 4×4.
    """
    src = np.asarray(src, float)
    dst = np.asarray(dst, float)
    n = len(src)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    sc, dc = src - mu_s, dst - mu_d
    cov = (dc.T @ sc) / n
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1
    R = U @ S @ Vt
    var_s = (sc ** 2).sum() / n
    s = float(np.trace(np.diag(D) @ S) / var_s) if var_s > 1e-12 else 1.0
    M = np.eye(4)
    M[:3, :3] = s * R
    M[:3, 3] = mu_d - s * R @ mu_s
    return M


def align_from_points(
    src_pts: list, dst_pts: list, new_mesh: Path, ref_mesh: Path,
    ref_scale: float | None = None,
) -> dict:
    """
    Alignment from user-picked correspondences (mesh coords): a similarity fit
    through the points, then a GENTLE ICP polish that's kept only if it improves
    the tight-threshold overlap — so a good manual pick is never made worse.
    Returns the mesh-space matrix (new → ref), like `register`.
    """
    import open3d as o3d

    src_pts = np.asarray(src_pts, float)
    dst_pts = np.asarray(dst_pts, float)
    M = _umeyama(src_pts, dst_pts)          # new mesh → ref mesh

    # residual of the picked points (how consistently the pairs fit ONE
    # similarity transform). With >3 points this exposes mismatched clicks.
    proj = (M[:3, :3] @ src_pts.T).T + M[:3, 3]
    per_pair = np.sqrt(((proj - dst_pts) ** 2).sum(1))   # residual of each pair
    point_rmse = float(np.sqrt((per_pair ** 2).mean()))
    dst_extent = float(np.linalg.norm(dst_pts.max(0) - dst_pts.min(0))) or 1.0
    # per-pair residual in metres (A's mesh→metre factor), so the UI can point
    # at the exact dvojice that doesn't match instead of a black-box "off".
    _m = ref_scale if _calibrated(ref_scale) else 1.0
    pair_residuals = [round(float(r) * _m, 3) for r in per_pair]

    # Conditioning: points confined to (nearly) a plane or a line leave the
    # out-of-plane orientation unconstrained, so small pick errors get amplified
    # into a tilt/shift ("rotated & offset"). Flag it so the UI can ask for a
    # point at a different depth.
    def _spread(p):
        c = p - p.mean(0)
        sv = np.linalg.svd(c, compute_uv=False)
        return float(sv[-1] / sv[0]) if sv[0] > 1e-9 else 0.0
    conditioning = min(_spread(src_pts), _spread(dst_pts))

    warning = None
    if len(src_pts) >= 4 and point_rmse / dst_extent > 0.08:
        # the pairs can't be reconciled by one transform → they don't actually
        # match. This is the usual cause of a wildly-off result.
        warning = ("Naklikané body si navzájem neodpovídají — každá dvojice musí "
                   "být PŘESNĚ stejné fyzické místo na obou modelech. Zkontrolujte "
                   "je (přibližte si), nebo je vymažte a zkuste znovu.")
    elif conditioning < 0.06:
        warning = ("Zvolené body leží skoro v jedné rovině — zarovnání může být "
                   "přetočené. Přidejte aspoň jeden bod v jiné hloubce (ostění, "
                   "roh stěny, něco vystouplého).")

    # The transform is EXACTLY the fit through the user's points — nothing else.
    # (An earlier ICP "polish" could slide a small detail onto a different, flat
    # part of the big model that happened to score higher — the opposite of what
    # the user asked for. The picks ARE the answer; we trust them.)
    best = M

    # overlap fitness just for display — evaluate the picks' transform, don't
    # change it. Cheap point clouds straight from the gltf (no texture decode).
    def _cloud_fast(path):
        pc = o3d.geometry.PointCloud()
        pc.points = o3d.utility.Vector3dVector(gltf_vertices(path, 30000))
        return pc

    try:
        src = _cloud_fast(new_mesh)
        dst = _cloud_fast(ref_mesh)
        diag = float(np.linalg.norm(dst.get_max_bound() - dst.get_min_bound())) or 1.0
        voxel = diag * 0.01
        ev = o3d.pipelines.registration.evaluate_registration(
            src.voxel_down_sample(voxel), dst.voxel_down_sample(voxel), voxel * 1.5, best,
        )
        fitness = float(ev.fitness)
    except Exception:
        fitness = 0.0

    return {
        "matrix": [float(v) for v in best.flatten()],
        "fitness": fitness,
        "warning": warning,
        "rmse": point_rmse * (ref_scale if _calibrated(ref_scale) else 1.0),
        "pair_residuals": pair_residuals,
        "metric": bool(_calibrated(ref_scale)),
        "ok": True,          # user-driven — trust the picks
    }


def mesh_matrix_to_scene(
    matrix: list[float],
    new_mesh: Path,
    ref_mesh: Path,
) -> list[float]:
    """mesh-space M (new→ref) → scene-space matrix (new_scene → ref_scene)."""
    s_new, o_new = display_transform(new_mesh)
    s_ref, o_ref = display_transform(ref_mesh)
    M = np.array(matrix, dtype=float).reshape(4, 4)

    D_new_inv = np.eye(4)
    D_new_inv[:3, :3] = np.eye(3) / s_new
    D_new_inv[:3, 3] = -np.array(o_new) / s_new

    D_ref = np.eye(4)
    D_ref[:3, :3] = np.eye(3) * s_ref
    D_ref[:3, 3] = np.array(o_ref)

    return [float(v) for v in (D_ref @ M @ D_new_inv).flatten()]
