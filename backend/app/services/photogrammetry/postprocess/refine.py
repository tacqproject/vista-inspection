"""
Mesh refinement: PyMeshLab cleanup of the raw AliceVision mesh, followed by
a separate aliceVision_texturing pass on the cleaned geometry.

Workflow (active only when params.refine_mesh is set):
  1. Meshroom stops at MeshFiltering (engine adds --toNode MeshFiltering)
  2. PyMeshLab: weld seams → remove floaters → repair non-manifold edges →
     close every hole except the scan rim (optional: screened-Poisson rebuild,
     decimation)
  3. aliceVision_texturing projects the source photos onto the clean mesh —
     the camera poses from step 1 stay valid because the mesh is only
     cleaned/decimated, never moved.

Runs BEFORE harvest-cameras (order 5 < 10), while the cache still exists.
Texturing runs through the same Docker wrapper as meshroom_batch
(VISTA_MESHROOM_BIN selects the binary; cache is mounted at /tmp/MeshroomCache).
"""

import asyncio
import os
from pathlib import Path

from app.config import settings
from ..base import ReconstructionContext, postprocess_step

CONTAINER_CACHE = "/tmp/MeshroomCache"

# Inspection models need surface FIDELITY: cracks and spalling are centimetres.
# Measured on real scans (app/services/mesh_quality.py): closing holes directly
# on the reconstructed surface removes them all without moving a single
# measured point, whereas screened Poisson at depth 10 re-samples the whole
# surface on a ~diag/1024 grid (≈5 cm on a 50 m bridge) and smears detail — so
# Poisson is now opt-in, and so is decimation (the viewer streams full-resolution
# tiles, the browser no longer needs a small mesh).
MIN_COMPONENT_REL = 0.002     # floaters < 0.2 % of the faces are removed
HOLE_MAX_REL = 1.0            # close holes with perimeter up to 1.0 × bbox diagonal


