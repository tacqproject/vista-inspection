"""
Bake a rigid/similarity transform into a structure's inspection models so every
inspection permanently lives in one shared "canonical" frame (the reference
inspection's scene box). After baking, all models line up: the same camera view,
coordinates and scale hold across inspections — no per-view alignment needed.

Only POSITION accessors are touched (our exports carry no normals). Originals are
backed up next to each file (`*.orig`) so a re-bake or restore is always possible.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

FLOAT = 5126   # gltf componentType for 32-bit float


def _apply(matrix: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a 4×4 (row-major) to (N,3) points."""
    return (matrix[:3, :3] @ pts.T).T + matrix[:3, 3]


def _position_accessor_indices(g: dict) -> set[int]:
    idx = set()
    for m in g.get("meshes", []):
        for pr in m.get("primitives", []):
            a = pr.get("attributes", {}).get("POSITION")
            if a is not None:
                idx.add(a)
    return idx


def restore_from_orig(model_dir: Path) -> list[str]:
    """Copy every ``*.orig`` backup back over its baked file, returning the model
    to its pristine (pre-bake) geometry + cameras. The ``.orig`` files are kept, so
    this is idempotent and a subsequent bake starts from the original exactly once.
    No-op when nothing is baked (no ``.orig`` present)."""
    model_dir = Path(model_dir)
    restored: list[str] = []
    for orig in model_dir.glob("**/*.orig"):
        target = orig.with_suffix("")          # strip the trailing ".orig"
        target.write_bytes(orig.read_bytes())
        restored.append(target.name)
    if restored:
        from app.services import tiles        # streamed tiles follow the frame
        tiles.sync_model_dir(model_dir)
    return restored


def bake_gltf(gltf_path: Path, matrix: np.ndarray, baked_bins: "set | None" = None) -> None:
    """
    Transform every POSITION of one .gltf (+ its .bin) by `matrix`, in place.
    A `<name>.orig` / `<name>.bin.orig` backup is written first (once). Pass a
    shared `baked_bins` set when baking several files so a .bin shared by more
    than one .gltf is transformed only once (see the note below).
    """
    gltf_path = Path(gltf_path)
    g = json.loads(gltf_path.read_text())

    # single-buffer .bin exports only (our meshroom output); bail loudly otherwise
    bufs = g.get("buffers", [])
    if len(bufs) != 1 or "uri" not in bufs[0]:
        raise ValueError(f"{gltf_path.name}: expected a single external .bin buffer")
    bin_path = gltf_path.parent / bufs[0]["uri"]
    data = bytearray(bin_path.read_bytes())

    # CRITICAL: several .gltf (e.g. a LOD and its *_mobile twin) can share ONE
    # .bin. Transforming that buffer once per file would apply `matrix` twice and
    # collapse the mesh. `baked_bins` tracks buffers already transformed on disk;
    # for those we only RECOMPUTE this file's accessor bounds from the (already
    # transformed) data, we do not transform again.
    already = baked_bins is not None and bin_path in baked_bins

    # back up originals once, so baking stays reversible / re-runnable
    for src in (gltf_path, bin_path):
        bak = src.with_suffix(src.suffix + ".orig")
        if not bak.exists():
            bak.write_bytes(src.read_bytes())

    # A similarity bake would need to rotate NORMAL/TANGENT vectors too (by R, no
    # translation/scale). Our exports carry none; if that ever changes, fail loudly
    # rather than silently leaving lighting vectors in the old frame.
    for m in g.get("meshes", []):
        for pr in m.get("primitives", []):
            bad = {"NORMAL", "TANGENT"} & set(pr.get("attributes", {}))
            if bad:
                raise ValueError(f"{gltf_path.name}: baking {sorted(bad)} not supported")

    bvs = g["bufferViews"]
    for ai in _position_accessor_indices(g):
        acc = g["accessors"][ai]
        if acc.get("componentType") != FLOAT or acc.get("type") != "VEC3":
            raise ValueError(f"{gltf_path.name}: POSITION acc {ai} is not float VEC3")
        bv = bvs[acc["bufferView"]]
        off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        n = acc["count"]
        stride = bv.get("byteStride") or 12          # tightly packed VEC3 float
        if stride == 12:
            pts = np.frombuffer(data, dtype="<f4", count=n * 3, offset=off).reshape(n, 3)
            out = pts.astype(np.float64) if already else _apply(matrix, pts.astype(np.float64))
            if not already:
                data[off:off + n * 12] = out.astype("<f4").tobytes()
        else:  # interleaved — walk element by element
            out = np.empty((n, 3), np.float64)
            for i in range(n):
                out[i] = struct.unpack_from("<3f", data, off + i * stride)
            if not already:
                out = _apply(matrix, out)
                for i in range(n):
                    struct.pack_into("<3f", data, off + i * stride, *out[i].astype("<f4"))
        acc["min"] = [float(v) for v in out.min(0)]
        acc["max"] = [float(v) for v in out.max(0)]

    if not already:
        bin_path.write_bytes(bytes(data))
    gltf_path.write_text(json.dumps(g))
    if baked_bins is not None:
        baked_bins.add(bin_path)


