"""
Hierarchical streaming tiles (OGC 3D Tiles 1.1, glb content) for big models.

The viewer used to download the full-resolution mesh AND all 8K texture atlases
up front (~130 MB, ~2.8 GB of decoded GPU textures for a large scan), even when
the model was zoomed out. Here the model is cut into a kd-tree of tiles:

  • leaves  — the ORIGINAL full-resolution triangles,
  • parents — the same region simplified (quadric, cut boundaries preserved so
              neighbouring tiles of ANY level meet without cracks),

and every tile gets its OWN re-baked texture (xatlas UVs + bake from the source
atlases): leaves at the original texel density, parents proportionally coarser
(sampled from a mip pyramid, no aliasing). The client (3d-tiles-renderer) then
loads only the coarse root when zoomed out and streams full-resolution leaves
just for the part being looked at.

Frames: tiles are always built from the PRISTINE geometry (`*.orig` if the model
has been baked into a structure's canonical frame). The bake is expressed as the
tileset root `transform`, re-synced by `sync_tileset()` after every bake/restore,
so tiles never have to be rebuilt for a re-bake. `asset.gltfUpAxis = "Z"` tells
the renderer NOT to apply the Y-up→Z-up content rotation, so tiles render in the
exact same frame as the model_lod*.gltf files (and all the annotations/cameras).

Run as a separate process (heavy: pymeshlab, xatlas, embree, ~2 GB of atlases):
    python -m app.services.tiles build <model_lod0.gltf>
    python -m app.services.tiles all            # every inspection lacking tiles
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ─── Tunables ────────────────────────────────────────────────────────────────
# Small tiles stream smoothly on tablets: a 4096² texture is 64 MB decoded and
# a 150k-triangle tile costs a visible stall to parse/upload on an iPad; 2048² /
# 60k keeps every tile cheap (the tree is just one level deeper) and needs no
# client-side downsizing.
TEX_MAX = 2048            # max texture side of one tile
TEX_MIN = 256
LEAF_MAX_FACES = 60_000   # leaf split threshold (original triangles)
NODE_FACES = 60_000       # face budget of a simplified (parent) tile
PACK_EFF = 0.72           # expected xatlas packing efficiency (with padding)
PADDING = 4               # texel gutter between charts (mip bleeding)
JPEG_QUALITY = 88
# Leaves copy the source atlas 1:1 (UV islands cut out and repacked, no
# resampling): a photo-resolution atlas has a texel size that varies per face
# (0.6–4 mm on a drone pillar) and any uniform re-bake would blur its finest
# parts. The tree then splits by the texels a leaf really needs.
COPY_LEAVES = True
COPY_OVERHEAD = 1.5       # island bounding boxes + shelf packing vs. exact texel area
COPY_MARGIN = 6           # source texels kept around each island (filtering, mips)
LEAF_JPEG_QUALITY = 92
# geometricError of a parent = TEXEL_ERR × its world texel size (or measured
# surface deviation, if larger). With the viewer's errorTarget ≈ 6 px a parent is
# refined once one of its texels would cover > ~1.5 screen px.
TEXEL_ERR = 4.0
TILES_VERSION = 2


# ─── Source model ────────────────────────────────────────────────────────────

_COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
         5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _read_accessor(g: dict, buf: bytes, idx: int) -> np.ndarray:
    acc = g["accessors"][idx]
    bv = g["bufferViews"][acc["bufferView"]]
    dt = np.dtype(_COMP[acc["componentType"]]).newbyteorder("<")
    n, k = acc["count"], _NCOMP[acc["type"]]
    off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or dt.itemsize * k
    if stride == dt.itemsize * k:
        arr = np.frombuffer(buf, dtype=dt, count=n * k, offset=off)
    else:  # interleaved
        raw = np.frombuffer(buf, dtype=np.uint8, count=stride * (n - 1) + dt.itemsize * k, offset=off)
        arr = np.lib.stride_tricks.as_strided(
            raw, shape=(n, dt.itemsize * k), strides=(stride, 1)).copy().view(dt)
    return arr.reshape(n, k) if k > 1 else arr.reshape(n)


@dataclass
class Source:
    V: np.ndarray          # (n,3) float64 vertex positions (mesh frame)
    UV: np.ndarray         # (n,2) float64 texcoords (glTF: v=0 is the image top)
    F: np.ndarray          # (m,3) int64 faces → V
    mat: np.ndarray        # (m,) int   atlas index per face
    atlases: list[Path]    # atlas image per material index
    area: np.ndarray = field(default=None)      # (m,) 3D face area
    cent: np.ndarray = field(default=None)      # (m,3) face centroids
    density: float = 0.0   # original texels per world unit²
    texels: np.ndarray = field(default=None)    # (m,) source texels per face (1:1 leaves)


def load_source(gltf_path: Path) -> Source:
    gltf_path = Path(gltf_path)
    g = json.loads(gltf_path.read_text())
    bufs = g.get("buffers", [])
    if len(bufs) != 1 or "uri" not in bufs[0]:
        raise ValueError("expected a single external .bin buffer")
    buf = (gltf_path.parent / bufs[0]["uri"]).read_bytes()

    img_of_mat: list[Path] = []
    for m in g.get("materials", []):
        t = m.get("pbrMetallicRoughness", {}).get("baseColorTexture", {}).get("index")
        uri = g["images"][g["textures"][t]["source"]]["uri"] if t is not None else None
        img_of_mat.append(gltf_path.parent / uri if uri else None)

    Vs, UVs, Fs, Ms, base = [], [], [], [], 0
    for mesh in g["meshes"]:
        for pr in mesh["primitives"]:
            if pr.get("mode", 4) != 4:
                continue
            P = _read_accessor(g, buf, pr["attributes"]["POSITION"]).astype(np.float64)
            T = (_read_accessor(g, buf, pr["attributes"]["TEXCOORD_0"]).astype(np.float64)
                 if "TEXCOORD_0" in pr["attributes"] else np.zeros((len(P), 2)))
            I = (_read_accessor(g, buf, pr["indices"]).astype(np.int64)
                 if "indices" in pr else np.arange(len(P), dtype=np.int64))
            Vs.append(P); UVs.append(T); Fs.append(I.reshape(-1, 3) + base)
            Ms.append(np.full(len(I) // 3, pr.get("material", 0), np.int32))
            base += len(P)
    src = Source(V=np.concatenate(Vs), UV=np.concatenate(UVs), F=np.concatenate(Fs),
                 mat=np.concatenate(Ms), atlases=img_of_mat)
    tri = src.V[src.F]
    src.area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    src.cent = tri.mean(axis=1)
    return src


def _measure_density(src: Source, sizes: dict[int, tuple[int, int]]) -> float:
    """Original texel density (texels per world unit²), area-weighted over faces
    with a sane mapping — this is the detail level the leaves must preserve."""
    uv = src.UV[src.F]
    e1, e2 = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    a_uv = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])   # 2D cross
    wh = np.array([sizes[m][0] * sizes[m][1] if m in sizes else 0 for m in range(len(src.atlases))], float)
    texels = a_uv * wh[src.mat]
    src.texels = texels
    ok = (src.area > 0) & (texels > 0)
    d = texels[ok] / src.area[ok]
    lo, hi = np.percentile(d, [2, 98])
    sel = (d >= lo) & (d <= hi)
    return float(texels[ok][sel].sum() / src.area[ok][sel].sum())


# ─── Tree ────────────────────────────────────────────────────────────────────

@dataclass
class Node:
    id: int
    faces: np.ndarray            # original face indices in this region
    depth: int
    children: list["Node"] = field(default_factory=list)
    error: float = 0.0
    lo: np.ndarray = None
    hi: np.ndarray = None

    @property
    def leaf(self) -> bool:
        return not self.children


def build_tree(src: Source) -> Node:
    counter = [0]
    budget = TEX_MAX * TEX_MAX * PACK_EFF

    def make(faces: np.ndarray, depth: int) -> Node:
        node = Node(counter[0], faces, depth)
        counter[0] += 1
        area = src.area[faces].sum()
        need = area * src.density
        if COPY_LEAVES and src.texels is not None:
            need = max(need, float(src.texels[faces].sum()) * COPY_OVERHEAD)
        if (len(faces) <= LEAF_MAX_FACES and need <= budget) or len(faces) < 2000 or depth > 24:
            return node
        c = src.cent[faces]
        axis = int(np.argmax(c.max(0) - c.min(0)))
        order = np.argsort(c[:, axis], kind="stable")
        cum = np.cumsum(src.area[faces][order])
        k = int(np.searchsorted(cum, cum[-1] / 2))          # area-balanced split
        k = min(max(k, 1), len(faces) - 1)
        node.children = [make(faces[order[:k]], depth + 1), make(faces[order[k:]], depth + 1)]
        return node

    return make(np.arange(len(src.F)), 0)


def _walk(n: Node):
    yield n
    for c in n.children:
        yield from _walk(c)


# ─── Worker-side state (populated in the parent before fork → shared COW) ─────

_SRC: Source | None = None
_ATLAS_PATHS: list = []              # atlas image per material (None = missing)
_ATLAS_SIZE: dict = {}               # material → (w, h) of the full-size atlas
_ATLAS_CACHE = None                    # per process OrderedDict: (material, level) → BGR uint8
ATLAS_CACHE_BYTES = 1.5e9            # per worker; a whole bridge has 65 × 8K atlases (17 GB with mips)
_OUT: Path | None = None


def _atlas(m: int, level: int) -> np.ndarray:
    """Atlas m at mip level (0 = full size), loaded on demand into a per-process
    LRU cache — preloading every atlas (and its mips) in the parent took
    17 GB for a whole bridge and, with the workers, ran out of memory.
    Levels 1–3 decode directly at reduced size (JPEG DCT scaling)."""
    import cv2
    from collections import OrderedDict
    global _ATLAS_CACHE
    if _ATLAS_CACHE is None:
        _ATLAS_CACHE = OrderedDict()
    key = (m, level)
    img = _ATLAS_CACHE.get(key)
    if img is not None:
        _ATLAS_CACHE.move_to_end(key)
        return img
    p = _ATLAS_PATHS[m] if m < len(_ATLAS_PATHS) else None
    if p is None or not Path(p).exists():
        img = np.full((4, 4, 3), 160, np.uint8)
    else:
        flag = {0: cv2.IMREAD_COLOR, 1: cv2.IMREAD_REDUCED_COLOR_2,
                2: cv2.IMREAD_REDUCED_COLOR_4}.get(level, cv2.IMREAD_REDUCED_COLOR_8)
        img = cv2.imread(str(p), flag)
        for _ in range(max(0, level - 3)):
            h, w = img.shape[:2]
            if min(h, w) <= 64:
                break
            img = cv2.resize(img, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    _ATLAS_CACHE[key] = img
    total = sum(a.nbytes for a in _ATLAS_CACHE.values())
    while total > ATLAS_CACHE_BYTES and len(_ATLAS_CACHE) > 1:
        _, old = _ATLAS_CACHE.popitem(last=False)
        total -= old.nbytes
    return img


def _sample(uv: np.ndarray, mat: np.ndarray, level: int) -> np.ndarray:
    """Bilinear sample the source atlases (per-face material) at glTF uv."""
    out = np.zeros((len(uv), 3), np.float32)
    for m in np.unique(mat):
        sel = np.nonzero(mat == m)[0]
        lv = _atlas(int(m), level)
        h, w = lv.shape[:2]
        # AliceVision UDIM: atlas k's UVs live in u ∈ [k, k+1) and rely on the
        # glTF default REPEAT wrap — take the fractional part, like the GPU does
        u = uv[sel, 0] - np.floor(uv[sel, 0])
        v = uv[sel, 1] - np.floor(uv[sel, 1])
        x = np.clip(u * w - 0.5, 0, w - 1.001)
        y = np.clip(v * h - 0.5, 0, h - 1.001)
        x0 = x.astype(np.int64); y0 = y.astype(np.int64)
        fx = (x - x0)[:, None].astype(np.float32); fy = (y - y0)[:, None].astype(np.float32)
        x1 = np.minimum(x0 + 1, w - 1); y1 = np.minimum(y0 + 1, h - 1)
        a = lv[y0, x0].astype(np.float32); b = lv[y0, x1].astype(np.float32)
        c = lv[y1, x0].astype(np.float32); d = lv[y1, x1].astype(np.float32)
        out[sel] = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
    return out


def _bary(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Barycentric coordinates of 2D/3D points p in triangles (a,b,c), clamped."""
    v0, v1, v2 = b - a, c - a, p - a
    d00 = (v0 * v0).sum(1); d01 = (v0 * v1).sum(1); d11 = (v1 * v1).sum(1)
    d20 = (v2 * v0).sum(1); d21 = (v2 * v1).sum(1)
    den = d00 * d11 - d01 * d01
    den[np.abs(den) < 1e-30] = 1e-30
    v = (d11 * d20 - d01 * d21) / den
    w = (d00 * d21 - d01 * d20) / den
    bc = np.stack([1 - v - w, v, w], 1)
    bc = np.clip(bc, 0, 1)
    bc /= np.maximum(bc.sum(1, keepdims=True), 1e-12)
    return bc


