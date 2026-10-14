"""
Photo-resolution texturing: every face gets its texture as a 1:1 crop of the
photo that sees it best — no resampling, so the model shows exactly the detail
of the photos (DJI at 2.5 m ≈ 0.9 mm/px).

OpenMVS' TextureMesh picks views by its own data term; measured on a drone
pillar it textured faces the closest photo sees at 0.6–1.0 mm with 2–4.7 mm
texels. Here the choice is explicit:

  1. visibility — every photo casts a ray per pixel (full resolution, Open3D):
     the first face hit is what that pixel sees (occlusion handled for free),
     and the number of pixels a face receives IS its resolution in that photo
  2. view choice — per face the photo with the most pixels, down-weighted for
     blurred photos; then a few smoothing passes switch a face to its
     neighbours' photo when that photo is nearly as good (fewer seams)
  3. atlas — connected faces with the same photo form a chart whose UVs are
     simply their pixel positions in that (undistorted) photo; charts are
     cropped 1:1 and shelf-packed into 8K pages
  4. consistency — per patch, a photo whose colour disagrees with the other
     photos (occluder, wrong surface through a mesh hole) is not used
  5. seams — a per-photo colour gain (least squares over faces two photos
     share) evens out exposures, then a smooth per-vertex correction field
     (Waechter-style global seam leveling) removes what is left at chart
     borders without flattening the photo's local contrast

Input: an OpenMVS/COLMAP work dir (images/ undistorted PINHOLE + sparse/
cameras.txt, images.txt) and a mesh in the SfM frame. Output: OBJ (app frame,
y,z negated) + MTL + texture pages.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

PAGE = 8192
PAD = 8                 # real photo context around each chart (filtering / mip levels sample it, not the neighbouring chart)


def _read_scene(work: Path):
    cams = {}
    for l in (work / "sparse/cameras.txt").read_text().splitlines():
        if l.strip() and not l.startswith("#"):
            p = l.split()
            cams[p[0]] = tuple(float(v) for v in p[2:8])            # w h fx fy cx cy
    lines = [l for l in (work / "sparse/images.txt").read_text().splitlines() if not l.startswith("#")]
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
        w, h, fx, fy, cx, cy = cams[p[8]]
        views.append({"name": p[9], "R": R, "t": np.array([tx, ty, tz]), "w": int(w), "h": int(h),
                      "K": np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])})
    return views


def _valid_mask(img_path: Path, margin: int = 12) -> np.ndarray:
    """Pixels inside the photo proper: undistortion leaves black borders, and the
    outermost pixels are the softest — never texture from there."""
    import cv2
    img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
    m = (img > 3).astype(np.uint8)
    m = cv2.erode(m, np.ones((2 * margin + 1, 2 * margin + 1), np.uint8))
    m[:margin] = 0; m[-margin:] = 0; m[:, :margin] = 0; m[:, -margin:] = 0
    return m.astype(bool)


def _sharpness(img_path: Path) -> float:
    import cv2
    img = cv2.imread(str(img_path), cv2.IMREAD_REDUCED_GRAYSCALE_2)
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0); gy = cv2.Sobel(img, cv2.CV_32F, 0, 1)
    g = np.hypot(gx, gy)
    lap = np.abs(cv2.Laplacian(img, cv2.CV_32F))
    s = g > np.percentile(g, 95)
    return float(lap[s].mean() / (g[s].mean() + 1e-6))


def visibility(V, F, views, stride: int = 1, log=print):
    """Sparse (face, view) → pixel count, from per-pixel ray casting."""
    import open3d as o3d
    import scipy.sparse as sp
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
    rows, cols, vals = [], [], []
    miss = np.iinfo(np.uint32).max
    for j, vw in enumerate(views):
        w, h = vw["w"], vw["h"]
        u, v = np.meshgrid(np.arange(0, w, stride) + 0.5, np.arange(0, h, stride) + 0.5)
        valid = vw["valid"][::stride, ::stride].ravel() if "valid" in vw else None
        Ki = np.linalg.inv(vw["K"])
        d = np.stack([u.ravel(), v.ravel(), np.ones(u.size)], 1) @ Ki.T
        d = d @ vw["R"]                                  # Rᵀ d (row form) → world
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        C = -vw["R"].T @ vw["t"]
        rays = np.hstack([np.broadcast_to(C, d.shape), d]).astype(np.float32)
        ids = scene.cast_rays(o3d.core.Tensor(rays))["primitive_ids"].numpy()
        if valid is not None:
            ids = ids[valid]
        ids = ids[ids != miss].astype(np.int64)
        cnt = np.bincount(ids, minlength=len(F))
        nz = np.nonzero(cnt)[0]
        rows.append(nz); cols.append(np.full(len(nz), j)); vals.append(cnt[nz] * stride * stride)
        if (j + 1) % 20 == 0:
            log(f"visibility {j + 1}/{len(views)}")
    S = sp.csr_matrix((np.concatenate(vals).astype(np.float32),
                       (np.concatenate(rows), np.concatenate(cols))), shape=(len(F), len(views)))
    return S


def choose_views(S, F, weights: np.ndarray, iters: int = 12, keep: float = 0.55):
    """Best view per face (pixels × photo sharpness weight) + seam smoothing."""
    import scipy.sparse as sp
    import trimesh
    W = S.multiply(weights[None, :]).tocsr()
    best = np.asarray(W.argmax(axis=1)).ravel()
    bestval = np.asarray(W.max(axis=1).todense()).ravel()
    seen = bestval > 0
    adj = trimesh.graph.face_adjacency(F)
    for _ in range(iters):
        # for each face, the label of each neighbour — switch to a neighbour's
        # photo when it keeps ≥ `keep` of the best score and ≥2 neighbours use it
        a, b = adj[:, 0], adj[:, 1]
        cand_f = np.concatenate([a, b]); cand_v = np.concatenate([best[b], best[a]])
        key = cand_f * S.shape[1] + cand_v
        uk, cnt = np.unique(key, return_counts=True)
        f_u, v_u = uk // S.shape[1], uk % S.shape[1]
        ok = cnt >= 2
        f_u, v_u, cnt = f_u[ok], v_u[ok], cnt[ok]
        sc = np.asarray(W[f_u, v_u]).ravel()
        good = (sc >= keep * bestval[f_u]) & (v_u != best[f_u])
        if not good.any():
            break
        # most-supported alternative per face
        order = np.lexsort((-cnt[good], f_u[good]))
        ff, vv = f_u[good][order], v_u[good][order]
        first = np.r_[True, ff[1:] != ff[:-1]]
        best[ff[first]] = vv[first]
    return best, seen


def patches(V, F, size: float) -> np.ndarray:
    """Face → patch id: voxel of the centroid × dominant normal direction."""
    import trimesh
    tm = trimesh.Trimesh(V, F, process=False)
    c = tm.triangles_center
    n = tm.face_normals
    ax = np.argmax(np.abs(n), 1) * 2 + (n[np.arange(len(n)), np.argmax(np.abs(n), 1)] > 0)
    key = np.c_[np.floor(c / size).astype(np.int64), ax]
    _, pid = np.unique(key, axis=0, return_inverse=True)
    return pid.ravel(), tm.area_faces


def choose_views_patches(S, F, weights, pid, area, min_cover: float = 0.5):
    """One photo per patch: most pixels among photos covering ≥ min_cover of its
    area; faces that photo misses fall back to their own best photo. (0.9 let a
    distant grazing photo win over a close one that misses a corner of the
    patch — blurred texture where the close photo has 8× the pixels.)"""
    import scipy.sparse as sp
    W = S.multiply(weights[None, :]).tocsr()
    n_p = pid.max() + 1
    P = sp.csr_matrix((np.ones(len(pid)), (pid, np.arange(len(pid)))), shape=(n_p, len(pid)))
    score = (P @ W).tocsr()                                   # patch × view pixels
    seen_area = (P @ sp.csr_matrix(((W > 0).multiply(area[:, None])))).tocsr()
    parea = np.asarray(P @ area).ravel()
    cover = seen_area.multiply(1.0 / np.maximum(parea, 1e-12)[:, None]).tocsr()
    ok = cover >= min_cover
    sc = score.multiply(ok).tocsr()
    pbest = np.asarray(sc.argmax(axis=1)).ravel()
    has = np.asarray(sc.max(axis=1).todense()).ravel() > 0
    pbest_any = np.asarray(score.argmax(axis=1)).ravel()
    pbest = np.where(has, pbest, pbest_any)
    labels = pbest[pid]
    fbest = np.asarray(W.argmax(axis=1)).ravel()
    lab_seen = np.asarray(W[np.arange(len(pid)), labels]).ravel() > 0
    seen = np.asarray(W.max(axis=1).todense()).ravel() > 0
    labels = np.where(lab_seen, labels, fbest)
    return labels, seen


def clean_labels(S, F, labels, seen, min_faces: int = 40, iters: int = 8, log=print,
                 V=None, views=None):
    """Tiny charts (a few faces from another photo inside a big chart) show as
    flecks: two photos never register to the pixel. Their faces take the photo
    most of their neighbours use, if that photo sees them (S > 0) — or, in a
    second pass (V, views given), if the face merely faces that camera inside
    its valid image area: a face hidden only by a millimetre bump of its own
    surface is better textured from the neighbours' photo than from another."""
    import trimesh
    adj = trimesh.graph.face_adjacency(F)
    a, b = np.r_[adj[:, 0], adj[:, 1]], np.r_[adj[:, 1], adj[:, 0]]       # face a, neighbour b
    Sc = S.tocsr()
    labels = labels.copy()
    n0 = None
    for it in range(iters):
        ch = _charts(F, labels, seen)
        size = np.bincount(ch)
        small = seen & (size[ch] < min_faces)
        if n0 is None:
            n0 = int(small.sum())
        sel = small[a] & seen[b] & (labels[b] != labels[a])
        fa, vb = a[sel], labels[b[sel]]
        if not len(fa):
            break                                    # nothing small left at all
        if it < iters // 2 or V is None:
            ok = np.asarray(Sc[fa, vb]).ravel() > 0
        else:
            ok = _faces_camera(V, F, fa, vb, views)
        fa, vb = fa[ok], vb[ok]
        if not len(fa):
            continue                                 # strict pass exhausted → relaxed pass
        key = fa.astype(np.int64) * S.shape[1] + vb
        uk, cnt = np.unique(key, return_counts=True)
        f_u, v_u = uk // S.shape[1], uk % S.shape[1]
        order = np.lexsort((-cnt, f_u))
        f_u, v_u = f_u[order], v_u[order]
        first = np.r_[True, f_u[1:] != f_u[:-1]]
        labels[f_u[first]] = v_u[first]
    ch = _charts(F, labels, seen)
    log(f"label cleanup: {n0} faces in charts < {min_faces} faces → "
        f"{int((seen & (np.bincount(ch)[ch] < min_faces)).sum())}")
    return labels