def bake_cameras_sfm(sfm_path: Path, matrix: np.ndarray) -> None:
    """
    Transform camera poses in a cameras.sfm into the baked (canonical) frame,
    matching bake_gltf's transform of the geometry — otherwise photo projection
    and occlusion would be wrong on baked models (markers land on random spots).

    `matrix` is the mesh-frame bake (X_mesh_new = matrix·X_mesh). Cameras live in
    the SfM frame, related to the mesh frame by the involution S=diag(1,-1,-1)
    (mesh_to_sfm), so in the SfM frame points transform by B = S·matrix·S.
    Intrinsics are unchanged (a similarity preserves the projected pixel).
    """
    sfm_path = Path(sfm_path)
    data = json.loads(sfm_path.read_text())
    poses = data.get("poses")
    if not poses:
        return

    bak = sfm_path.with_suffix(sfm_path.suffix + ".orig")
    if not bak.exists():                     # back up once (reversible re-bake)
        bak.write_text(sfm_path.read_text())

    S = np.diag([1.0, -1.0, -1.0])
    B3 = S @ matrix[:3, :3].astype(float) @ S
    Bt = S @ matrix[:3, 3].astype(float)
    # decompose the similarity B3 = s·R (uniform scale from display_transform +
    # the rigid alignment); scale must not rotate the camera axes
    s = float(np.cbrt(abs(np.linalg.det(B3)))) or 1.0
    Rb = B3 / s

    for pose in poses:
        tf = pose.get("pose", {}).get("transform")
        if not tf:
            continue
        R = np.array([float(v) for v in tf["rotation"]], float).reshape(3, 3)
        C = np.array([float(v) for v in tf["center"]], float)
        tf["rotation"] = [str(v) for v in (Rb @ R).flatten()]
        tf["center"] = [str(v) for v in (B3 @ C + Bt)]

    sfm_path.write_text(json.dumps(data))


def bake_inspection_models(model_dir: Path, matrix: np.ndarray) -> list[str]:
    """Bake every .gltf in a model dir (all LODs, mobile + crop variants share
    the same mesh frame) plus its cameras.sfm. Buffers shared by several .gltf
    are transformed once."""
    model_dir = Path(model_dir)
    # If the model is already (or partially) baked — any .orig present — restore it
    # to the pristine geometry first, so we bake the ORIGINAL exactly once. Without
    # this, a re-bake (or a retry after a mid-bake failure) transforms already-baked
    # positions a SECOND time and collapses the mesh.
    restore_from_orig(model_dir)
    baked: list[str] = []
    baked_bins: set[Path] = set()
    for gltf in sorted(model_dir.glob("*.gltf")):
        bake_gltf(gltf, matrix, baked_bins)
        baked.append(gltf.name)
    # keep cameras aligned with the baked geometry (projection/occlusion);
    # ** already covers the top level — dedup so poses aren't baked twice
    for sfm_file in sorted(set(model_dir.glob("**/cameras.sfm"))):
        bake_cameras_sfm(sfm_file, matrix)
    # tiles are built from the pristine geometry; the bake becomes their root
    # transform (cheap, exact) instead of a rebuild
    from app.services import tiles
    tiles.sync_model_dir(model_dir)
    return baked