def _weld(V: np.ndarray, F: np.ndarray):
    """Merge bit-identical positions (glTF splits vertices along UV seams)."""
    uniq, inv = np.unique(V, axis=0, return_inverse=True)
    return uniq, inv.reshape(-1)[F]


def _simplify(V: np.ndarray, F: np.ndarray, target: int):
    import pymeshlab
    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(vertex_matrix=V, face_matrix=F.astype(np.int32)))
    ms.meshing_remove_duplicate_faces()
    ms.meshing_decimation_quadric_edge_collapse(
        targetfacenum=int(target), preserveboundary=True, boundaryweight=2.0,
        preservenormal=True, preservetopology=False, optimalplacement=True,
        planarquadric=True, qualitythr=0.3, autoclean=True,
    )
    m = ms.current_mesh()
    return m.vertex_matrix().astype(np.float64), m.face_matrix().astype(np.int64)


def _ray(faces: np.ndarray):
    """Intersector over the node's OWN source faces (a parent tile simplifies
    exactly those): a per-worker accelerator over the whole source mesh cost
    ≈ 6 GB per worker for a 27 M-triangle bridge."""
    import trimesh
    return trimesh.Trimesh(_SRC.V, _SRC.F[faces], process=False).ray


def _copy_leaf(job: dict):
    """Leaf texture as a 1:1 copy of the source atlas: the leaf's UV islands
    (faces connected through shared glTF vertices = same UVs) are cut out of
    their atlas with a small margin and shelf-packed into one image; UVs are
    only shifted. None when the islands do not fit TEX_MAX² (→ re-bake)."""
    import cv2
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components

    src = _SRC
    faces = job["faces"]
    t0 = time.time()
    Fo = src.F[faces]
    used, Fl = np.unique(Fo, return_inverse=True)
    Fl = Fl.reshape(-1, 3)
    nf, nv = len(Fl), len(used)
    g = sp.coo_matrix((np.ones(3 * nf), (np.repeat(np.arange(nf), 3), nf + Fl.ravel())),
                      shape=(nf + nv, nf + nv))
    _, comp = connected_components(g, directed=False)
    fcomp = comp[:nf]
    _, fcomp = np.unique(fcomp, return_inverse=True)
    ncomp = int(fcomp.max()) + 1
    mat_f = src.mat[faces]
    uv = src.UV[used]
    uv = uv - np.floor(uv)                               # UDIM → fractional (see _sample)
    # per-vertex material (a glTF vertex belongs to one primitive)
    mat_v = np.zeros(nv, np.int64); mat_v[Fl.ravel()] = np.repeat(mat_f, 3)
    size = np.array([_ATLAS_SIZE.get(m, (4, 4)) for m in range(len(_ATLAS_PATHS))], float)
    px = uv * size[mat_v]
    vcomp = np.zeros(nv, np.int64); vcomp[Fl.ravel()] = np.repeat(fcomp, 3)
    lo = np.full((ncomp, 2), np.inf); hi = np.full((ncomp, 2), -np.inf)
    np.minimum.at(lo, vcomp, px); np.maximum.at(hi, vcomp, px)
    cmat = np.zeros(ncomp, np.int64); cmat[fcomp] = mat_f
    x0 = np.maximum(np.floor(lo).astype(np.int64) - COPY_MARGIN, 0)
    x1 = np.minimum(np.ceil(hi).astype(np.int64) + COPY_MARGIN, size[cmat].astype(np.int64))
    wh = np.maximum(x1 - x0, 1)
    if (wh > TEX_MAX).any():
        return None
    total = float((wh[:, 0] * wh[:, 1]).sum())
    order = np.argsort(-wh[:, 1], kind="stable")

    def pack(W):
        pos = np.zeros((ncomp, 2), np.int64)
        x = y = shelf = 0
        for i in order:
            w, h = wh[i]
            if x + w > W:
                x, y, shelf = 0, y + shelf, 0
            pos[i] = (x, y)
            x += w; shelf = max(shelf, h)
        return pos, y + shelf

    W = int(min(TEX_MAX, max(wh[:, 0].max(), math.ceil(math.sqrt(total * 1.1) / 64) * 64)))
    pos, H = pack(W)
    if H > TEX_MAX and W < TEX_MAX:
        W = TEX_MAX
        pos, H = pack(W)
    if H > TEX_MAX:
        return None
    H = int(math.ceil(H / 4) * 4)
    img = np.zeros((H, W, 3), np.uint8)
    empty = np.ones((H, W), bool)
    for i in range(ncomp):
        a = _atlas(int(cmat[i]), 0)
        (sx, sy), (w, h), (dx, dy) = x0[i], wh[i], pos[i]
        img[dy:dy + h, dx:dx + w] = a[sy:sy + h, sx:sx + w]
        empty[dy:dy + h, dx:dx + w] = False
    if empty.any():                                   # gutters: nearest colour (no dark mip bleeding)
        from scipy import ndimage
        _, (iy, ix) = ndimage.distance_transform_edt(empty, return_indices=True)
        img = img[iy, ix]
    new_uv = (px - x0[vcomp] + pos[vcomp]) / [W, H]
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, LEAF_JPEG_QUALITY,
                                         cv2.IMWRITE_JPEG_OPTIMIZE, 1])
    Vl = src.V[used]
    uri = f"t{job['id']}.glb"
    _write_glb(_OUT / uri, Vl.astype(np.float32), new_uv.astype(np.float32),
               Fl.astype(np.uint32), jpg.tobytes())
    return {
        "id": job["id"], "uri": uri, "faces": int(nf), "tex": [W, H], "error": 0.0,
        "lo": Vl.min(0).tolist(), "hi": Vl.max(0).tolist(),
        "secs": round(time.time() - t0, 1), "copy": True,
    }