def _boundary_loops(ms) -> list[tuple[int, float]]:
    """(edge count, perimeter) of every boundary loop, largest perimeter first."""
    import numpy as np
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components
    m = ms.current_mesh()
    V, F = m.vertex_matrix(), m.face_matrix()
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    uniq, cnt = np.unique(e, axis=0, return_counts=True)
    bnd = uniq[cnt == 1]
    if not len(bnd):
        return []
    n = len(V)
    g = sp.coo_matrix((np.ones(len(bnd)), (bnd[:, 0], bnd[:, 1])), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    comp = lab[bnd[:, 0]]
    elen = np.linalg.norm(V[bnd[:, 0]] - V[bnd[:, 1]], axis=1)
    per = np.bincount(comp, weights=elen)
    num = np.bincount(comp)
    loops = [(int(num[c]), float(per[c])) for c in np.unique(comp)]
    return sorted(loops, key=lambda x: -x[1])


def _close_holes(ms, stats: dict) -> None:
    """Fill every hole except the outer rim of the scanned area (the largest
    boundary loop, when it is clearly a scan boundary). Needs a 2-manifold mesh."""
    ms.meshing_repair_non_manifold_edges(method="Remove Faces")
    ms.meshing_repair_non_manifold_vertices()
    ms.meshing_remove_unreferenced_vertices()
    bb = ms.current_mesh().bounding_box().diagonal()
    loops = _boundary_loops(ms)
    stats["holes_before"] = max(0, len(loops) - 1)
    if not loops:
        return
    rim_is_scan_edge = loops[0][1] >= 0.5 * bb
    holes = loops[1:] if rim_is_scan_edge else loops
    holes = [h for h in holes if h[1] <= HOLE_MAX_REL * bb]
    if not holes:
        return
    maxedges = max(h[0] for h in holes) + 1
    if rim_is_scan_edge:
        maxedges = min(maxedges, loops[0][0] - 1)      # never close the scan rim
    ms.meshing_close_holes(maxholesize=int(maxedges), selected=False, newfaceselected=False,
                           selfintersection=False, refinehole=True)
    stats["holes_after"] = max(0, len(_boundary_loops(ms)) - (1 if rim_is_scan_edge else 0))


def _clean_mesh(in_path: str, out_path: str, target_faces: int | None = None,
                watertight: bool = False, keep_largest: bool = False) -> dict:
    """
    PyMeshLab cleanup — runs in a worker thread (CPU heavy).

    Default: weld seams → drop floaters → repair non-manifold → close holes (all
    but the scan rim) — every measured vertex stays where photogrammetry put it.
    `watertight=True` adds the old screened-Poisson rebuild (+ trimming of the
    "soap bubbles" it invents) for users who want a closed shell regardless of
    detail; `target_faces` optionally decimates.
    """
    import pymeshlab

    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(in_path)
    faces_in = ms.current_mesh().face_number()
    stats: dict = {"faces_in": faces_in}

    def floaters():
        ms.meshing_remove_connected_component_by_face_number(
            mincomponentsize=max(500, int(faces_in * MIN_COMPONENT_REL)))

    def largest_only():
        # "ponechat jen hlavní souvislou síť": drop every component smaller than
        # the main one — distant background chunks (terrain, trees) otherwise blow
        # up the bounding box and the viewer shrinks the real structure to a dot
        ms.compute_selection_by_small_disconnected_components_per_face(nbfaceratio=0.99)
        ms.meshing_remove_selected_faces()
        ms.meshing_remove_unreferenced_vertices()

    steps = [
        # weld only bit-identical seam duplicates — pymeshlab's DEFAULT threshold is
        # 1 % of the bbox diagonal (≈50 cm on a bridge!), which collapsed 63 % of a
        # refined OpenMVS mesh and produced degenerate faces that crashed texturing
        ("svaření švů", lambda: ms.meshing_merge_close_vertices(
            threshold=pymeshlab.PercentageValue(0.0001))),
        ("odstranění plovoucích fragmentů", floaters),
        ("uzavření děr", lambda: _close_holes(ms, stats)),
        # the non-manifold repair can chip off tiny pieces — sweep them again
        ("odstranění úlomků po opravě", floaters),
    ]
    if keep_largest:
        steps.append(("ponechání hlavní souvislé sítě", largest_only))
    if watertight:
        steps += [
            ("normály pro Poisson", lambda: ms.compute_normal_per_vertex()),
            ("Poisson rekonstrukce (slití děr)",
             lambda: ms.generate_surface_reconstruction_screened_poisson(depth=11, preclean=True)),
            ("ořez ploch daleko od původního povrchu", lambda: _trim_far_faces(ms)),
        ]
    if target_faces:
        steps += [
            ("zrušení výběru", lambda: ms.set_selection_none(allfaces=True, allverts=True)),
            ("decimace", lambda: ms.meshing_decimation_quadric_edge_collapse(
                targetfacenum=int(target_faces), preservenormal=True,
                planarquadric=True, qualitythr=0.5)),
        ]
    # A requested decimation is load-bearing (the caller wants a small mesh) —
    # fail loudly if it breaks; the other steps degrade gracefully with a warning.
    critical = {"decimace"}
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            if name in critical:
                raise RuntimeError(f"Refine selhal v kroku '{name}': {e}") from e
            stats.setdefault("warnings", []).append(f"{name}: {e}")

    faces_out = ms.current_mesh().face_number()
    if target_faces and faces_out > max(target_faces * 2, target_faces + 200_000):
        raise RuntimeError(f"Refine nedecimoval síť ({faces_out} ploch, cíl {target_faces}).")
    ms.save_current_mesh(out_path)
    stats["faces_out"] = faces_out
    return stats


def _trim_far_faces(ms) -> None:
    """Delete Poisson faces farther than 0.5 % of bbox diagonal from mesh 0."""
    ms.compute_scalar_by_distance_from_another_mesh_per_vertex(
        measuremesh=ms.current_mesh_id(), refmesh=0, signeddist=False,
    )
    thr = ms.current_mesh().bounding_box().diagonal() * 0.005
    ms.compute_selection_by_condition_per_vertex(condselect=f"q > {thr}")
    ms.compute_selection_transfer_vertex_to_face(inclusive=False)
    ms.meshing_remove_selected_faces()
    ms.meshing_remove_unreferenced_vertices()


def wants_refine(params: dict) -> bool:
    """Refine (hole closing + re-texturing) is on for the top quality presets and
    whenever explicitly requested."""
    if params.get("quality") == "max":
        return False            # the OpenMVS step does hole closing + texturing itself
    return bool(params.get("refine_mesh")) or params.get("quality") == "high"


@postprocess_step("refine-mesh", order=5)
async def run(ctx: ReconstructionContext) -> None:
    if not wants_refine(ctx.params):
        return
    cache_dir = ctx.models_dir / "meshroom_cache"
    output_dir = ctx.models_dir / "meshroom_out"
    output_dir.mkdir(exist_ok=True)

    def _newest(paths):
        paths = list(paths)
        return max(paths, key=lambda p: p.stat().st_mtime) if paths else None

    # AliceVision writes a fresh hash-dir per node per run; after an aborted/
    # retried reconstruction the cache holds several. Pick the NEWEST of each node
    # (the latest run wrote them last) — matching harvest-cameras, which harvests
    # the newest SfM — so the mesh, dense cloud and images all come from the SAME
    # run. Mixing runs projects one run's photos onto another run's geometry (and
    # a lexicographic sorted()[0] could even pick a stale/aborted run).
    raw_mesh = _newest(cache_dir.glob("MeshFiltering/*/mesh.obj"))
    dense_cloud = _newest(cache_dir.glob("Meshing/*/densePointCloud.abc"))
    dense_scene = _newest(d for d in cache_dir.glob("PrepareDenseScene/*") if d.is_dir())
    if not (raw_mesh and dense_cloud and dense_scene):
        raise RuntimeError(
            "Cache neobsahuje výstupy pro refine (MeshFiltering/Meshing/PrepareDenseScene) — "
            "mesh nelze vyčistit a otexturovat."
        )

    # 1) PyMeshLab cleanup
    await ctx.report(88, "Čištění sítě (PyMeshLab)…")
    refined = cache_dir / "refined.obj"
    target = ctx.params.get("refine_faces")                 # None → keep full detail
    stats = await asyncio.to_thread(
        _clean_mesh, str(raw_mesh), str(refined),
        int(target) if target else None, bool(ctx.params.get("watertight")),
    )
    (output_dir / "refine_stats.txt").write_text(str(stats))

    # 2) Texturing of the clean mesh — camera poses from the same run
    await ctx.report(92, "Texturování vyčištěné sítě…")

    def cpath(host: Path) -> str:
        """Host cache path → path inside the container."""
        return f"{CONTAINER_CACHE}/{host.relative_to(cache_dir)}"

    texture_side = int(ctx.params.get("texture_size") or 8192)
    # Match the standard/high pipeline: full-res texture projection (downscale 1)
    # is the biggest sharpness lever, and fillHoles paints unobserved texels
    # instead of leaving black patches. Honour texture_downscale when the caller
    # sets it. Hardcoding downscale=2 (as before) made the refine path — a premium
    # option — produce BLURRIER, hole-pocked textures than a plain standard run.
    downscale = int(ctx.params.get("texture_downscale") or 1)
    cmd = [
        settings.meshroom_path,
        "--input", cpath(dense_cloud),
        "--imagesFolder", cpath(dense_scene),
        "--inputMesh", cpath(refined),
        "--textureSide", str(texture_side),
        "--downscale", str(downscale),
        "--fillHoles", "True",
        "--outputMeshFileType", "obj",
        "--colorMappingFileType", "exr",
        "--unwrapMethod", "Basic",
        "--useUDIM", "True",
        "--output", str(output_dir),
    ]
    env = {
        **os.environ,
        "VISTA_MESHROOM_CACHE": str(cache_dir),
        "VISTA_MESHROOM_BIN": "aliceVision_texturing",
    }
    log_file = ctx.models_dir / "texturing.log"
    with open(log_file, "w", encoding="utf-8") as lf:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=lf, stderr=asyncio.subprocess.STDOUT,
        )
        rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"Texturování vyčištěné sítě selhalo (kód {rc}). Detaily: {log_file}")

    obj_files = sorted(output_dir.glob("texturedMesh*.obj"))
    if not obj_files:
        raise RuntimeError("Texturing nevytvořil texturedMesh.obj")
    ctx.obj_file = obj_files[0]
    ctx.result["model_path"] = ctx.obj_file
