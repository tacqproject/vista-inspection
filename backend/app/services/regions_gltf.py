"""
Merged region models → glTF LODs without gltfpack.

The npm (WebAssembly) gltfpack cannot open the merged full-resolution model of
a whole bridge (40 regions, 26 M triangles, 3,6 GB OBJ: it has a 4 GB address
space), so a large model is written here directly from the region OBJs that
hires_texture.bake writes — the same output gltfpack produced for smaller
ones:

  model_lod0.gltf/.bin  every triangle, positions as in the OBJ (app/mesh
                        frame, float32), TEXCOORD_0 with v flipped (glTF:
                        v=0 is the image top), one primitive per texture page,
                        vertices shared where position AND uv are equal
  model_lod1/lod2       25 % / 6 % quadric decimation per region, geometry
                        only (raycasting for photo lookup; the viewer shows
                        streamed tiles built from lod0)

Textures stay external files next to the gltf (texture_r<k>_<page>.jpg, as
batch.merge names them).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

FLOAT, UINT = 5126, 5125


def read_region_obj(obj: Path):
    """hires_texture OBJ → V (n,3), VT (m,2), and [(material name, Fv (k,3), Ft (k,3))]."""
    lines = Path(obj).read_bytes().split(b"\n")
    vl, tl, groups = [], [], []
    cur = None
    for ln in lines:
        if ln.startswith(b"v "):
            vl.append(ln[2:])
        elif ln.startswith(b"vt "):
            tl.append(ln[3:])
        elif ln.startswith(b"f "):
            cur[1].append(ln[2:])
        elif ln.startswith(b"usemtl "):
            cur = (ln[7:].strip().decode(), [])
            groups.append(cur)
    V = np.array(b" ".join(vl).split(), np.float64).reshape(-1, 3)
    T = np.array(b" ".join(tl).split(), np.float64).reshape(-1, 2)
    out = []
    for name, fl in groups:
        if not fl:
            continue
        a = np.array(b" ".join(fl).replace(b"/", b" ").split(), np.int64).reshape(-1, 6) - 1
        out.append((name, a[:, [0, 2, 4]], a[:, [1, 3, 5]]))
    return V, T, out


def read_mtl(mtl: Path) -> dict[str, str]:
    m, cur = {}, None
    for ln in Path(mtl).read_text().splitlines():
        ln = ln.strip()
        if ln.startswith("newmtl "):
            cur = ln.split(None, 1)[1]
        elif ln.startswith("map_Kd") and cur:
            m[cur] = ln.split()[-1]
    return m


class _Writer:
    def __init__(self, bin_path: Path):
        self.f = open(bin_path, "wb")
        self.off = 0
        self.views, self.accessors = [], []

    def add(self, arr: np.ndarray, ctype: int, typ: str, target: int, minmax: bool = False) -> int:
        b = np.ascontiguousarray(arr).tobytes()
        pad = (-self.off) % 4
        if pad:
            self.f.write(b"\0" * pad); self.off += pad
        self.views.append({"buffer": 0, "byteOffset": self.off, "byteLength": len(b), "target": target})
        self.f.write(b); self.off += len(b)
        acc = {"bufferView": len(self.views) - 1, "componentType": ctype, "count": int(len(arr)), "type": typ}
        if minmax:
            acc["min"] = arr.min(0).astype(float).tolist(); acc["max"] = arr.max(0).astype(float).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def close(self):
        self.f.close()


def _gltf(path: Path, w: _Writer, meshes, materials, textures, images):
    g = {"asset": {"version": "2.0", "generator": "VISTA regions_gltf"},
         "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
         "meshes": [{"primitives": meshes}], "materials": materials,
         "buffers": [{"uri": path.with_suffix(".bin").name, "byteLength": w.off}],
         "bufferViews": w.views, "accessors": w.accessors}
    if textures:
        g["textures"], g["images"] = textures, images
        g["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        for t in textures:
            t["sampler"] = 0
    path.write_text(json.dumps(g))


def write_lods(region_dirs: list[Path], out: Path, prefix: str = "model", log=print) -> Path:
    import pymeshlab
    out = Path(out)
    lod0 = out / f"{prefix}_lod0.gltf"
    w0 = _Writer(lod0.with_suffix(".bin"))
    w1, w2 = _Writer(out / f"{prefix}_lod1.bin"), _Writer(out / f"{prefix}_lod2.bin")
    prims0, prims1, prims2, mats, texs, imgs = [], [], [], [], [], []
    plain = {"pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.8, 0.8, 1.0], "metallicFactor": 0.0}}
    faces_total = 0
    for k, d in enumerate(region_dirs):
        d = Path(d)
        V, T, groups = read_region_obj(d / "texturedMesh.obj")
        pages = read_mtl(d / "texturedMesh.mtl")
        allF = []
        for name, Fv, Ft in groups:
            src = pages.get(name)
            if src is None:
                continue
            dst = f"texture_r{k}_{src}"
            if not (out / dst).exists():
                shutil.copy2(d / src, out / dst)
            imgs.append({"uri": dst}); texs.append({"source": len(imgs) - 1})
            mats.append({"pbrMetallicRoughness": {"baseColorTexture": {"index": len(texs) - 1},
                                                  "metallicFactor": 0.0}})
            # weld corners with the same position index AND the same uv (charts share uvs)
            uv = T[Ft.ravel()].astype(np.float32)
            key = np.c_[Fv.ravel(), uv.view(np.int32)]
            uk, inv = np.unique(key, axis=0, return_inverse=True)
            pos = V[uk[:, 0]].astype(np.float32)
            tc = uk[:, 1:3].astype(np.int32).view(np.float32).copy()
            tc[:, 1] = 1.0 - tc[:, 1]
            prims0.append({"attributes": {"POSITION": w0.add(pos, FLOAT, "VEC3", 34962, True),
                                          "TEXCOORD_0": w0.add(tc, FLOAT, "VEC2", 34962)},
                           "indices": w0.add(inv.ravel().astype(np.uint32), UINT, "SCALAR", 34963),
                           "material": len(mats) - 1, "mode": 4})
            allF.append(Fv)
            faces_total += len(Fv)
        # decimated geometry LODs of this region
        F = np.concatenate(allF)
        used, Fl = np.unique(F, return_inverse=True)
        for w, prims, frac in ((w1, prims1, 0.25), (w2, prims2, 0.06)):
            ms = pymeshlab.MeshSet()
            ms.add_mesh(pymeshlab.Mesh(V[used], Fl.reshape(-1, 3)))
            ms.meshing_decimation_quadric_edge_collapse(targetfacenum=max(500, int(len(F) * frac)),
                                                        preservetopology=False, planarquadric=True)
            m = ms.current_mesh()
            vv = m.vertex_matrix().astype(np.float32); ff = m.face_matrix().astype(np.uint32)
            prims.append({"attributes": {"POSITION": w.add(vv, FLOAT, "VEC3", 34962, True)},
                          "indices": w.add(ff.ravel(), UINT, "SCALAR", 34963), "material": 0, "mode": 4})
        log(f"regions_gltf: region {k + 1}/{len(region_dirs)} ({d.name}) {len(F):,} faces")
    for w in (w0, w1, w2):
        w.close()
    _gltf(lod0, w0, prims0, mats, texs, imgs)
    _gltf(out / f"{prefix}_lod1.gltf", w1, prims1, [plain], [], [])
    _gltf(out / f"{prefix}_lod2.gltf", w2, prims2, [plain], [], [])
    log(f"regions_gltf: {faces_total:,} faces → {lod0}")
    return lod0
