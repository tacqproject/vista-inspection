"""
Mesh editing: crop the reconstructed model to a user-drawn region of interest.

The user draws a polygon on the model surface in the viewer. We keep only the
faces whose centroid projects inside that polygon (extruded infinitely along
the polygon's normal) and drop everything else — trees, neighboring buildings,
reconstruction debris.

Works directly on the OBJ text so UVs, materials and textures survive intact;
unused vertices are pruned later by gltfpack during LOD generation.
"""

import numpy as np
from pathlib import Path


def _polygon_basis(points: np.ndarray):
    """Newell normal + in-plane (u, v) basis for the polygon."""
    n = np.zeros(3)
    for i in range(len(points)):
        a, b = points[i], points[(i + 1) % len(points)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    norm = np.linalg.norm(n)
    if norm < 1e-12:
        raise ValueError("Degenerovaný polygon")
    n /= norm
    u = points[1] - points[0]
    u -= n * (u @ n)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return points[0], u, v


def _points_in_polygon(pts2: np.ndarray, poly2: np.ndarray) -> np.ndarray:
    """Vectorized even-odd point-in-polygon test."""
    inside = np.zeros(len(pts2), dtype=bool)
    x, y = pts2[:, 0], pts2[:, 1]
    n = len(poly2)
    for i in range(n):
        x1, y1 = poly2[i]
        x2, y2 = poly2[(i + 1) % n]
        crosses = ((y1 > y) != (y2 > y)) & (
            x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-30) + x1
        )
        inside ^= crosses
    return inside


def crop_obj(obj_path: Path, polygon: list[dict], out_path: Path) -> int:
    """
    Write a copy of the OBJ keeping only faces inside the polygon prism.
    polygon: [{x, y, z}, ...] in model coordinates (≥ 3 points).
    Returns the number of faces kept.
    """
    poly = np.array([[p["x"], p["y"], p["z"]] for p in polygon], dtype=np.float64)
    origin, u_ax, v_ax = _polygon_basis(poly)
    poly2 = np.stack([(poly - origin) @ u_ax, (poly - origin) @ v_ax], axis=1)

    # Pass 1: vertex positions
    verts: list[list[float]] = []
    with open(obj_path) as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                verts.append([float(p[1]), float(p[2]), float(p[3])])
    V = np.array(verts)
    rel = V - origin
    V2 = np.stack([rel @ u_ax, rel @ v_ax], axis=1)
    vert_inside = _points_in_polygon(V2, poly2)

    # Pass 2: rewrite, keeping faces with at least one vertex inside
    kept = 0
    with open(obj_path) as fin, open(out_path, "w") as fout:
        for line in fin:
            if line.startswith("f "):
                idxs = [int(tok.split("/")[0]) - 1 for tok in line.split()[1:]]
                if any(vert_inside[i] for i in idxs):
                    fout.write(line)
                    kept += 1
            else:
                fout.write(line)

    return kept