def _faces_camera(V, F, fa, vb, views, margin: int = 16):
    """Face fa front-facing camera vb and inside its valid image area."""
    ok = np.zeros(len(fa), bool)
    tri = V[F[fa]]
    c = tri.mean(1)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    for j in np.unique(vb):
        s = np.nonzero(vb == j)[0]
        vw = views[j]
        C = -vw["R"].T @ vw["t"]
        front = ((C - c[s]) * n[s]).sum(1) > 0
        pc = c[s] @ vw["R"].T + vw["t"]
        xy = pc @ vw["K"].T
        xy = xy[:, :2] / np.maximum(xy[:, 2:3], 1e-9)
        x, y = xy[:, 0].astype(int), xy[:, 1].astype(int)
        inside = (pc[:, 2] > 0) & (x >= margin) & (y >= margin) & (x < vw["w"] - margin) & (y < vw["h"] - margin)
        valid = np.zeros(len(s), bool)
        if "valid" in vw:
            valid[inside] = vw["valid"][y[inside], x[inside]]
        else:
            valid = inside
        ok[s] = front & valid
    return ok


def _charts(F, labels, seen):
    """Connected faces sharing a label → chart ids."""
    import scipy.sparse as sp
    import trimesh
    from scipy.sparse.csgraph import connected_components
    adj = trimesh.graph.face_adjacency(F)
    same = (labels[adj[:, 0]] == labels[adj[:, 1]]) & seen[adj[:, 0]] & seen[adj[:, 1]]
    a = adj[same]
    n = len(F)
    g = sp.coo_matrix((np.ones(len(a)), (a[:, 0], a[:, 1])), shape=(n, n))
    k, lab = connected_components(g, directed=False)
    return lab


