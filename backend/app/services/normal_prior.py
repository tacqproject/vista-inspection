"""
AI-guided mesh denoising: monocular surface normals (DSINE, CVPR 2024) predicted
for every source photo steer the removal of multi-view-stereo noise.

Depth-map MVS on weakly textured concrete produces "hairy", lumpy walls: the
depth noise is high where the photo has little texture. A normal network sees
the same surface as clean, piecewise-flat planes with crisp edges — exactly the
prior these structures need — while the MVS mesh supplies correct *position*.

  1. every photo's normal map is ray-cast onto the mesh (visibility for free) and
     the predicted world normals are averaged per face, weighted by viewing angle
     and agreement with the current mesh (occlusion edges / wrong predictions of
     one view are outvoted by the others);
  2. guided normal filtering: face normals are smoothed over edge neighbours with
     range weights taken from the AI normals — noise is averaged away within a
     planar region, never across a real edge the network sees;
  3. vertices are moved to fit the filtered normals (Sun et al. 2007 update),
     with the displacement clamped so no measured point moves far.

Frames: mesh and COLMAP scene in the SfM frame (OpenMVS work dir convention).
CLI: python -m app.services.normal_prior <in.ply> <out.ply> <scene dir> <normals dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


def _read_colmap(scene: Path):
    cams = {}
    for l in (scene / "sparse/cameras.txt").read_text().splitlines():
        if l.strip() and not l.startswith("#"):
            p = l.split()
            cams[p[0]] = [float(v) for v in p[2:8]]           # w h fx fy cx cy
    lines = [l for l in (scene / "sparse/images.txt").read_text().splitlines() if not l.startswith("#")]
    views = []
    for i in range(0, len(lines), 2):
        p = lines[i].split()
        if len(p) < 10:
            continue
        qw, qx, qy, qz, tx, ty, tz = map(float, p[1:8])
        R = np.array([
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
            [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
            [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]])
        views.append({"name": p[9], "R": R, "t": np.array([tx, ty, tz]), "cam": cams[p[8]]})
    return views


def _face_normals(V, F):
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    a = np.linalg.norm(n, axis=1)
    return n / np.maximum(a, 1e-20)[:, None], a / 2


def _unit(x):
    return x / np.maximum(np.linalg.norm(x, axis=1), 1e-12)[:, None]


# DSINE's camera normals are the negated OpenCV frame (measured: of all 8 axis
# sign combinations only this one agrees with the mesh, median cos 0.96 vs
# heavily smoothed mesh normals). Checked again at run time.
CV_FROM_DSINE = np.array([-1.0, -1.0, -1.0])


def accumulate_prior(V, F, views, normals_dir: Path, stride: int = 1, log=print):
    """Per-face average of the predicted normals of all photos that see the face."""
    import open3d as o3d
    FN, _ = _face_normals(V, F)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
    acc = np.zeros((len(F), 3))
    wsum = np.zeros(len(F))
    signs = []
    for vw in views:
        f = Path(normals_dir) / (Path(vw["name"]).stem + ".npy")
        if not f.exists():
            continue
        nm = np.load(f).astype(np.float32)[:, ::stride, ::stride]          # 3,H,W
        H, W = nm.shape[1:]
        w0, h0, fx, fy, cx, cy = vw["cam"]
        s = W / w0
        u, v = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
        d = np.stack([(u / s - cx) / fx, (v / s - cy) / fy, np.ones_like(u)], -1).reshape(-1, 3)
        R, t = vw["R"], vw["t"]
        C = -R.T @ t
        dw = _unit(d @ R)                                                  # R^T d, row form
        rays = np.hstack([np.broadcast_to(C, dw.shape), dw]).astype(np.float32)
        hit = scene.cast_rays(o3d.core.Tensor(rays))
        fid = hit["primitive_ids"].numpy().astype(np.int64)
        ok = fid != np.iinfo(np.uint32).max
        fid = fid[ok]
        ncv = nm.reshape(3, -1).T[ok] * CV_FROM_DSINE
        nw = _unit(ncv @ R)                                                # to world
        # orientation of the mesh face towards this camera (meshes may be inward)
        fn = FN[fid] * np.sign(-(FN[fid] * dw[ok]).sum(1))[:, None]
        cos_mesh = (fn * nw).sum(1)
        signs.append(np.median(cos_mesh))
        view_cos = np.clip(-(fn * dw[ok]).sum(1), 0, 1)
        wgt = view_cos * np.clip(cos_mesh, 0, 1) ** 2                      # agree + not grazing
        # predicted normal expressed with the face's own orientation
        nw = nw * np.sign((FN[fid] * fn).sum(1))[:, None]
        for k in range(3):
            acc[:, k] += np.bincount(fid, weights=wgt * nw[:, k], minlength=len(F))
        wsum += np.bincount(fid, weights=wgt, minlength=len(F))
    log(f"prior: {len(signs)} views, median cos(mesh, AI) {np.median(signs):.3f}, "
        f"faces covered {np.mean(wsum > 0):.1%}")
    return acc, wsum, float(np.median(signs)) if signs else 0.0


def _edge_adjacency(F):
    import trimesh
    import scipy.sparse as sp
    adj = trimesh.graph.face_adjacency(F)
    n = len(F)
    return sp.coo_matrix((np.ones(len(adj) * 2), (np.r_[adj[:, 0], adj[:, 1]], np.r_[adj[:, 1], adj[:, 0]])),
                         shape=(n, n)).tocsr()


def denoise(V, F, prior_acc, prior_w, filter_iters=8, sigma_deg=20.0, prior_blend=0.5,
            vertex_iters=20, max_disp=None, log=print):
    import scipy.sparse as sp
    FN, A = _face_normals(V, F)
    has = prior_w > 0
    G = FN.copy()
    G[has] = _unit(prior_acc[has])
    # prior faces: blend AI normal with the mesh normal (keeps real sub-pixel detail)
    target = FN.copy()
    target[has] = _unit(prior_blend * G[has] + (1 - prior_blend) * FN[has])
    Adj = _edge_adjacency(F)
    rows, cols = Adj.nonzero()
    sig = 2 * (1 - np.cos(np.deg2rad(sigma_deg)))
    N = target.copy()
    for _ in range(filter_iters):
        # range weight from the guidance (AI) normals: no smoothing across edges
        wr = np.exp(-((G[rows] - G[cols]) ** 2).sum(1) / sig) * A[cols]
        Wm = sp.coo_matrix((wr, (rows, cols)), shape=Adj.shape).tocsr()
        N = _unit(Wm @ N + A[:, None] * N)
    # vertex update: x_i += 1/|F_i| Σ n_f (n_f · (c_f − x_i))
    nv, nf = len(V), len(F)
    Pj = [sp.csr_matrix((np.ones(nf), (F[:, j], np.arange(nf))), shape=(nv, nf)) for j in range(3)]
    deg = np.maximum(np.bincount(F.ravel(), minlength=nv), 1)[:, None]
    X = V.copy()
    for _ in range(vertex_iters):
        c = (X[F[:, 0]] + X[F[:, 1]] + X[F[:, 2]]) / 3
        acc = sum(P @ (np.einsum("ij,ij->i", c - X[F[:, j]], N)[:, None] * N) for j, P in enumerate(Pj))
        X = X + acc / deg
    disp = np.linalg.norm(X - V, axis=1)
    if max_disp is not None:
        over = disp > max_disp
        X[over] = V[over] + (X[over] - V[over]) * (max_disp / disp[over])[:, None]
    log(f"vertex displacement: median {np.median(disp):.2e}, p99 {np.percentile(disp, 99):.2e}"
        + (f", clamped {np.mean(disp > max_disp):.2%}" if max_disp else ""))
    return X


# Chosen on the pillar test set (61 photos, shared SfM, OpenMVS mesh "O1"):
#                      flat-area noise p25 / median   landmark deviation
#   none                  0.0168 / 0.0308               0.323 ‰
#   light  (0.7, 60)      0.0046 / 0.0110               0.327 ‰
#   this   (0.9, 100)     0.0030 / 0.0075               0.333 ‰
#   strong (1.0, 300)     0.0025 / 0.0055               0.354 ‰
# (RealityScan on the full set: 0.0 / 0.0165). "strong" starts to flatten real
# relief (+10 % deviation); this keeps −80 % noise at +3 %.
PRESET = dict(prior_blend=0.9, filter_iters=20, vertex_iters=100, max_disp_rel=0.005)


def run(mesh_in, mesh_out, scene_dir, normals_dir, stride=1, log=print, **kw):
    kw = {**PRESET, **kw}
    max_disp_rel = kw.pop("max_disp_rel")
    import trimesh
    m = trimesh.load(mesh_in, force="mesh", process=False)
    V, F = np.asarray(m.vertices, dtype=np.float64), np.asarray(m.faces, dtype=np.int64)
    views = _read_colmap(Path(scene_dir))
    acc, w, med = accumulate_prior(V, F, views, Path(normals_dir), stride, log)
    if med < 0.3:
        raise RuntimeError(f"AI normály nesouhlasí se sítí (medián cos {med:.2f}) — špatná konvence?")
    diag = float(np.linalg.norm(V.max(0) - V.min(0)))
    X = denoise(V, F, acc, w, max_disp=max_disp_rel * diag, log=log, **kw)
    trimesh.Trimesh(X, F, process=False).export(mesh_out)
    return {"covered": float(np.mean(w > 0)), "agreement": med}


if __name__ == "__main__":
    import json
    a = sys.argv[1:]
    kw = json.loads(a[4]) if len(a) > 4 else {}
    print(json.dumps(run(a[0], a[1], a[2], a[3], **kw)))