def _build_node(job: dict) -> dict:
    """Build one tile: geometry → xatlas UVs → baked texture → glb."""
    import cv2
    import xatlas
    from scipy import ndimage

    src = _SRC
    faces = job["faces"]
    leaf = job["leaf"]
    t0 = time.time()
    if leaf and COPY_LEAVES:
        r = _copy_leaf(job)
        if r is not None:
            return r

    # 1) geometry (welded; leaves keep face order ↔ original faces)
    Vw, Fw = _weld(src.V, src.F[faces])
    used = np.unique(Fw)
    remap = np.full(len(Vw), -1, np.int64); remap[used] = np.arange(len(used))
    Vn, Fn = Vw[used], remap[Fw]
    if not leaf and len(Fn) > NODE_FACES:
        Vn, Fn = _simplify(Vn, Fn, NODE_FACES)

    area = 0.5 * np.linalg.norm(np.cross(Vn[Fn[:, 1]] - Vn[Fn[:, 0]], Vn[Fn[:, 2]] - Vn[Fn[:, 0]]), axis=1).sum()
    density = src.density if leaf else min(src.density, TEX_MAX * TEX_MAX * PACK_EFF / max(area, 1e-12))
    side = int(math.ceil(math.sqrt(area * density / PACK_EFF) / 64.0) * 64)
    side = max(TEX_MIN, min(TEX_MAX, side))

    # 2) parametrization (face order preserved by xatlas)
    atlas = xatlas.Atlas()
    atlas.add_mesh(Vn.astype(np.float32), Fn.astype(np.uint32))
    po = xatlas.PackOptions()
    po.resolution = side
    po.padding = PADDING
    po.bilinear = True
    po.blockAlign = True
    co = xatlas.ChartOptions()
    co.max_cost = 4.0            # slightly larger charts: ~20 % faster, same packing
    atlas.generate(chart_options=co, pack_options=po)
    # resolution + auto texels-per-unit *approximately* fits one page; if charts
    # spilled onto a second page, shrink the texel scale until it fits. (A single
    # page that overshoots TEX_MAX is fine — the baked image is downscaled below,
    # UVs are normalized — which avoids a second ~25 s parametrization.)
    for _ in range(8):
        if atlas.atlas_count <= 1:
            break
        po.texels_per_unit = float(atlas.texels_per_unit) * 0.85
        atlas = xatlas.Atlas()
        atlas.add_mesh(Vn.astype(np.float32), Fn.astype(np.uint32))
        atlas.generate(chart_options=co, pack_options=po)
    vmap, Fx, UVx = atlas[0]
    W, H = int(atlas.width), int(atlas.height)
    Vx = Vn[vmap]
    Fx = Fx.astype(np.int64)

    # 3) rasterize texel → face id
    ids = np.full((H, W), -1, np.int32)
    pts = np.round((UVx[Fx] * [W, H] - 0.5) * 16).astype(np.int32)
    for i in range(len(Fx)):
        cv2.fillConvexPoly(ids, pts[i], i, lineType=cv2.LINE_8, shift=4)
    ys, xs = np.nonzero(ids >= 0)
    f = ids[ys, xs].astype(np.int64)
    tuv = np.stack([(xs + 0.5) / W, (ys + 0.5) / H], 1)
    bc = _bary(tuv, UVx[Fx[f, 0]], UVx[Fx[f, 1]], UVx[Fx[f, 2]])

    # 4) bake colour
    deviation = 0.0
    if leaf:
        of = faces[f]                                   # original face per texel
        ouv = (bc[:, :, None] * src.UV[src.F[of]]).sum(1)
        col = _sample(ouv, src.mat[of], 0)
    else:
        P = (bc[:, :, None] * Vx[Fx[f]]).sum(1)
        tri = Vx[Fx]
        N = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-20)
        N = N[f]
        lo, hi = src.V[src.F[faces]].reshape(-1, 3).min(0), src.V[src.F[faces]].reshape(-1, 3).max(0)
        maxd = float(np.linalg.norm(hi - lo)) * 0.03
        ray = _ray(faces)
        best_d = np.full(len(P), np.inf); best_f = np.full(len(P), -1, np.int64)
        best_p = np.zeros_like(P)
        for sgn in (-1.0, 1.0):
            tri_i, ray_i, loc = ray.intersects_id(P - sgn * N * maxd * 0.5, sgn * N,
                                                  multiple_hits=False, return_locations=True)
            d = np.linalg.norm(loc - P[ray_i], axis=1)
            better = d < best_d[ray_i]
            r = ray_i[better]
            best_d[r] = d[better]; best_f[r] = tri_i[better]; best_p[r] = loc[better]
        hit = (best_f >= 0) & (best_d <= maxd)
        if hit.any():
            deviation = float(np.percentile(best_d[hit], 99))
        ohf = faces[best_f[hit]]                       # node-local → source face ids
        obc = _bary(best_p[hit], *[src.V[src.F[ohf, k]] for k in range(3)])
        ouv = (obc[:, :, None] * src.UV[src.F[ohf]]).sum(1)
        texel_ratio = math.sqrt(src.density / max(density, 1e-12))
        level = max(0, int(math.floor(math.log2(max(texel_ratio, 1.0)))))
        col = np.zeros((len(P), 3), np.float32)
        col[hit] = _sample(ouv, src.mat[ohf], level)
        keep = hit
        ys, xs, col = ys[keep], xs[keep], col[keep]

    img = np.zeros((H, W, 3), np.uint8)
    img[ys, xs] = np.clip(col + 0.5, 0, 255).astype(np.uint8)
    # 5) gutter fill: every empty texel takes its nearest baked colour (no black
    # seams once mipmapped / filtered)
    empty = np.ones((H, W), bool); empty[ys, xs] = False
    if empty.any() and (~empty).any():
        _, (iy, ix) = ndimage.distance_transform_edt(empty, return_indices=True)
        img = img[iy, ix]
    coverage = len(ys) / float(W * H)
    if max(W, H) > TEX_MAX:           # hard GPU limit (older iPads: 4096)
        k = TEX_MAX / max(W, H)
        W, H = max(1, int(W * k)), max(1, int(H * k))
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY,
                                         cv2.IMWRITE_JPEG_OPTIMIZE, 1])

    real_density = (coverage * W * H) / max(area, 1e-12)
    texel = 1.0 / math.sqrt(max(real_density, 1e-12))

    uri = f"t{job['id']}.glb"
    _write_glb(_OUT / uri, Vx.astype(np.float32), UVx.astype(np.float32),
               Fx.astype(np.uint32), jpg.tobytes())
    return {
        "id": job["id"], "uri": uri, "faces": int(len(Fx)), "tex": [W, H],
        "error": 0.0 if leaf else max(deviation, TEXEL_ERR * texel),
        "lo": Vx.min(0).tolist(), "hi": Vx.max(0).tolist(),
        "secs": round(time.time() - t0, 1),
    }


