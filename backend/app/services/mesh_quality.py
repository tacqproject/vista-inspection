"""
Objective quality metrics of a reconstructed mesh — so pipeline changes are
judged by numbers, not by eye.

  • holes     — boundary loops of the mesh except the outer rim of the scanned
                area: count, total / largest perimeter (relative to the bbox
                diagonal) and how many are "significant" (> 1 % of the diagonal).
  • accuracy  — distance of the SfM landmarks (points triangulated from many
                photos, the most reliable 3D evidence we have) to the mesh
                surface: median / p90 relative to the diagonal. Also the SfM
                reprojection RMS in pixels.
  • size      — faces, vertices, connected components.

CLI:  python -m app.services.mesh_quality <mesh.obj|gltf> [sfm.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def _load(mesh_path: Path):
    import trimesh
    m = trimesh.load(mesh_path, force="mesh", process=False, skip_materials=True)
    # weld seam-split vertices (texture seams are not holes)
    m.merge_vertices(merge_tex=True, merge_norm=True)
    return m


def hole_stats(m) -> dict:
    import trimesh
    diag = float(np.linalg.norm(m.bounds[1] - m.bounds[0]))
    e = np.sort(m.edges, axis=1)
    uniq, cnt = np.unique(e, axis=0, return_counts=True)
    bnd = uniq[cnt == 1]
    if len(bnd) == 0:
        return {"holes": 0, "significant_holes": 0, "hole_perimeter_rel": 0.0,
                "largest_hole_rel": 0.0, "outer_rim_rel": 0.0, "boundary_edges": 0}
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components
    n = len(m.vertices)
    g = sp.coo_matrix((np.ones(len(bnd)), (bnd[:, 0], bnd[:, 1])), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    elen = np.linalg.norm(m.vertices[bnd[:, 0]] - m.vertices[bnd[:, 1]], axis=1)
    comp = lab[bnd[:, 0]]
    per = np.bincount(comp, weights=elen)
    per = np.sort(per[per > 0])[::-1]
    rim, holes = per[0], per[1:]
    return {
        "holes": int(len(holes)),
        "significant_holes": int((holes > 0.01 * diag).sum()),
        "hole_perimeter_rel": float(holes.sum() / diag),
        "largest_hole_rel": float(holes.max() / diag) if len(holes) else 0.0,
        "outer_rim_rel": float(rim / diag),
        "boundary_edges": int(len(bnd)),
    }


def landmark_accuracy(m, sfm_json: Path, sample: int = 20000) -> dict:
    """Distance of SfM landmarks to the mesh (landmarks: SfM frame → mesh frame)."""
    from app.services.sfm import mesh_to_sfm
    # Keep only coordinates, not millions of observation dictionaries.
    from array import array
    from app.services.photogrammetry.runtime import sfm_items
    coords = array('d')
    for landmark in sfm_items(Path(sfm_json), 'structure'):
        coords.extend(float(v) for v in landmark['X'])
    pts = np.frombuffer(coords, dtype=np.float64).reshape(-1, 3)
    if not len(pts):
        return {}
    diag = float(np.linalg.norm(m.bounds[1] - m.bounds[0]))
    lo, hi = m.bounds[0] - 0.02 * diag, m.bounds[1] + 0.02 * diag
    # AliceVision OBJ/glTF meshes have y,z negated vs the SfM frame; OpenMVS
    # output stays in the SfM frame — pick the flip that lands more landmarks
    # inside the mesh bbox (landmarks outside it are background: sky/trees)
    cands = [pts * np.array([1.0, -1.0, -1.0]), pts]
    inside = [np.all((c >= lo) & (c <= hi), axis=1) for c in cands]
    k = int(np.argmax([i.sum() for i in inside]))
    pts = cands[k][inside[k]]
    rng = np.random.default_rng(0)
    if len(pts) > sample:
        pts = pts[rng.choice(len(pts), sample, replace=False)]
    _, d, _ = m.nearest.on_surface(pts)
    return {
        "landmarks": int(len(pts)),
        "lm_median_rel": float(np.median(d) / diag),
        "lm_p90_rel": float(np.percentile(d, 90) / diag),
        "lm_within_0p1pct": float((d < 0.001 * diag).mean()),
    }


def reprojection_rms(sfm_json: Path) -> dict:
    """RMS reprojection error of the SfM (px) — bundle-adjustment quality."""
    from app.services import sfm as S
    from app.services.photogrammetry.runtime import sfm_header, sfm_items
    from itertools import islice
    data = sfm_header(Path(sfm_json))
    # cameras by viewId
    intr = {i["intrinsicId"]: i for i in data.get("intrinsics", [])}
    poses = {p["poseId"]: p["pose"]["transform"] for p in data.get("poses", [])}
    cams = {}
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
        else:                                   # AliceVision mm focal is scaleY
            fy = float(it["focalLength"]) / float(it.get("sensorWidth", 36.0)) * w
            fx = fy / float(it.get("pixelRatio", 1.0))
        pp = it.get("principalPoint", [0, 0])
        dp = [float(x) for x in it.get("distortionParams", [])[:3]] + [0.0] * 3
        cams[v["viewId"]] = S.Camera(v["path"], w, h, [float(x) for x in pose["rotation"]],
                                     [float(x) for x in pose["center"]], fx, fy,
                                     float(pp[0]), float(pp[1]), tuple(dp[:3]))
    errs = []
    for L in islice(sfm_items(Path(sfm_json), 'structure'), 40000):
        X = [float(v) for v in L["X"]]
        for ob in L.get("observations", []):
            cam = cams.get(ob.get("observationId"))
            if cam is None:
                continue
            uv = S._project_into_camera(cam, *X)
            if uv is None:
                continue
            x = [float(v) for v in ob["x"]]
            errs.append((uv[0] * cam.width - x[0]) ** 2 + (uv[1] * cam.height - x[1]) ** 2)
    if not errs:
        return {}
    e = np.sqrt(np.array(errs))
    return {"reproj_rms_px": float(np.sqrt(np.mean(e ** 2))), "reproj_median_px": float(np.median(e)),
            "views_registered": len(cams), "views_total": len(data.get("views", []))}


def roughness(m, radius_rel: float = 0.005, queries: int = 6000) -> dict:
    """Local surface noise: RMS distance of the surface to a fitted plane within a
    small radius (default 0.5 % of the diagonal), relative to that radius. Flat
    concrete should score near 0; a "hairy"/lumpy reconstruction scores high. The
    25th percentile ≈ the flattest quarter of the model (walls, pillar faces) —
    genuinely rough areas (vegetation, gravel) don't dominate it."""
    from scipy.spatial import cKDTree
    diag = float(np.linalg.norm(m.bounds[1] - m.bounds[0]))
    r = radius_rel * diag
    pts, _ = m.sample(400_000, return_index=True) if len(m.faces) else (np.zeros((0, 3)), None)
    tree = cKDTree(pts)
    rng = np.random.default_rng(0)
    q = pts[rng.choice(len(pts), min(queries, len(pts)), replace=False)]
    sig = []
    for i, nb in enumerate(tree.query_ball_point(q, r)):
        if len(nb) < 20:
            continue
        P = pts[nb] - pts[nb].mean(0)
        sig.append(np.sqrt(max(np.linalg.eigvalsh(P.T @ P / len(nb))[0], 0.0)))
    sig = np.array(sig) / r
    return {"rough_p25": float(np.percentile(sig, 25)), "rough_median": float(np.median(sig))}


def _components(m) -> int:
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components
    adj = m.face_adjacency
    n = len(m.faces)
    g = sp.coo_matrix((np.ones(len(adj)), (adj[:, 0], adj[:, 1])), shape=(n, n))
    return int(connected_components(g, directed=False)[0])


def evaluate(mesh_path: Path, sfm_json: Path | None = None) -> dict:
    m = _load(Path(mesh_path))
    out = {
        "faces": int(len(m.faces)), "vertices": int(len(m.vertices)),
        "components": _components(m),
    }
    out.update(hole_stats(m))
    out.update(roughness(m))
    if sfm_json and Path(sfm_json).exists():
        out.update(landmark_accuracy(m, sfm_json))
        out.update(reprojection_rms(sfm_json))
    return out


if __name__ == "__main__":
    r = evaluate(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
    for k, v in r.items():
        print(f"{k:22} {v:.5f}" if isinstance(v, float) else f"{k:22} {v}")
