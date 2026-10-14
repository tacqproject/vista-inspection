"""
High-resolution region model: geometry AND texture at the full resolution of
the photos for one part of a structure (a pillar, a bearing, a piece of soffit).

Why: a whole-site model is a compromise — 800 drone photos of a bridge have
~0.9 mm/px on the concrete, but texturing 17 000 m² (fields and trees
included) into a handful of 8K atlases leaves ~5 mm per texel, and the whole
scene is too big for full-resolution depth maps and refinement in 62 GB RAM.
A region is small enough to do everything at maximum:

  1. photos    views that actually measured the region (SfM landmarks inside
               it, from sfm.json observations) — typically 100–150 photos
  2. scene     sub-scene sfm.json (those views + the region's landmarks) →
               COLMAP → OpenMVS; the sparse points confine OpenMVS' region of
               interest, so depth fusion keeps only the region in RAM
  3. dense     DensifyPointCloud at full resolution (level 0, no 2560 cap)
  4. mesh      ReconstructMesh → cropped to the region → RefineMesh at full
               resolution (photometric refinement from the 12 MP photos)
  5. texture   photo-resolution bake (hires_texture) from the ORIGINAL photos
               (undistorted once more, Lanczos + JPEG 98): the prepared photos
               that feed the geometry carry a tone curve (flattened highlights)
               and extra JPEG generations — fine for matching, not for texture
  6. output    texturedMesh.obj in the app frame (y,z negated) + stats

CLI: python -m app.services.hires <inspection_id> <cx> <cy> <cz> <radius> <out_dir> [--no-refine]
(centre in the viewer's mesh frame, radius in mesh units)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from app.services import storage
from app.services.photogrammetry import openmvs_bridge as O
from app.services.photogrammetry.postprocess.openmvs import _to_app_frame, EMPTY_COLOR


def select_views(sfm_data: dict, centre_sfm: np.ndarray, radius: float,
                 max_views: int = 160, min_obs: int = 30) -> tuple[set, list]:
    """Views ranked by how many region landmarks they observed."""
    X = np.array([[float(v) for v in L["X"]] for L in sfm_data["structure"]])
    inside = np.linalg.norm(X - centre_sfm, axis=1) < radius
    counts: dict[str, int] = {}
    for L, ok in zip(sfm_data["structure"], inside):
        if ok:
            for o in L["observations"]:
                counts[o["observationId"]] = counts.get(o["observationId"], 0) + 1
    # the camera must look AT the region: OpenMVS only confines its region of
    # interest when every camera points towards the scene centre
    poses = {p["poseId"]: p["pose"]["transform"] for p in sfm_data["poses"]}
    pose_of = {v["viewId"]: v["poseId"] for v in sfm_data["views"]}
    for vid in list(counts):
        tr = poses.get(pose_of.get(vid))
        if tr is None:
            counts.pop(vid); continue
        M = np.array([float(x) for x in tr["rotation"]]).reshape(3, 3)
        C = np.array([float(x) for x in tr["center"]])
        fwd = M[:, 2]                                    # camera +Z in the world
        d = centre_sfm - C
        if fwd @ d / (np.linalg.norm(d) + 1e-9) < 0.57:   # > ~55° off-axis
            counts.pop(vid)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if not ranked:
        return set(), []
    thr = max(min_obs, int(0.02 * ranked[0][1]))
    keep = [v for v, c in ranked if c >= thr][:max_views]
    return set(keep), ranked


def sub_scene(sfm_data: dict, views: set, centre_sfm: np.ndarray, radius: float) -> dict:
    """sfm.json restricted to `views` and landmarks near the region."""
    out = {k: v for k, v in sfm_data.items() if k not in ("views", "poses", "structure")}
    out["views"] = [v for v in sfm_data["views"] if v["viewId"] in views]
    pose_ids = {v["poseId"] for v in out["views"]}
    out["poses"] = [p for p in sfm_data["poses"] if p["poseId"] in pose_ids]
    st = []
    for L in sfm_data["structure"]:
        X = np.array([float(v) for v in L["X"]])
        if np.linalg.norm(X - centre_sfm) > radius:
            continue
        obs = [o for o in L["observations"] if o["observationId"] in views]
        if len(obs) >= 2:
            st.append({**L, "observations": obs})
    out["structure"] = st
    return out


CELL_OVERLAP = 0.2      # d0 ≤ d_other + 0.2 → ≈ 0.1 m past the bisector


def crop_mesh(src: Path, dst: Path, centre_sfm: np.ndarray, radius: float,
              others_sfm: np.ndarray | None = None, margin: float = 0.0) -> dict:
    """Keep faces within the region (SfM frame); with `others_sfm` (centres of
    neighbouring regions) only faces closer to this centre than to any other —
    a Voronoi partition, so adjacent regions tile the structure without overlap.
    `margin` widens the cell: d0 ≤ d_other + margin (≈ margin/2 beyond the
    bisector) — context for RefineMesh at the borders. Small disconnected
    bits are dropped."""
    import trimesh
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components
    m = trimesh.load(src, force="mesh", process=False)
    d0 = np.linalg.norm(m.triangles_center - centre_sfm, axis=1)
    keep = d0 < radius
    if others_sfm is not None and len(others_sfm):
        from scipy.spatial import cKDTree
        d_other, _ = cKDTree(np.asarray(others_sfm)).query(m.triangles_center)
        keep &= d0 <= d_other + margin
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    adj = m.face_adjacency
    n = len(m.faces)
    g = sp.coo_matrix((np.ones(len(adj)), (adj[:, 0], adj[:, 1])), shape=(n, n))
    k, lab = connected_components(g, directed=False)
    if k > 1:
        size = np.bincount(lab)
        m.update_faces(size[lab] >= 0.05 * size.max())   # pillar + soffit may be separate parts
        m.remove_unreferenced_vertices()
    m.export(dst)
    return {"faces": int(len(m.faces)), "parts": int(k)}


def build(inspection_id: str, centre_mesh, radius: float, out_dir: Path,
          refine: bool = True, dense_level: int = 0, refine_level: int = 0,
          dense_args: tuple = (), refine_args: tuple = (), tex_args: tuple = (),
          ai_normals: dict | bool = True, others_mesh=None, texturer: str = "photo",
          log=print) -> dict:
    """Full-resolution model of one region → out_dir/texturedMesh.obj (app frame)."""
    t0 = time.time()
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    resume = (work / "scene_dense.ply").exists() and (work / "mesh_full.ply").exists() \
        and not dense_args and dense_level == 0
    if not resume:
        shutil.rmtree(work, ignore_errors=True); work.mkdir()
    models = storage.get_models_dir(inspection_id)
    sfm_json = next(models.glob("**/sfm.json"))
    data = json.loads(sfm_json.read_text())
    c = np.array(centre_mesh, float) * [1, -1, -1]            # mesh → SfM frame
    others = np.asarray(others_mesh, float) * [1, -1, -1] if others_mesh is not None and len(others_mesh) else None
    views, ranked = select_views(data, c, radius)
    log(f"hires: {len(views)} photos observe the region (best {ranked[0][1] if ranked else 0} landmarks)")
    sub = sub_scene(data, views, c, radius * 1.15)
    names = {v["viewId"]: Path(v["path"]).name for v in sub["views"]}
    if not resume:
        (work / "sub_sfm.json").write_text(json.dumps(sub))
        exp = O.export_colmap(work / "sub_sfm.json", work)
    else:
        exp = {"views": len(sub["views"]), "points": len(sub["structure"])}
        log("hires: reusing dense cloud + mesh of a previous run")
    log(f"hires: scene {exp}")
    stats = {"photos": exp["views"], "landmarks": exp["points"],
             "photo_names": sorted(names.values())}

    with open(work / "openmvs.log", "a") as lf:
        if not resume:
            O._mvs(work, "InterfaceCOLMAP", "-i", str(work), "-o", "scene.mvs",
                   "--image-folder", str(work / "images") + "/", log=lf)
            # out of memory in depth fusion (a region whose photos see a lot of
            # surroundings): one more try at half image resolution
            for lvl in (dense_level, dense_level + 1):
                try:
                    O._densify(work, lf, "--resolution-level", str(lvl), "--max-resolution", "8192",
                               "--number-views-fuse", "3", *dense_args)
                    stats["dense_level"] = lvl
                    break
                except RuntimeError as e:
                    if lvl > dense_level:
                        raise
                    log(f"hires: DensifyPointCloud level {lvl} failed ({e}), retrying at level {lvl + 1}")
                    for f in work.glob("*.dmap"):
                        f.unlink()
            for f in work.glob("*.dmap"):
                f.unlink()
        n_pts = O._ply_vertex_count(work / "scene_dense.ply")
        if not resume:
            dist = 2.5 * max(1.0, (n_pts / O.MESH_POINTS) ** 0.5) if n_pts else 2.5
            O._mvs(work, "ReconstructMesh", "scene_dense.mvs", "--close-holes", "30",
                   "--min-point-distance", f"{dist:.2f}", "-o", "mesh_full.ply", log=lf)
        stats["dense_points"] = n_pts
        # crop a margin wider than the Voronoi cell: RefineMesh needs context
        # at the borders; the exact cell is cut after refinement
        # (cell + ~0.5 m, not the whole sphere: RefineMesh at level 0 needs RAM
        # ∝ faces × photos — a 7 m sphere with 99 photos exceeded 62 GB)
        stats["crop"] = crop_mesh(work / "mesh_full.ply", work / "mesh.ply", c, radius,
                                  others, margin=1.0)
        mesh = "mesh.ply"
        if refine:
            # an OOM kill (137): retry with fewer views, then one level coarser
            # rather than no refinement
            # fewer neighbour views first (memory ∝ image pairs), then a coarser level
            tries = ((refine_level, ()), (refine_level, ("--max-views", "4")),
                     (refine_level + 1, ()))
            for attempt, (lvl, extra) in enumerate(tries):
                try:
                    O._mvs(work, "RefineMesh", "scene_dense.mvs", "--mesh-file", mesh,
                           "--resolution-level", str(lvl), "--close-holes", "30",
                           "--decimate", "0", *extra, *refine_args, "-o", "mesh_refined.ply", log=lf)
                    stats["refine_level"] = lvl
                    stats["refine_extra"] = list(extra)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    log(f"hires: RefineMesh level {lvl} failed ({e}), retrying")
                    time.sleep(30)
            mesh = "mesh_refined.ply"
    if ai_normals is not False:
        mesh = denoise_mesh(work, mesh, log, **(ai_normals if isinstance(ai_normals, dict) else {}))
    if others is not None:
        # cells overlap by ~10 cm: two refinements of the same border never
        # share vertices, an exact bisector cut leaves hairline cracks
        stats["cell"] = crop_mesh(work / mesh, work / "mesh_cell.ply", c, radius, others, margin=CELL_OVERLAP)
        mesh = "mesh_cell.ply"
    if others is None:
        from app.services.photogrammetry.postprocess.refine import _clean_mesh
        stats["clean"] = str(_clean_mesh(str(work / mesh), str(work / "mesh_final.ply"), None, False, True))
    else:
        # a Voronoi cell has open borders towards its neighbours — general hole
        # closing would cap them; only small interior holes are filled
        stats["holes"] = fill_small_holes(work / mesh, work / "mesh_final.ply")
    for f in out_dir.glob("texture_*"):
        f.unlink()
    if texturer == "photo":
        from app.services import hires_texture
        tex = tex_images(inspection_id, work, log)
        stats["bake"] = hires_texture.bake(work, work / "mesh_final.ply", out_dir, images_dir=tex, log=log)
    else:
        with open(work / "openmvs.log", "a") as lf:
            O._mvs(work, "TextureMesh", "scene_dense.mvs", "--mesh-file", "mesh_final.ply",
                   "--export-type", "obj", "--resolution-level", "0", "--max-texture-size", "8192",
                   "--empty-color", str(EMPTY_COLOR), "--outlier-threshold", "0",
                   "--cost-smoothness-ratio", "0", *tex_args, "-o", "textured.obj", log=lf)
        _to_app_frame(work / "textured.obj", out_dir)
    # the photos of the region are needed for the viewer's camera/photo tools
    shutil.copy2(work / "sub_sfm.json", out_dir / "sfm.json")
    stats.update(texture_stats(out_dir))
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    (out_dir / "hires.json").write_text(json.dumps(stats, indent=1))
    log(f"hires: done {stats}")
    return stats


def texture_stats(model_dir: Path) -> dict:
    """Texel density: atlas pixels actually covered / surface area."""
    import cv2
    import trimesh
    m = trimesh.load(model_dir / "texturedMesh.obj", force="mesh", process=False, skip_materials=True)
    tex = sorted(model_dir.glob("texture_*.jpg"))
    px = 0
    for t in tex:
        im = cv2.imread(str(t), cv2.IMREAD_REDUCED_GRAYSCALE_4)
        if im is not None:
            px += int((np.abs(im.astype(int) - 128) > 2).sum()) * 16   # not the empty grey
    area = float(m.area)
    return {"faces": int(len(m.faces)), "area_units2": area, "textures": len(tex),
            "texels_used": px, "texel_size_units": float(np.sqrt(area / px)) if px else None}


if __name__ == "__main__":
    a = sys.argv[1:]
    build(a[0], [float(a[1]), float(a[2]), float(a[3])], float(a[4]), Path(a[5]),
          refine="--no-refine" not in a)


async def publish(src_inspection: str, region_dir: Path, name: str) -> str:
    """Region model → its own inspection (same structure) so it opens in the
    viewer: photos shared with the source (symlink), cameras of the source
    (same frame), LODs + streamed tiles + quality report like the pipeline."""
    from datetime import date, datetime
    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.models import Inspection, PhotoSet

    region_dir = Path(region_dir)
    async with AsyncSessionLocal() as db:
        src = await db.get(Inspection, src_inspection)
        insp = Inspection(structure_id=src.structure_id, name=name,
                          inspection_date=src.inspection_date or date.today(), method=src.method)
        db.add(insp)
        await db.commit()
        new_id = insp.id
    # storage on the data SSD (like the drone inspection), linked into uploads/
    base = Path(settings.work_dir).parent / "inspections" / new_id if settings.work_dir else None
    link = settings.upload_path / "inspections" / new_id
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)
        if link.exists() and not link.is_symlink():
            link.rmdir()
        if not link.exists():
            link.symlink_to(base)
    root = link.resolve()
    lod0 = await _install_model(src_inspection, region_dir, root)
    photos = root / "photos"
    async with AsyncSessionLocal() as db:
        db.add(PhotoSet(inspection_id=new_id, status="completed",
                        photo_count=sum(1 for _ in photos.iterdir()),
                        upload_dir=str(photos), model_path=storage.get_relative_path(lod0),
                        engine_used="hires-region", progress=100, completed_at=datetime.utcnow()))
        await db.commit()
    return new_id


async def _install_model(src_inspection: str, region_dir: Path, root: Path,
                         region_dirs: list[Path] | None = None) -> Path:
    """Region files → root/model/meshroom_out, LODs, tiles, quality report.
    region_dirs (the regions merged into region_dir): LODs are written from
    them directly (regions_gltf) — gltfpack cannot open a whole-bridge OBJ."""
    import subprocess
    from app.services import tiles
    from app.services.photogrammetry.postprocess.lods import generate_lods
    (root / "model").mkdir(parents=True, exist_ok=True)
    photos = root / "photos"
    if not photos.exists():
        photos.symlink_to(storage.get_photos_dir(src_inspection).resolve())
    out = root / "model" / "meshroom_out"
    out.mkdir(exist_ok=True)
    for f in out.glob("texture*"):
        f.unlink()
    for f in region_dir.iterdir():
        if f.is_file() and (f.name.startswith("texture") or f.suffix in (".obj", ".mtl", ".json")):
            try:                                   # same disk: a hard link, not a 3,6 GB copy
                (out / f.name).unlink(missing_ok=True)
                os.link(f, out / f.name)
            except OSError:
                shutil.copy2(f, out / f.name)
    src_out = next(storage.get_models_dir(src_inspection).glob("**/meshroom_out"))
    shutil.copy2(src_out / "cameras.sfm", out / "cameras.sfm")
    if not (out / "sfm.json").exists():
        shutil.copy2(src_out / "sfm.json", out / "sfm.json")
    if region_dirs:
        from app.services import regions_gltf
        lod0 = await asyncio.to_thread(regions_gltf.write_lods, [Path(d) for d in region_dirs], out)
    else:
        lod0 = await generate_lods(out / "texturedMesh.obj")
    if lod0 is None:
        raise RuntimeError("glTF LODs could not be written")
    # tiles + quality report in their own processes (heavy)
    subprocess.run(tiles.build_command(lod0), cwd=tiles.BACKEND_DIR,
                   stdout=open(out / "tiles_build.log", "w"), stderr=subprocess.STDOUT)
    subprocess.run([sys.executable, "-m", "app.services.quality_report", str(out), str(photos)],
                   cwd=tiles.BACKEND_DIR, stdout=open(out / "quality_report.log", "w"),
                   stderr=subprocess.STDOUT)
    return lod0


async def update_published(inspection_id: str, src_inspection: str, region_dir: Path,
                           region_dirs: list[Path] | None = None) -> Path:
    """Replace the model of an inspection made by publish() (same id / link)."""
    root = storage.get_models_dir(inspection_id).parent.resolve()
    return await _install_model(src_inspection, Path(region_dir), root, region_dirs)


def scene_frame(inspection_id: str):
    """(scale, offset) of the viewer's scene normalization for the inspection's lod0."""
    from app.services.registration import display_transform
    lod0 = next(storage.get_models_dir(inspection_id).glob("**/model_lod0.gltf"), None)
    return display_transform(lod0) if lod0 else None


async def refresh_published(inspection_id: str, src_inspection: str, old_frame=None, log=print) -> dict:
    """After a published model grew (more regions merged in), its scene
    normalization (5-unit box of the CURRENT bounds) changed: re-express the
    stored annotations / measurements in the new scene frame so they stay on
    the surface, and set the metric scale from the source inspection (same SfM
    frame: metres per mesh unit carry over)."""
    from app.database import AsyncSessionLocal
    from app.models import Annotation, Inspection, Measurement
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    s_new, off_new = scene_frame(inspection_id)
    off_new = np.array(off_new)
    out = {"rebased": 0}
    async with AsyncSessionLocal() as db:
        insp = await db.get(Inspection, inspection_id)
        src = await db.get(Inspection, src_inspection)
        if src and src.scale_factor and abs(src.scale_factor - 1.0) > 1e-9:
            s_src, _ = scene_frame(src_inspection)
            insp.scale_factor = src.scale_factor * s_src / s_new
            out["scale_factor"] = insp.scale_factor
        if old_frame is not None:
            s_old, off_old = old_frame[0], np.array(old_frame[1])
            if abs(s_old - s_new) > 1e-12 or np.abs(off_old - off_new).max() > 1e-12:
                f = lambda x, y, z: ((np.array([x, y, z]) - off_old) / s_old) * s_new + off_new

                def pts(lst):
                    res = []
                    for q in lst or []:
                        if isinstance(q, dict):
                            v = f(q["x"], q["y"], q["z"]); res.append({**q, "x": float(v[0]), "y": float(v[1]), "z": float(v[2])})
                        else:
                            v = f(*q[:3]); res.append([float(v[0]), float(v[1]), float(v[2]), *q[3:]])
                    return res
                for a in (await db.execute(select(Annotation).where(Annotation.inspection_id == inspection_id))).scalars():
                    if a.position_x is not None:
                        a.position_x, a.position_y, a.position_z = map(float, f(a.position_x, a.position_y, a.position_z))
                    if isinstance(a.geometry_json, list):
                        a.geometry_json = pts(a.geometry_json); flag_modified(a, "geometry_json")
                    out["rebased"] += 1
                for m in (await db.execute(select(Measurement).where(Measurement.inspection_id == inspection_id))).scalars():
                    if isinstance(m.points_json, list):
                        m.points_json = pts(m.points_json); flag_modified(m, "points_json")
                        out["rebased"] += 1
        await db.commit()
    log(f"refresh {inspection_id}: {out}")
    return out


def link_detections(inspection_id: str, src_inspection: str) -> bool:
    """AI defect detections are per photo; a published region model shares the
    source's photos → reuse the source's detections (no second GPU pass)."""
    from app.services import defects_auto
    src = defects_auto.out_dir(src_inspection)
    dst = defects_auto.out_dir(inspection_id)
    if not (src / "detections.json").exists():
        return False
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.name.startswith("status"):
            continue
        t = dst / f.name
        if not t.exists():
            t.symlink_to(f.resolve())
    return True


def denoise_mesh(work: Path, mesh: str, log=print, **kw) -> str:
    """AI denoising of the refined mesh (DSINE normals of every photo → normal
    prior, app.services.normal_prior): MVS noise on smooth concrete — the cm
    bumps and dents — goes, edges and real relief the network sees stay.
    Best effort: on failure the mesh is returned unchanged."""
    import subprocess
    work = Path(work)
    out = work / "mesh_ai.ply"
    try:
        nd = work / "normals"
        n_img = len(list((work / "images").glob("*.jpg")))
        if not nd.is_dir() or len(list(nd.glob("*.npy"))) < n_img:
            with open(work / "openmvs.log", "a") as fh:
                O.predict_normals(work, nd, 1344, fh)
        args = [sys.executable, "-m", "app.services.normal_prior", str(work / mesh), str(out), str(work), str(nd)]
        if kw:
            args.append(json.dumps(kw))
        # own process: the mesh + sparse operators need several GB of RAM
        with open(work / "openmvs.log", "a") as fh:
            r = subprocess.run(args, cwd=Path(__file__).resolve().parents[2], stdout=fh, stderr=subprocess.STDOUT)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"normal_prior rc={r.returncode}")
        log(f"hires: AI denoise done ({mesh} → mesh_ai.ply)")
        return "mesh_ai.ply"
    except Exception as e:  # noqa: BLE001
        log(f"hires: AI denoise skipped: {e}")
        return mesh


def fill_small_holes(src: Path, dst: Path, max_perimeter: float = 1.5) -> dict:
    """Close interior holes up to `max_perimeter` (SfM units ≈ m); the long
    open borders of a Voronoi cell stay open."""
    import pymeshlab
    import trimesh
    m = trimesh.load(src, force="mesh", process=False)
    edge = float(np.median(m.edges_unique_length)) if len(m.faces) else 0.01
    max_edges = int(max(30, max_perimeter / max(edge, 1e-6)))
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(src))
    n0 = ms.current_mesh().face_number()
    try:
        ms.meshing_close_holes(maxholesize=max_edges, newfaceselected=False)
    except Exception:  # noqa: BLE001 — non-manifold input: keep the mesh as is
        pass
    ms.save_current_mesh(str(dst), save_vertex_color=False, save_face_color=False)
    return {"added_faces": ms.current_mesh().face_number() - n0, "max_edges": max_edges}


def refinish(inspection_id: str, region_dir: Path, centre_mesh, radius: float, others_mesh, log=print) -> dict:
    """Finished region again from its refined mesh: AI denoise → Voronoi cell →
    small holes → photo-resolution texture (dense cloud / refine reused)."""
    region_dir = Path(region_dir); work = region_dir / "work"
    c = np.array(centre_mesh, float) * [1, -1, -1]
    others = np.asarray(others_mesh, float) * [1, -1, -1]
    mesh = denoise_mesh(work, "mesh_refined.ply", log)
    st = {"denoised": mesh == "mesh_ai.ply"}
    st["cell"] = crop_mesh(work / mesh, work / "mesh_cell.ply", c, radius, others, margin=CELL_OVERLAP)
    st["holes"] = fill_small_holes(work / "mesh_cell.ply", work / "mesh_final.ply")
    st["bake"] = rebake(inspection_id, region_dir, log=log)
    return st


def tex_images(inspection_id: str, work: Path, log=print) -> Path:
    """Undistorted ORIGINAL photos for texturing → work/tex/images (same names
    and intrinsics as work/images, which come from the prepared photos)."""
    import cv2
    work = Path(work)
    tex = work / "tex"
    sub = json.loads((work / "sub_sfm.json").read_text())
    if (tex / "images").is_dir() and len(list((tex / "images").iterdir())) == len(sub["views"]):
        return tex / "images"
    photos = storage.get_photos_dir(inspection_id)
    for v in sub["views"]:
        orig = photos / Path(v["path"]).name
        if orig.exists():
            v["path"] = str(orig)
    tex.mkdir(exist_ok=True)
    (tex / "sub_sfm.json").write_text(json.dumps(sub))
    O.export_colmap(tex / "sub_sfm.json", tex, quality=98, interp=cv2.INTER_LANCZOS4)
    log(f"hires: {len(sub['views'])} original photos undistorted for texturing")
    return tex / "images"


def rebake(inspection_id: str, region_dir: Path, out_dir: Path | None = None, log=print, **kw) -> dict:
    """Photo-resolution texture of a finished region again (mesh reused)."""
    from app.services import hires_texture
    region_dir = Path(region_dir); out_dir = Path(out_dir or region_dir)
    work = region_dir / "work"
    tex = tex_images(inspection_id, work, log)
    for f in out_dir.glob("texture_*"):
        f.unlink()
    st = hires_texture.bake(work, work / "mesh_final.ply", out_dir, images_dir=tex, log=log, **kw)
    if out_dir != region_dir:
        shutil.copy2(region_dir / "sfm.json", out_dir / "sfm.json")
        shutil.copy2(region_dir / "hires.json", out_dir / "hires.json")
    return st


def retexture(region_dir: Path, out_dir: Path, tex_args: tuple = (), log=print) -> dict:
    """Texture the finished region mesh again with other TextureMesh options
    (the expensive dense/refine steps are reused)."""
    region_dir, out_dir = Path(region_dir), Path(out_dir)
    work = region_dir / "work"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = out_dir.name
    with open(work / "openmvs.log", "a") as lf:
        O._mvs(work, "TextureMesh", "scene_dense.mvs", "--mesh-file", "mesh_final.ply",
               "--export-type", "obj", "--resolution-level", "0", "--max-texture-size", "8192",
               "--empty-color", str(EMPTY_COLOR), *tex_args, "-o", f"textured_{tag}.obj", log=lf)
    for f in out_dir.glob("texture_*"):
        f.unlink()
    _to_app_frame(work / f"textured_{tag}.obj", out_dir)
    shutil.copy2(region_dir / "sfm.json", out_dir / "sfm.json")
    st = json.loads((region_dir / "hires.json").read_text())
    st.update(texture_stats(out_dir), tex_args=list(tex_args))
    (out_dir / "hires.json").write_text(json.dumps(st, indent=1))
    log(f"retexture {tag}: {st['textures']} textures, texel {st['texel_size_units']:.4f}")
    return st