# ─── GLB writer ──────────────────────────────────────────────────────────────

def _write_glb(path: Path, V: np.ndarray, UV: np.ndarray, F: np.ndarray, jpg: bytes) -> None:
    def pad4(b: bytes, fill=b"\0") -> bytes:
        return b + fill * ((4 - len(b) % 4) % 4)

    parts, views = [], []

    def add(data: bytes, target=None) -> int:
        off = sum(len(p) for p in parts)
        parts.append(pad4(data))
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        return len(views) - 1

    bv_p = add(V.tobytes(), 34962)
    bv_t = add(UV.tobytes(), 34962)
    bv_i = add(F.reshape(-1).tobytes(), 34963)
    bv_img = add(jpg)
    g = {
        "asset": {"version": "2.0", "generator": "VISTA tiles"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1},
                                    "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                                "metallicFactor": 0.0}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": bv_img, "mimeType": "image/jpeg"}],
        "accessors": [
            {"bufferView": bv_p, "componentType": 5126, "count": len(V), "type": "VEC3",
             "min": V.min(0).tolist(), "max": V.max(0).tolist()},
            {"bufferView": bv_t, "componentType": 5126, "count": len(UV), "type": "VEC2"},
            {"bufferView": bv_i, "componentType": 5125, "count": int(F.size), "type": "SCALAR"},
        ],
        "bufferViews": views,
    }
    binb = b"".join(parts)
    g["buffers"] = [{"byteLength": len(binb)}]
    js = pad4(json.dumps(g, separators=(",", ":")).encode(), b" ")
    total = 12 + 8 + len(js) + 8 + len(binb)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A)); fh.write(js)
        fh.write(struct.pack("<II", len(binb), 0x004E4942)); fh.write(binb)