def _shelf_pack(sizes: list[tuple[int, int]]):
    """Place (w,h) rects on PAGE×PAGE pages, tallest first. → [(page, x, y)]."""
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    pos = [None] * len(sizes)
    page, x, y, shelf_h = 0, 0, 0, 0
    for i in order:
        w, h = sizes[i]
        if x + w > PAGE:
            x, y, shelf_h = 0, y + shelf_h, 0
        if y + h > PAGE:
            page, x, y, shelf_h = page + 1, 0, 0, 0
        pos[i] = (page, x, y)
        x += w
        shelf_h = max(shelf_h, h)
    return pos, page + 1


def _color_gains(S, V, F, views, img_dir: Path, log=print, n_max: int = 60000, pid=None) -> np.ndarray:
    """Per-photo RGB gain so photos agree on the faces they share.
    For faces well seen (≥ 200 px) in several photos, the mean colour around the
    face centroid is sampled in each; gains g solve, in log space and least
    squares, log g_i + log c_fi = log g_j + log c_fj (plus Σ log g = 0)."""
    import cv2
    import scipy.sparse as sp
    from scipy.sparse.linalg import lsqr
    Sc = S.tocsr()
    good = np.nonzero(np.diff(Sc.indptr) >= 2)[0]
    rng = np.random.default_rng(0)
    if len(good) > n_max:
        good = rng.choice(good, n_max, replace=False)
    cent = V[F[good]].mean(1)
    Sg = Sc[good].tocsc()                              # column access per view
    obs = []                                           # (sample id, view, rgb)
    for j, vw in enumerate(views):
        rows = Sg.indices[Sg.indptr[j]:Sg.indptr[j + 1]]
        fs = rows[Sg.data[Sg.indptr[j]:Sg.indptr[j + 1]] >= 8]
        if not len(fs):
            continue
        img = cv2.imread(str(img_dir / vw["name"]), cv2.IMREAD_REDUCED_COLOR_4).astype(np.float32)
        img = cv2.blur(img, (5, 5))
        pc = cent[fs] @ vw["R"].T + vw["t"]
        px = (pc @ vw["K"].T)
        xy = px[:, :2] / px[:, 2:3] / 4.0
        x = np.clip(xy[:, 0].astype(int), 0, img.shape[1] - 1); y = np.clip(xy[:, 1].astype(int), 0, img.shape[0] - 1)
        rgb = img[y, x]
        ok = (rgb.min(1) > 8) & (rgb.max(1) < 247)            # skip black borders / clipped highlights
        obs.append((fs[ok], np.full(ok.sum(), j), rgb[ok]))
    if not obs:
        return np.ones((len(views), 3))
    f_all = np.concatenate([o[0] for o in obs]); v_all = np.concatenate([o[1] for o in obs])
    c_all = np.log(np.concatenate([o[2] for o in obs]) + 1.0)
    if pid is not None:                                # robust: mean log colour per (patch, view)
        pk = pid[good][f_all] * len(views) + v_all
        uk, inv = np.unique(pk, return_inverse=True)
        cs = np.zeros((len(uk), 3))
        np.add.at(cs, inv, c_all)
        cnt = np.bincount(inv, minlength=len(uk))[:, None]
        keep = cnt[:, 0] >= 5
        f_all = (uk // len(views))[keep]; v_all = (uk % len(views))[keep]; c_all = (cs / cnt)[keep]
    order = np.argsort(f_all, kind="stable")
    f_all, v_all, c_all = f_all[order], v_all[order], c_all[order]
    starts = np.r_[0, np.nonzero(np.diff(f_all))[0] + 1, len(f_all)]
    rows_i, rows_j, rhs = [], [], []
    for a, b in zip(starts[:-1], starts[1:]):
        if b - a < 2:
            continue
        # consecutive pairs within the face's observations
        for k in range(a, b - 1):
            rows_i.append(v_all[k]); rows_j.append(v_all[k + 1]); rhs.append(c_all[k + 1] - c_all[k])
    n = len(rows_i)
    if n == 0:
        return np.ones((len(views), 3))
    nv = len(views)
    A = sp.coo_matrix((np.r_[np.ones(n), -np.ones(n), np.full(nv, 0.1)],
                       (np.r_[np.arange(n), np.arange(n), np.full(nv, n)],
                        np.r_[rows_i, rows_j, np.arange(nv)])), shape=(n + 1, nv)).tocsr()
    rhs = np.array(rhs)
    gains = np.ones((nv, 3))
    for ch in range(3):
        b = np.r_[rhs[:, ch], 0.0]
        x = lsqr(A, b, damp=0.05)[0]
        gains[:, ch] = np.exp(x - np.median(x))
    gains = np.clip(gains, 0.4, 2.5)            # original photos: exposures differ by > 2×
    log(f"colour gains: {n} constraints, gain range {gains.min():.2f}–{gains.max():.2f}")
    return gains


def _photo_outliers(S, V, F, views, gains, img_dir: Path, log=print, thr: float = 0.3,
                    min_px: int = 8):
    """Photo-consistency check per (face, photo): the colour around the face
    centroid in each photo that sees it (≥ min_px pixels, per-photo gains
    applied); with ≥ 3 photos, one far from the median (log space, any
    channel) sees something else — an occluder, ground or sky past a fold of
    the mesh, a moved shadow. Its entry is removed from S so it is never
    chosen for that face."""
    import cv2
    import scipy.sparse as sp
    Sc = S.tocsc()
    cent = V[F].mean(1)
    fl, jl, cl, wl = [], [], [], []
    for j, vw in enumerate(views):
        rows = Sc.indices[Sc.indptr[j]:Sc.indptr[j + 1]]
        px_n = Sc.data[Sc.indptr[j]:Sc.indptr[j + 1]]
        fs = rows[px_n >= min_px]; px_n = px_n[px_n >= min_px]
        if not len(fs):
            continue
        img = cv2.imread(str(img_dir / vw["name"]), cv2.IMREAD_REDUCED_COLOR_4).astype(np.float32)
        # ~3 cm footprint: robust to the 1–2 px registration differences between
        # photos (high-contrast paint edges would otherwise look like outliers);
        # real occluders — gravel, sky, a fold of the mesh — are larger
        img = cv2.blur(img, (9, 9))
        pc = cent[fs] @ vw["R"].T + vw["t"]
        px = pc @ vw["K"].T
        xy = px[:, :2] / px[:, 2:3] / 4.0
        x = np.clip(xy[:, 0].astype(int), 0, img.shape[1] - 1); y = np.clip(xy[:, 1].astype(int), 0, img.shape[0] - 1)
        c = img[y, x]
        ok = (c.min(1) > 6) & (c.max(1) < 249)
        fl.append(fs[ok].astype(np.int32)); jl.append(np.full(ok.sum(), j, np.int16))
        cl.append(np.log(c[ok] * gains[j] + 1.0).astype(np.float32))
        wl.append(px_n[ok].astype(np.float32))
    if not fl:
        return S, None
    f = np.concatenate(fl); jv = np.concatenate(jl); c = np.concatenate(cl); wt = np.concatenate(wl)
    del fl, jl, cl, wl
    cnt = np.bincount(f, minlength=len(F))
    sel = cnt[f] >= 3
    f, jv, c, wt = f[sel], jv[sel], c[sel], wt[sel]
    order = np.argsort(f, kind="stable")
    f, jv, c, wt = f[order], jv[order], c[order], wt[order]
    starts = np.r_[0, np.nonzero(np.diff(f))[0] + 1]
    n = np.diff(np.r_[starts, len(f)])
    grp = np.repeat(np.arange(len(starts)), n)
    med = np.zeros((len(starts), 3), np.float32)
    for ch in range(3):                                  # pixel-weighted median per face
        o = np.lexsort((c[:, ch], grp))
        cw = np.cumsum(wt[o].astype(np.float64))
        before = np.r_[0.0, cw][starts]
        total = cw[starts + n - 1] - before
        k = np.searchsorted(cw, before + 0.5 * total)
        med[:, ch] = c[o, ch][np.minimum(k, len(cw) - 1)]
    cons = {"f": f, "j": jv, "c": c, "face_ids": f[starts], "med": med}
    dev = np.abs(c - med[grp]).max(1)
    bad = dev > thr
    if not bad.any():
        return S, cons
    B = sp.csr_matrix((np.ones(bad.sum(), np.float32), (f[bad], jv[bad].astype(np.int64))), shape=S.shape)
    S2 = (S - S.multiply(B > 0)).tocsr()
    S2.eliminate_zeros()
    log(f"outliers: {bad.sum()} of {len(f)} (face, photo) entries removed "
        f"({len(np.unique(f[bad]))} faces)")
    return S2, cons


def _lighting_weights(S, cons, sigma: float = 0.12, floor: float = 0.03):
    """Soft preference for photos that show a face in the CONSENSUS lighting.
    Photos taken minutes apart see a pillar face in sun or in shade; charts
    cut from both kinds side by side make a patchwork (white blobs on grey)
    that no smooth colour correction removes. Each (face, photo) weight is
    scaled by exp(−(Δ/σ)²), Δ = |log brightness − weighted median of all
    photos| (gain-corrected); faces without a colour sample keep weight 1."""
    import scipy.sparse as sp
    if cons is None:
        return S
    f, j, c = cons["f"], cons["j"].astype(np.int64), cons["c"]
    medf = np.zeros((S.shape[0], 3), np.float32)
    medf[cons["face_ids"]] = cons["med"]
    dev = np.abs(c.mean(1) - medf[f].mean(1))
    w = np.maximum(np.exp(-(dev / sigma) ** 2), floor).astype(np.float32)
    Sc = S.tocsr().astype(np.float32)
    Wm = sp.csr_matrix((w, (f, j)), shape=S.shape)
    has = sp.csr_matrix((np.ones(len(f), np.float32), (f, j)), shape=S.shape)
    out = Sc.multiply(Wm) + (Sc - Sc.multiply(has))      # weighted where sampled, else unchanged
    return out.tocsr()


def _capture_times(work: Path, views) -> np.ndarray | None:
    """Capture time (s) of each COLMAP view: work/sub_sfm.json lists the photos
    in export order (export_colmap names them 00001.jpg … in that order,
    skipping views without pose/intrinsics); time from EXIF DateTimeOriginal,
    else from a DJI_YYYYMMDDhhmmss file name."""
    import re
    from datetime import datetime
    try:
        sub = json.loads((Path(work) / "sub_sfm.json").read_text())
    except (OSError, ValueError):
        return None
    poses = {p["poseId"] for p in sub.get("poses", [])}
    intr = {i["intrinsicId"] for i in sub.get("intrinsics", [])}
    paths = [v["path"] for v in sub.get("views", []) if v.get("poseId") in poses and v.get("intrinsicId") in intr]
    if len(paths) != len(views):
        return None
    out = []
    for pth in paths:
        t = None
        try:
            from PIL import Image
            ex = Image.open(pth).getexif()
            dt = ex.get_ifd(0x8769).get(36867) or ex.get(306)
            if dt:
                t = datetime.strptime(str(dt).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            t = None
        if t is None:
            m = re.search(r"(20\d{12})", Path(pth).name)
            t = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").timestamp() if m else None
        if t is None:
            return None
        out.append(t)
    return np.array(out)


def _pass_weights(S, times: np.ndarray | None, gap_s: float = 600, other: float = 0.2, log=print):
    """A flight often visits a part twice (here 50 min apart): the sun moved,
    cast shadows sit elsewhere, and charts from both visits side by side make a
    patchwork. Photos are grouped into passes (gaps > gap_s); the pass that
    sees the region most (pixel total) is preferred, the others are scaled by
    `other` — still used wherever the preferred pass has no view."""
    import scipy.sparse as sp
    if times is None or len(times) < 2:
        return S
    order = np.argsort(times)
    cut = np.r_[0, np.cumsum(np.diff(times[order]) > gap_s)]
    pass_of = np.empty(len(times), int); pass_of[order] = cut
    n_pass = int(pass_of.max()) + 1
    if n_pass < 2:
        return S
    tot = np.asarray(S.sum(axis=0)).ravel()
    per = np.bincount(pass_of, weights=tot, minlength=n_pass)
    best = int(np.argmax(per))
    w = np.where(pass_of == best, 1.0, other).astype(np.float32)
    log(f"passes: {n_pass} (photos {np.bincount(pass_of).tolist()}), preferred pass {best} "
        f"({per[best] / max(per.sum(), 1):.0%} of pixels)")
    return (S.tocsr() @ sp.diags(w)).tocsr()


def _vertex_leveling(V, F, views, labels, chart_of_face, seen, gains, img_dir: Path, log=print,
                     smooth: float = 1.0, clip: float = 0.35, delta=None, data_w: float = float(__import__("os").environ.get("VISTA_LEVEL_DATA_W", "0.5"))):
    """Smooth colour correction field per chart (Waechter et al. 2014, global
    seam leveling, in log space): every (vertex, chart) pair gets an offset g;
    where charts meet, the two sides must agree (colour of the SAME surface
    point in two photos), inside a chart g varies smoothly (graph Laplacian).
    Seams vanish while each chart keeps its own photo's local contrast.
    Corners between differently lit faces (sun / shade) count little.
    → per face-corner correction factors (F, 3 corners, 3 BGR)."""
    import cv2
    import scipy.sparse as sp
    import trimesh
    from scipy.sparse.linalg import cg
    sf = np.nonzero(seen & (chart_of_face >= 0))[0]
    n_ch = int(chart_of_face.max()) + 1
    key = F[sf].astype(np.int64) * n_ch + chart_of_face[sf][:, None]          # (n,3)
    uk, var = np.unique(key.ravel(), return_inverse=True)
    var = var.reshape(-1, 3)
    nvar = len(uk)
    v_of = uk // n_ch
    # seam variables: vertices in ≥ 2 charts
    order = np.argsort(v_of, kind="stable")
    vs = v_of[order]
    first = np.r_[True, vs[1:] != vs[:-1]]
    grp_start = np.nonzero(first)[0]
    grp_len = np.diff(np.r_[grp_start, len(vs)])
    multi = np.repeat(grp_len >= 2, grp_len)
    seam_vars = order[multi]
    # chart normal at each seam variable (area-weighted normals of its faces)
    fn = trimesh.Trimesh(V, F, process=False).face_normals
    nrm = np.zeros((nvar, 3))
    for k in range(3):
        np.add.at(nrm, var[:, k], fn[sf])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    # colour of each seam variable in its chart's photo
    col = np.full((nvar, 3), np.nan)
    lab_of_var = np.zeros(nvar, np.int64)
    lab_of_var[var.ravel()] = np.repeat(labels[sf], 3)
    for j in np.unique(lab_of_var[seam_vars]):
        idx = seam_vars[lab_of_var[seam_vars] == j]
        vw = views[j]
        img = cv2.imread(str(img_dir / vw["name"]), cv2.IMREAD_REDUCED_COLOR_2).astype(np.float32)
        img = cv2.blur(img, (7, 7)) * gains[j][None, None, :]
        pc = V[v_of[idx]] @ vw["R"].T + vw["t"]
        px = pc @ vw["K"].T
        xy = px[:, :2] / px[:, 2:3] / 2.0
        x = np.clip(np.round(xy[:, 0]).astype(int), 0, img.shape[1] - 1)
        y = np.clip(np.round(xy[:, 1]).astype(int), 0, img.shape[0] - 1)
        col[idx] = img[y, x]
    lc = np.log(col + 1.0)
    # seam equations: consecutive variables of the same vertex
    sv = order[multi]
    same_next = np.r_[v_of[sv][1:] == v_of[sv][:-1], False]
    a, b = sv[same_next], sv[np.nonzero(same_next)[0] + 1]
    ok = np.isfinite(lc[a]).all(1) & np.isfinite(lc[b]).all(1) & (col[a].min(1) > 6) & (col[b].min(1) > 6) \
        & (col[a].max(1) < 249) & (col[b].max(1) < 249)
    a, b = a[ok], b[ok]
    d = lc[b] - lc[a]                                     # g_a − g_b = d
    w = np.where((nrm[a] * nrm[b]).sum(1) > 0.9, 1.0, 0.05)
    w = w / (1.0 + (np.abs(d).max(1) / 0.3) ** 2)        # big jumps: occluder / moved shadow
    # smoothness: edges inside a chart
    e = np.concatenate([var[:, [0, 1]], var[:, [1, 2]], var[:, [2, 0]]])
    e = np.unique(np.sort(e, 1), axis=0)
    ne, ns = len(e), len(a)
    rows = np.r_[np.arange(ns), np.arange(ns), ns + np.arange(ne), ns + np.arange(ne)]
    cols = np.r_[a, b, e[:, 0], e[:, 1]]
    vals = np.r_[w, -w, np.full(ne, smooth), np.full(ne, -smooth)]
    # data term (delta given): pull every chart towards the consensus colour of
    # all photos — removes sun/shade patchwork, keeps the chart's own detail
    nd = 0
    dv = np.zeros((0, 3))
    if delta is not None:
        sums = np.zeros((nvar, 3)); cnt = np.zeros(nvar)
        df = delta[sf]
        okf = np.isfinite(df).all(1)
        for k in range(3):
            np.add.at(sums, var[okf, k], df[okf]); np.add.at(cnt, var[okf, k], 1)
        has = np.nonzero(cnt > 0)[0]
        nd, dv = len(has), sums[has] / cnt[has, None]
        rows = np.r_[rows, ns + ne + np.arange(nd)]
        cols = np.r_[cols, has]
        vals = np.r_[vals, np.full(nd, data_w)]
        clip = max(clip, 1.0)
    A = sp.csr_matrix((vals, (rows, cols)), shape=(ns + ne + nd, nvar))
    L = (A.T @ A + sp.identity(nvar) * 1e-4).tocsr()
    diag = L.diagonal()
    M = sp.diags(1.0 / diag)
    g = np.zeros((nvar, 3))
    for ch in range(3):
        rhs = A.T @ np.r_[w * d[:, ch], np.zeros(ne), data_w * dv[:, ch]]
        x, info = cg(L, rhs, M=M, rtol=1e-6, maxiter=3000) if "rtol" in cg.__code__.co_varnames \
            else cg(L, rhs, M=M, tol=1e-6, maxiter=3000)
        g[:, ch] = x
    if delta is None:
        g -= np.median(g, axis=0)          # relative leveling only: keep overall exposure
    g = np.clip(g, -clip, clip)
    res_before = np.abs(d).mean()
    res_after = np.abs(d - (g[a] - g[b])).mean()
    log(f"vertex leveling: {nvar} vars, {ns} seam pairs, {ne} edges, {nd} consensus targets; "
        f"seam |Δlog| {res_before:.3f} → {res_after:.3f}")
    corr = np.ones((len(F), 3, 3), np.float32)
    corr[sf] = np.exp(g[var]).astype(np.float32)
    return corr


def _raster_corr(crop_h, crop_w, q, corr_f):
    """Per-pixel correction inside one chart crop: faces rasterized with their
    corner factors interpolated (barycentric); gaps take the nearest value."""
    import cv2
    from scipy import ndimage
    ids = np.full((crop_h, crop_w), -1, np.int32)
    pts = np.round((q - 0.5) * 16).astype(np.int32)
    for i in range(len(q)):
        cv2.fillConvexPoly(ids, pts[i], i, lineType=cv2.LINE_8, shift=4)
    ys, xs = np.nonzero(ids >= 0)
    if not len(ys):
        return np.broadcast_to(corr_f.mean((0, 1)), (crop_h, crop_w, 3)).astype(np.float32)
    f = ids[ys, xs]
    p = np.stack([xs + 0.5, ys + 0.5], 1)
    A, B, C = q[f, 0], q[f, 1], q[f, 2]
    v0, v1, v2 = B - A, C - A, p - A
    d00 = (v0 * v0).sum(1); d01 = (v0 * v1).sum(1); d11 = (v1 * v1).sum(1)
    d20 = (v2 * v0).sum(1); d21 = (v2 * v1).sum(1)
    den = d00 * d11 - d01 * d01
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    bv = (d11 * d20 - d01 * d21) / den
    bw = (d00 * d21 - d01 * d20) / den
    bu = 1 - bv - bw
    bc = np.clip(np.stack([bu, bv, bw], 1), 0, 1)
    bc /= np.maximum(bc.sum(1, keepdims=True), 1e-9)
    out = np.zeros((crop_h, crop_w, 3), np.float32)
    out[ys, xs] = (bc[:, :, None] * corr_f[f]).sum(1)
    empty = ids < 0
    if empty.any():
        _, (iy, ix) = ndimage.distance_transform_edt(empty, return_indices=True)
        out = out[iy, ix]
    return out


def bake(work: Path, mesh_ply: Path, out_dir: Path, stride: int = 1, patch_size: float = 0.25,
         images_dir: Path | None = None, log=print) -> dict:
    """images_dir: undistorted photos to texture from (same names/intrinsics as
    work/sparse); default work/images. The texture wants the ORIGINAL photos
    (no tone curve, no extra JPEG generation) — see hires.tex_images."""
    import cv2
    import trimesh
    t0 = time.time()
    work, out_dir = Path(work), Path(out_dir)
    img_dir = Path(images_dir) if images_dir else work / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    views = _read_scene(work)
    m = trimesh.load(mesh_ply, force="mesh", process=False)
    V, F = np.asarray(m.vertices, float), np.asarray(m.faces, np.int64)
    log(f"bake: {len(F)} faces, {len(views)} photos from {img_dir}")
    for vw in views:
        vw["valid"] = _valid_mask(img_dir / vw["name"])
    S = visibility(V, F, views, stride, log)
    sharp = np.array([_sharpness(img_dir / v["name"]) for v in views])
    # photo-level sharpness is content dependent: only a mild preference, so it
    # never outweighs the resolution of a closer photo
    wts = np.clip(sharp / np.median(sharp), 0.5, 1.2) ** 2
    pid, area = patches(V, F, patch_size)
    gains = _color_gains(S, V, F, views, img_dir, log, pid=pid)
    S, cons = _photo_outliers(S, V, F, views, gains, img_dir, log)
    S_sel = _lighting_weights(S, cons)                   # selection only; S stays pixel counts
    # (_pass_weights — prefer one flight pass — tested on the 2-pass DJI flight:
    #  texels 1,4 → 1,75 mm, twice the charts, neighbours chose different
    #  passes; off until a flight plan makes it worth it)
    labels, seen = choose_views_patches(S_sel, F, wts, pid, area)
    labels = clean_labels(S_sel, F, labels, seen, log=log, V=V, views=views)
    log(f"bake: {seen.mean():.1%} faces seen; views used {len(np.unique(labels[seen]))}")
    charts = _charts(F, labels, seen)

    # per face-corner pixel coords in its chosen photo
    uv_px = np.zeros((len(F), 3, 2))
    for j, vw in enumerate(views):
        fs = np.nonzero(seen & (labels == j))[0]
        if not len(fs):
            continue
        P = V[F[fs]].reshape(-1, 3)
        pc = P @ vw["R"].T + vw["t"]
        px = pc @ vw["K"].T
        uv_px[fs] = (px[:, :2] / px[:, 2:3]).reshape(-1, 3, 2)
    # chart rectangles (clipped to the photo), 1:1 crops
    order = np.argsort(charts[seen], kind="stable")
    seen_f = np.nonzero(seen)[0][order]
    ch_sorted = charts[seen_f]
    starts = np.r_[0, np.nonzero(np.diff(ch_sorted))[0] + 1, len(ch_sorted)]
    rects, sizes, chart_faces = [], [], []
    for s, e in zip(starts[:-1], starts[1:]):
        fs = seen_f[s:e]
        vw = views[labels[fs[0]]]
        p = uv_px[fs].reshape(-1, 2)
        x0, y0 = np.floor(p.min(0)).astype(int) - PAD
        x1, y1 = np.ceil(p.max(0)).astype(int) + PAD
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, vw["w"]), min(y1, vw["h"])
        w, h = max(x1 - x0, 1), max(y1 - y0, 1)
        if w > PAGE or h > PAGE:                      # cannot happen for ≤ 8K photos
            w, h = min(w, PAGE), min(h, PAGE)
        rects.append((labels[fs[0]], x0, y0, w, h)); sizes.append((w, h)); chart_faces.append(fs)
    pos, n_pages = _shelf_pack(sizes)
    log(f"bake: {len(rects)} charts on {n_pages} pages")
    pages = [np.full((PAGE, PAGE, 3), 128, np.uint8) for _ in range(n_pages)]
    by_view: dict[int, list] = {}
    for ci, (j, x0, y0, w, h) in enumerate(rects):
        by_view.setdefault(j, []).append(ci)
    uv = np.zeros((len(F), 3, 2))
    page_of_face = np.full(len(F), -1)
    chart_of_face = np.full(len(F), -1)
    for ci, fs in enumerate(chart_faces):
        chart_of_face[fs] = ci
    # consensus brightness target per face: weighted median of all photos minus
    # what the chosen photo shows there (both gain-corrected, log, ~3 cm scale)
    delta = None
    if cons is not None:
        mine = cons["j"].astype(np.int64) == labels[cons["f"]]
        medf = np.full((len(F), 3), np.nan, np.float32)
        medf[cons["face_ids"]] = cons["med"]
        delta = np.full((len(F), 3), np.nan, np.float32)
        fm = cons["f"][mine]
        delta[fm] = medf[fm] - cons["c"][mine]
    corr = _vertex_leveling(V, F, views, labels, chart_of_face, seen, gains, img_dir, log, delta=delta)
    for j, cis in by_view.items():
        img0 = cv2.imread(str(img_dir / views[j]["name"]), cv2.IMREAD_COLOR).astype(np.float32)
        img0 *= gains[j][None, None, :]                          # gains are in BGR like the image
        for ci in cis:
            _, x0, y0, w, h = rects[ci]
            pg, ax, ay = pos[ci]
            fs = chart_faces[ci]
            c = _raster_corr(h, w, uv_px[fs] - [x0, y0], corr[fs])
            crop = img0[y0:y0 + h, x0:x0 + w] * c
            pages[pg][ay:ay + h, ax:ax + w] = np.clip(crop + 0.5, 0, 255).astype(np.uint8)
            q = uv_px[fs] - [x0, y0] + [ax, ay]
            uv[fs] = np.stack([q[..., 0] / PAGE, 1.0 - q[..., 1] / PAGE], -1)
            page_of_face[fs] = pg
    # unseen faces (no photo saw them) → a reserved neutral-grey block. NOT a
    # page corner: the shelf packer puts the first chart at (0,0), so a corner
    # texel showed a random dark photo pixel on every unseen face.
    if (~seen).any():
        last = n_pages - 1
        used_h = max((pos[ci][2] + sizes[ci][1] for ci in range(len(rects)) if pos[ci][0] == last), default=0)
        if used_h > PAGE - 8:                      # last page full → one more page
            pages.append(np.full((PAGE, PAGE, 3), 128, np.uint8)); n_pages += 1; last += 1
        pages[last][PAGE - 8:, PAGE - 8:] = 128
        uv[~seen] = [(PAGE - 4) / PAGE, 1 - (PAGE - 4) / PAGE]
        page_of_face[~seen] = last
    for pg, im in enumerate(pages):
        cv2.imwrite(str(out_dir / f"texture_{1001 + pg}.jpg"), im, [cv2.IMWRITE_JPEG_QUALITY, 95])
    _write_obj(out_dir, V, F, uv, page_of_face, n_pages)
    # per-face photo choice (+ its pixel coords) for diagnostics
    np.savez_compressed(out_dir / "bake_faces.npz", label=np.where(seen, labels, -1).astype(np.int16),
                        uv_px=uv_px.astype(np.float32), names=np.array([v["name"] for v in views]))
    # texel density actually achieved (area-weighted)
    area = m.area_faces
    pix = np.asarray(S[np.arange(len(F)), labels]).ravel()
    ok = seen & (area > 0)
    dens = pix[ok] / area[ok]
    o = np.argsort(dens); cw = np.cumsum(area[ok][o]) / area[ok].sum()
    q = {f"texel_q{int(p * 100)}_units": float(1 / math.sqrt(max(dens[o][np.searchsorted(cw, p)], 1e-12)))
         for p in (0.1, 0.5, 0.9)}
    st = {"faces": int(len(F)), "seen": float(seen.mean()), "charts": len(rects), "pages": n_pages,
          "views_used": int(len(np.unique(labels[seen]))), "seconds": round(time.time() - t0), **q}
    (out_dir / "bake.json").write_text(json.dumps(st, indent=1))
    log(f"bake: done {st}")
    return st


def _write_obj(out_dir: Path, V, F, uv, page_of_face, n_pages):
    """OBJ in the app frame (y,z negated) with one material per page."""
    with open(out_dir / "texturedMesh.mtl", "w") as f:
        for pg in range(n_pages):
            f.write(f"newmtl material_{pg}\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nillum 1\nmap_Kd texture_{1001 + pg}.jpg\n\n")
    Va = V * [1, -1, -1]
    with open(out_dir / "texturedMesh.obj", "w") as f:
        f.write("mtllib texturedMesh.mtl\n")
        f.write("".join(f"v {x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in Va))
        uvf = uv.reshape(-1, 2)
        f.write("".join(f"vt {u:.7f} {v:.7f}\n" for u, v in uvf))
        for pg in range(n_pages):
            fs = np.nonzero(page_of_face == pg)[0]
            if not len(fs):
                continue
            f.write(f"usemtl material_{pg}\n")
            vi = F[fs] + 1
            ti = fs[:, None] * 3 + np.arange(3)[None, :] + 1
            f.write("".join(f"f {a}/{ta} {b}/{tb} {c}/{tc}\n"
                            for (a, b, c), (ta, tb, tc) in zip(vi, ti)))