# ─── Paths / frame sync ──────────────────────────────────────────────────────

def tiles_dir_for(lod0: Path) -> Path:
    """model_lod0.gltf → model_tiles/ ;  model_crop_lod0.gltf → model_crop_tiles/"""
    lod0 = Path(lod0)
    stem = lod0.name[: -len("_lod0.gltf")] if lod0.name.endswith("_lod0.gltf") else lod0.stem
    return lod0.parent / f"{stem}_tiles"


def _pristine(lod0: Path) -> Path:
    orig = lod0.with_name(lod0.name + ".orig")
    return orig if orig.exists() else lod0


def _first_positions(gltf: Path, n: int = 4000) -> np.ndarray:
    g = json.loads(Path(gltf).read_text())
    bin_name = g["buffers"][0]["uri"]
    bin_path = Path(gltf).parent / bin_name
    if Path(gltf).name.endswith(".orig"):
        cand = bin_path.with_name(bin_path.name + ".orig")
        bin_path = cand if cand.exists() else bin_path
    buf = bin_path.read_bytes()
    pr = g["meshes"][0]["primitives"][0]
    P = _read_accessor(g, buf, pr["attributes"]["POSITION"]).astype(np.float64)
    step = max(1, len(P) // n)
    return P[::step][:n]


def _current_bounds(lod0: Path):
    g = json.loads(Path(lod0).read_text())
    lo = np.full(3, np.inf); hi = np.full(3, -np.inf)
    for m in g["meshes"]:
        for pr in m["primitives"]:
            a = g["accessors"][pr["attributes"]["POSITION"]]
            lo = np.minimum(lo, a["min"]); hi = np.maximum(hi, a["max"])
    return lo.tolist(), hi.tolist()


def sync_tileset(lod0: Path) -> bool:
    """Re-express the model's CURRENT frame (bake) on an existing tileset: root
    transform = affine pristine→current (exact: same vertex order), plus the
    current bounds the viewer normalizes by. Cheap; call after bake / restore."""
    lod0 = Path(lod0)
    ts_path = tiles_dir_for(lod0) / "tileset.json"
    if not ts_path.exists():
        return False
    ts = json.loads(ts_path.read_text())
    A = np.eye(4)
    pristine = _pristine(lod0)
    if pristine != lod0:
        P0 = _first_positions(pristine)
        P1 = _first_positions(lod0)
        X = np.hstack([P0, np.ones((len(P0), 1))])
        M, *_ = np.linalg.lstsq(X, P1, rcond=None)     # (4,3): P1 = X @ M
        A[:3, :] = M.T
    if np.allclose(A, np.eye(4), atol=1e-9):
        ts["root"].pop("transform", None)
    else:
        ts["root"]["transform"] = [float(v) for v in A.T.reshape(-1)]   # column-major
    lo, hi = _current_bounds(lod0)
    ts.setdefault("extras", {})["bounds"] = {"min": lo, "max": hi}
    ts_path.write_text(json.dumps(ts))
    return True


# ─── Build ───────────────────────────────────────────────────────────────────

def build_tiles(lod0: Path, workers: int | None = None, log=print) -> Path:
    """Build the tileset for a model (by its model_lod0.gltf). Returns tileset path.
    Serialized per model by a file lock: a second build waits, then rebuilds
    (so the final tileset always matches the latest model)."""
    import fcntl
    lock_path = tiles_dir_for(lod0).with_name(tiles_dir_for(lod0).name + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not Path(lod0).exists():
                raise FileNotFoundError(lod0)
            return _build_tiles(Path(lod0), workers, log)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _build_tiles(lod0: Path, workers: int | None, log) -> Path:
    global _SRC, _OUT, _ATLAS_PATHS, _ATLAS_SIZE
    import cv2
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp

    lod0 = Path(lod0)
    t0 = time.time()
    src_path = _pristine(lod0)
    # a baked .orig gltf still references the (baked) .bin by name; point at its .orig
    if src_path != lod0:
        tmp = lod0.parent / f".tiles_src_{os.getpid()}.gltf"
        g = json.loads(src_path.read_text())
        b = g["buffers"][0]["uri"]
        if (lod0.parent / (b + ".orig")).exists():
            g["buffers"][0]["uri"] = b + ".orig"
        tmp.write_text(json.dumps(g))
        try:
            src = load_source(tmp)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        src = load_source(lod0)

    sizes = {}
    for i, p in enumerate(src.atlases):
        if p is not None and p.exists():
            im = cv2.imread(str(p), cv2.IMREAD_REDUCED_COLOR_8)
            sizes[i] = (im.shape[1] * 8, im.shape[0] * 8)
    src.density = _measure_density(src, sizes)
    log(f"source: {len(src.F):,} faces, {len(src.atlases)} atlases, "
        f"density {src.density:.1f} texels/unit²")

    root = build_tree(src)
    nodes = list(_walk(root))
    log(f"tree: {len(nodes)} tiles, {sum(n.leaf for n in nodes)} leaves, "
        f"depth {max(n.depth for n in nodes)}")

    out = tiles_dir_for(lod0)
    stage = out.with_name(out.name + ".building")
    # resumable: a stage built from the SAME source and tree keeps its finished
    # tiles (t<id>.glb + r<id>.json), so a crash (out of memory on a whole
    # bridge) does not throw away hours of work
    sig = {"faces": int(len(src.F)), "tiles": len(nodes), "mtime": lod0.stat().st_mtime_ns,
           "version": TILES_VERSION}
    sigf = stage / "_resume.json"
    if not (sigf.exists() and json.loads(sigf.read_text()) == sig):
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        sigf.write_text(json.dumps(sig))

    _SRC, _OUT = src, stage
    _ATLAS_PATHS, _ATLAS_SIZE = list(src.atlases), dict(sizes)

    results: dict[int, dict] = {}
    for f in stage.glob("r*.json"):
        r = json.loads(f.read_text())
        if (stage / r["uri"]).exists():
            results[r["id"]] = r
    if results:
        log(f"resuming: {len(results)} tiles already built")
    jobs = [{"id": n.id, "faces": n.faces, "leaf": n.leaf} for n in nodes if n.id not in results]
    # biggest (root, parents) first so the pool stays busy
    jobs.sort(key=lambda j: -len(j["faces"]))

    # Memory per job (measured on a 27 M-triangle bridge): a parent tile needs
    # ≈ 3,5 kB per source triangle (weld, quadric simplification, intersector)
    # + the atlas cache; a leaf only copies atlas pieces. Three tiers, each
    # with as many workers as the available memory allows.
    try:
        avail = next(int(l.split()[1]) * 1024 for l in open("/proc/meminfo") if l.startswith("MemAvailable"))
    except (OSError, StopIteration, ValueError):
        avail = 16e9
    cpu = max(1, min(8, (os.cpu_count() or 2) // 3))
    tiers = [
        ("parents > 3 M", lambda j: not j["leaf"] and len(j["faces"]) > 3_000_000, 1),
        ("parents", lambda j: not j["leaf"], 3_500 * 3_000_000 + ATLAS_CACHE_BYTES),
        ("leaves", lambda j: j["leaf"], 1.5e9 + ATLAS_CACHE_BYTES),
    ]
    done_ids: set = set()
    for name, sel, per in tiers:
        batch = [j for j in jobs if j["id"] not in done_ids and sel(j)]
        if not batch:
            continue
        n = workers or (1 if per == 1 else max(1, min(cpu, int((avail - 8e9) / per))))
        log(f"{name}: {len(batch)} tiles, {n} workers")
        with ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("fork")) as ex:
            for r in ex.map(_build_node, batch):
                results[r["id"]] = r
                done_ids.add(r["id"])
                (stage / f"r{r['id']}.json").write_text(json.dumps(r))
                log(f"  tile {r['id']:>3}: {r['faces']:>7,} faces, tex {r['tex'][0]}x{r['tex'][1]}, "
                    f"err {r['error']:.4g}, {r['secs']} s")

    # errors must not increase towards the leaves
    def fix(n: Node) -> float:
        e = results[n.id]["error"]
        if n.children:
            ce = max(fix(c) for c in n.children)
            e = max(e, ce * 1.05 + 1e-9)
        results[n.id]["error"] = e
        return e
    fix(root)

    def tile_json(n: Node) -> dict:
        r = results[n.id]
        kids = [tile_json(c) for c in n.children]      # finalizes the children's boxes
        lo, hi = np.array(r["lo"]), np.array(r["hi"])
        for c in n.children:              # parent volume must contain the children's
            lo = np.minimum(lo, results[c.id]["lo"]); hi = np.maximum(hi, results[c.id]["hi"])
        r["lo"], r["hi"] = lo.tolist(), hi.tolist()
        c = (lo + hi) / 2
        h = np.maximum((hi - lo) / 2, 1e-6)
        t = {
            "boundingVolume": {"box": [*c.tolist(), h[0], 0, 0, 0, h[1], 0, 0, 0, h[2]]},
            "geometricError": results[n.id]["error"],
            "refine": "REPLACE",
            "content": {"uri": r["uri"]},
        }
        if kids:
            t["children"] = kids
        return t

    root_json = tile_json(root)
    lo, hi = np.array(results[root.id]["lo"]), np.array(results[root.id]["hi"])
    tileset = {
        "asset": {"version": "1.1", "generator": "VISTA", "gltfUpAxis": "Z",
                  "tilesetVersion": str(TILES_VERSION)},
        "geometricError": float(max(root_json["geometricError"] * 4, np.linalg.norm(hi - lo))),
        "root": root_json,
        "extras": {"source": lod0.name, "faces": int(len(src.F)),
                   "density": src.density, "built": time.strftime("%Y-%m-%dT%H:%M:%S")},
    }
    (stage / "tileset.json").write_text(json.dumps(tileset))

    # atomic swap: the viewer never sees a half-written tileset
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.rename(old)
    for f in [*stage.glob("r*.json"), stage / "_resume.json"]:     # resume bookkeeping
        f.unlink(missing_ok=True)
    stage.rename(out)
    shutil.rmtree(old, ignore_errors=True)

    sync_tileset(lod0)
    size = sum(p.stat().st_size for p in out.iterdir()) / 1e6
    log(f"done: {len(nodes)} tiles, {size:.0f} MB, {time.time() - t0:.0f} s → {out}")
    return out / "tileset.json"


BACKEND_DIR = Path(__file__).resolve().parents[2]


def build_command(lod0: Path) -> list[str]:
    return [sys.executable, "-m", "app.services.tiles", "build", str(Path(lod0).resolve())]


def spawn_build(lod0: Path) -> None:
    """Fire-and-forget tile build in a detached process (survives a server
    reload; heavy memory stays out of the API worker). The viewer keeps using the
    glTF LODs until the tileset appears."""
    import subprocess
    lod0 = Path(lod0)
    log = lod0.parent / "tiles_build.log"
    with open(log, "w") as fh:
        subprocess.Popen(build_command(lod0), cwd=BACKEND_DIR, stdout=fh,
                         stderr=subprocess.STDOUT, start_new_session=True)


def remove_tiles(model_dir: Path) -> None:
    """Delete every tileset under a model dir (the model is being regenerated —
    stale tiles would otherwise be streamed for the new model)."""
    for d in Path(model_dir).glob("**/*_tiles"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def sync_model_dir(model_dir: Path) -> None:
    """Re-sync every tileset in a model dir after its geometry frame changed."""
    for lod0 in Path(model_dir).glob("**/*_lod0.gltf"):
        try:
            sync_tileset(lod0)
        except Exception as e:  # noqa: BLE001 — never break a bake over tiles
            print(f"tiles sync failed for {lod0}: {e}")


def model_lod0_for(inspection_dir: Path) -> Path | None:
    for p in sorted(Path(inspection_dir).glob("model/**/*_lod0.gltf")):
        if not p.name.endswith("_mobile.gltf"):
            return p
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build" and len(sys.argv) > 2:
        build_tiles(Path(sys.argv[2]))
    elif cmd == "sync" and len(sys.argv) > 2:
        print(sync_tileset(Path(sys.argv[2])))
    elif cmd == "all":
        # the ACTIVE model of each inspection (photo_sets.model_path) — a dir can
        # also hold crop variants, so globbing would pick the wrong one
        import sqlite3
        from app.config import settings
        db = BACKEND_DIR / "vista.db"
        rows = sqlite3.connect(db).execute(
            "SELECT inspection_id, model_path FROM photo_sets "
            "WHERE status = 'completed' AND model_path LIKE '%\\_lod0.gltf' ESCAPE '\\'"
        ).fetchall()
        force = "--force" in sys.argv
        for insp, rel in rows:
            lod0 = settings.upload_path / rel
            if not lod0.exists():
                continue
            if not force and (tiles_dir_for(lod0) / "tileset.json").exists():
                continue
            print(f"== {insp}: {lod0}", flush=True)
            try:
                build_tiles(lod0)
            except Exception as e:  # noqa: BLE001
                print(f"   FAILED: {e}", flush=True)
    else:
        print(__doc__)
