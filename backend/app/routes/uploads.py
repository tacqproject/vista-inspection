"""
Photo upload and 3D reconstruction endpoints.
"""

import asyncio
import io
import os
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, AsyncSessionLocal
from app.models import Inspection, PhotoSet
from app.schemas import PhotoSetOut
from app.services import storage, photogrammetry

router = APIRouter(prefix="/inspections/{inspection_id}", tags=["uploads"])


async def _get_inspection_or_404(inspection_id: str, db: AsyncSession) -> Inspection:
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    return insp


async def _decanonicalize(inspection_id: str, db: AsyncSession) -> None:
    """
    A regenerated / re-cropped / re-uploaded model is back in the raw
    photogrammetry frame, so it must NOT be treated as baked-into-the-canonical
    frame anymore (else the viewer skips normalization and the scale/placement
    goes wild). Clear the flag and drop the now-stale `.orig` bake backups so a
    future re-bake backs up the NEW model, not the old one.
    """
    from app.config import settings
    from app.services import canonicalize

    insp = await db.get(Inspection, inspection_id)
    if insp and insp.canonical:
        insp.canonical = False

    # If the REFERENCE model is rebuilt, the whole structure's canonical frame is
    # gone — every inspection baked into it is now orphaned. Drop the reference
    # and un-canonicalize the rest so nothing renders in a stale frame.
    if insp:
        from app.models import Structure
        struct = await db.get(Structure, insp.structure_id)
        if struct and struct.reference_inspection_id == inspection_id:
            struct.reference_inspection_id = None
            others = (await db.execute(
                select(Inspection).where(
                    Inspection.structure_id == insp.structure_id,
                    Inspection.canonical == True,   # noqa: E712
                )
            )).scalars().all()
            for o in others:
                # Their geometry is still physically baked into the OLD reference's
                # frame; just clearing the flag would make the viewer re-normalize
                # already-baked coordinates (wildly wrong placement/scale). Restore
                # each peer to its pristine geometry from its own .orig backups.
                peer_dir = settings.upload_path / "inspections" / o.id / "model"
                if peer_dir.exists():
                    await asyncio.to_thread(canonicalize.restore_from_orig, peer_dir)
                o.canonical = False

    model_dir = settings.upload_path / "inspections" / inspection_id / "model"
    if model_dir.exists():
        # Streamed tiles encode the model's frame relative to its .orig backups,
        # which go away below — and the model itself is being regenerated. Drop
        # them; they are rebuilt for the new model (viewer uses LODs meanwhile).
        from app.services import tiles as tiles_svc
        await asyncio.to_thread(tiles_svc.remove_tiles, model_dir)
        for orig in model_dir.rglob("*.orig"):
            try:
                orig.unlink()
            except OSError:
                pass


async def _remove_photo_set(inspection_id: str, db: AsyncSession):
    existing = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == inspection_id))
    ps = existing.scalar_one_or_none()
    if ps:
        await db.delete(ps)
        await db.commit()


# ─── Photo upload ─────────────────────────────────────────────────────────────

@router.post("/upload", response_model=PhotoSetOut, status_code=202)
async def upload_photos(
    inspection_id: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(default=[]),
    append: bool = False,
    start: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Upload individual photos or a ZIP archive. Triggers 3D reconstruction.

    Hundreds of drone photos go in batches: first batch append=false,
    start=false (replaces the photo set), further batches append=true,
    start=false, the last one append=true, start=true (starts the run)."""
    await _get_inspection_or_404(inspection_id, db)

    # Don't wipe the photo set out from under a running reconstruction: the task
    # holds this row's id and would silently no-op while its Meshroom subprocess
    # keeps writing into the same models_dir, racing the new run.
    existing = (await db.execute(
        select(PhotoSet).where(PhotoSet.inspection_id == inspection_id)
    )).scalar_one_or_none()
    if existing and existing.status == "processing":
        raise HTTPException(409, "Rekonstrukce právě probíhá — počkejte na její dokončení.")

    if append and existing:
        ps = existing
        ps.status = "uploading"
    else:
        await _remove_photo_set(inspection_id, db)
        ps = PhotoSet(inspection_id=inspection_id, status="uploading")
        db.add(ps)
    await db.commit()
    await db.refresh(ps)

    from app.services import video_frames
    for upload in files:
        content = await upload.read()
        ext = Path(upload.filename or "").suffix.lower()
        # All three branches touch disk (zip extract / frame extract / write each
        # file) — run off the event loop so a large upload can't stall the server.
        if ext == ".zip":
            await asyncio.to_thread(storage.extract_zip, inspection_id, content)
        elif ext in video_frames.VIDEO_EXTENSIONS:
            # Sample the video into sharp still frames, then treat as photos.
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
                tf.write(content)
                tmp_path = Path(tf.name)
            try:
                await asyncio.to_thread(
                    video_frames.extract_frames,
                    tmp_path, storage.get_photos_dir(inspection_id),
                )
            finally:
                os.unlink(tmp_path)
        else:
            await asyncio.to_thread(
                storage.save_uploaded_photos, inspection_id, [(upload.filename, content)]
            )

    # Clear thumbnail cache when new photos arrive
    await asyncio.to_thread(_clear_thumbnail_cache, inspection_id)

    ps.photo_count = await asyncio.to_thread(storage.count_photos, inspection_id)
    ps.upload_dir = str(storage.get_photos_dir(inspection_id))
    if not start:                              # more batches to come
        await db.commit()
        await db.refresh(ps)
        return PhotoSetOut.model_validate(ps)
    ps.status = "processing"
    await _decanonicalize(inspection_id, db)   # brand-new model → raw frame
    await db.commit()

    background_tasks.add_task(_run_reconstruction, inspection_id, ps.id)

    await db.refresh(ps)
    return PhotoSetOut.model_validate(ps)


_DETECT_TASKS: dict[str, asyncio.Task] = {}   # keep references (GC would cancel them)


async def _run_reconstruction(
    inspection_id: str,
    photo_set_id: str,
    params: dict | None = None,
):
    from app.config import settings as _settings
    # plain uploads get the full automatic treatment; explicit options win
    params = {"quality": _settings.default_quality, "ai_normals": True,
              "ai_defects": _settings.auto_defects, **(params or {})}
    if params.get("ai_defects"):
        # defect detection on the photos runs in parallel with the 3D model;
        # the "ai-defects" pipeline step waits for it and places the defects
        from app.services import defects_auto
        _DETECT_TASKS[inspection_id] = asyncio.create_task(
            asyncio.to_thread(defects_auto.detect_if_needed, inspection_id))

    async with AsyncSessionLocal() as db:
        ps = await db.get(PhotoSet, photo_set_id)
        if not ps:
            return

        async def on_progress(pct: int, message: str):
            ps.progress = pct
            from app.ws_manager import ws_manager
            await ws_manager.broadcast(
                inspection_id,
                {"type": "progress", "progress": pct, "message": message},
            )
            await db.commit()

        try:
            result = await photogrammetry.run_reconstruction(
                inspection_id, on_progress, params=params
            )
            ps.status = "completed"
            ps.progress = 100
            ps.model_path = result.get("model_path")
            ps.point_cloud_path = result.get("point_cloud_path")
            ps.orthophoto_path = result.get("orthophoto_path")
            ps.engine_used = result.get("engine_used")
            ps.completed_at = datetime.utcnow()
            from app.ws_manager import ws_manager
            await ws_manager.broadcast(
                inspection_id,
                {"type": "completed", "progress": 100, "model_path": ps.model_path},
            )
        except Exception as e:
            ps.status = "failed"
            ps.error_message = str(e)
            from app.ws_manager import ws_manager
            await ws_manager.broadcast(inspection_id, {"type": "error", "message": str(e)})

        await db.commit()


# ─── Reprocess (retry) reconstruction ─────────────────────────────────────────

class ReprocessIn(BaseModel):
    quality: str = "standard"          # draft | standard | high | max
    keep_largest_mesh: bool = True     # drop floating fragments
    max_points: int | None = None      # mesh density override
    texture_size: int | None = None    # 4096 / 8192
    refine_mesh: bool = False          # PyMeshLab cleanup + separate texturing
    refine_faces: int | None = None    # decimation target (default 600k)
    pipeline: str | None = None        # imported Meshroom .mg template filename
    web_postprocess: bool = True       # textures/LODs/mobile chain
    ai_normals: bool = True            # max: AI (DSINE) normal-guided denoising
    ai_defects: bool = True            # AI defect detection on photos → annotations
    photo_prep: bool = True            # exposure balance, shadow lift, cull blurred/duplicates


@router.post("/reprocess", response_model=PhotoSetOut, status_code=202)
async def reprocess(
    inspection_id: str,
    background_tasks: BackgroundTasks,
    body: ReprocessIn | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Re-run 3D reconstruction on already uploaded photos with optional settings."""
    await _get_inspection_or_404(inspection_id, db)

    result = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == inspection_id))
    ps = result.scalar_one_or_none()
    if not ps:
        raise HTTPException(404, "Žádná sada snímků — nejprve nahrajte fotografie")
    if ps.status == "processing":
        raise HTTPException(409, "Rekonstrukce již probíhá")
    if storage.count_photos(inspection_id) == 0:
        raise HTTPException(400, "Nebyly nalezeny žádné zdrojové snímky")

    ps.status = "processing"
    ps.progress = 0
    ps.error_message = None
    ps.model_path = None
    ps.point_cloud_path = None
    ps.orthophoto_path = None
    ps.completed_at = None
    await _decanonicalize(inspection_id, db)   # new model → back to raw frame
    await db.commit()
    await db.refresh(ps)

    body = body or ReprocessIn()
    background_tasks.add_task(
        _run_reconstruction, inspection_id, ps.id, body.model_dump()
    )
    return PhotoSetOut.model_validate(ps)


# ─── Import pre-built OBJ model ───────────────────────────────────────────────

@router.post("/import-model", response_model=PhotoSetOut, status_code=201)
async def import_model(
    inspection_id: str,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Import a finished 3D model — a photogrammetry OBJ (+ MTL + textures) or a
    reality-scan export: GLB / glTF (self-contained), FBX, or PLY (mesh or point
    cloud). Sources: RealityScan/RealityCapture, Polycam, Scaniverse, Luma AI,
    Meshroom, COLMAP, …
    """
    await _get_inspection_or_404(inspection_id, db)

    ACCEPTED = ("obj", "glb", "gltf", "fbx", "ply")
    # Validate BEFORE destroying anything: if no usable mesh is in the upload,
    # reject now. Otherwise _remove_photo_set (which commits) + _decanonicalize
    # would already have wiped the existing model when we 400 below, leaving the
    # inspection with no model and half-updated canonical flags.
    if not any(Path(u.filename or "").suffix.lower().lstrip(".") in ACCEPTED for u in files):
        raise HTTPException(
            400, "Nahrajte model: OBJ (+MTL+textury), nebo GLB/glTF/FBX/PLY z reality scanu.",
        )

    await _remove_photo_set(inspection_id, db)
    await _decanonicalize(inspection_id, db)   # imported model → raw frame

    models_dir = storage.get_models_dir(inspection_id)
    # first matching file per format; a single self-contained mesh wins over OBJ
    found: dict[str, Path] = {}

    for upload in files:
        safe_name = Path(upload.filename).name  # strip any directory prefix
        content = await upload.read()
        dest = models_dir / safe_name
        await asyncio.to_thread(dest.write_bytes, content)   # large GLB/PLY off-loop
        ext = dest.suffix.lower().lstrip(".")
        if ext in ACCEPTED and ext not in found:
            found[ext] = dest

    # priority: self-contained web formats first, then OBJ, then others
    mesh_path = next((found[e] for e in ("glb", "gltf", "obj", "fbx", "ply") if e in found), None)
    if mesh_path is None:
        raise HTTPException(
            400, "Nahrajte model: OBJ (+MTL+textury), nebo GLB/glTF/FBX/PLY z reality scanu.",
        )

    ps = PhotoSet(
        inspection_id=inspection_id,
        status="completed",
        photo_count=0,
        model_path=storage.get_relative_path(mesh_path),
        engine_used="import",
        progress=100,
        completed_at=datetime.utcnow(),
    )
    db.add(ps)
    await db.commit()
    await db.refresh(ps)
    return PhotoSetOut.model_validate(ps)


# ─── Photo set info ───────────────────────────────────────────────────────────

@router.get("/photo-set", response_model=PhotoSetOut)
async def get_photo_set(inspection_id: str, db: AsyncSession = Depends(get_db)):
    await _get_inspection_or_404(inspection_id, db)
    result = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == inspection_id))
    ps = result.scalar_one_or_none()
    if not ps:
        raise HTTPException(404, "Žádná sada snímků")
    return PhotoSetOut.model_validate(ps)


# ─── Source photo listing ─────────────────────────────────────────────────────

@router.get("/photos")
async def list_photos(inspection_id: str):
    """List all source photos uploaded for this inspection."""
    photos_dir = storage.get_photos_dir(inspection_id)
    if not photos_dir.exists():
        return {"photos": [], "total": 0}

    photos = []
    for f in sorted(photos_dir.iterdir()):
        if f.suffix.lower() in storage.ALLOWED_IMAGE_EXTENSIONS:
            rel = storage.get_relative_path(f)
            photos.append({
                "filename": f.name,
                "url": f"/uploads/{rel}",
                "size": f.stat().st_size,
            })
    return {"photos": photos, "total": len(photos)}


# ─── Crop model to region of interest ─────────────────────────────────────────

class CropModelIn(BaseModel):
    points: list[dict] | None = None   # polygon [{x,y,z},...] in model coords
    restore: bool = False              # true → go back to the full model


@router.post("/crop-model", response_model=PhotoSetOut)
async def crop_model(
    inspection_id: str,
    body: CropModelIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Crop the 3D model to the polygon-selected region (delete everything else),
    or restore the original full model. Textures and UVs are preserved.
    """
    import time
    from app.services import mesh_edit

    await _get_inspection_or_404(inspection_id, db)
    result = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == inspection_id))
    ps = result.scalar_one_or_none()
    if not ps or not ps.model_path:
        raise HTTPException(404, "Žádný model k oříznutí")

    # cropping/restoring rebuilds the model in the raw frame → drop canonical state
    await _decanonicalize(inspection_id, db)

    models_dir = storage.get_models_dir(inspection_id)
    obj_files = sorted(models_dir.glob("**/texturedMesh.obj"))
    if not obj_files:
        raise HTTPException(400, "Původní OBJ model nebyl nalezen — ořez není možný")
    obj_file = obj_files[0]

    if body.restore:
        lod0 = obj_file.parent / "model_lod0.gltf"
        if not lod0.exists():
            raise HTTPException(400, "Původní LOD soubory nebyly nalezeny")
        ps.model_path = storage.get_relative_path(lod0)
        await db.commit()
        await db.refresh(ps)
        from app.services import tiles as tiles_svc
        tiles_svc.spawn_build(lod0)            # tiles were dropped by _decanonicalize
        return PhotoSetOut.model_validate(ps)

    if not body.points or len(body.points) < 3:
        raise HTTPException(400, "Polygon musí mít alespoň 3 body")

    tag = f"crop{int(time.time())}"
    cropped_obj = obj_file.parent / f"texturedMesh_{tag}.obj"
    kept = await asyncio.to_thread(mesh_edit.crop_obj, obj_file, body.points, cropped_obj)
    if kept == 0:
        cropped_obj.unlink(missing_ok=True)
        raise HTTPException(400, "Ve vybrané oblasti nejsou žádné plochy modelu")

    # Generate LODs for the cropped mesh (same dir → textures resolve)
    lod0 = await photogrammetry.generate_lods(cropped_obj, prefix=f"model_{tag}")
    cropped_obj.unlink(missing_ok=True)
    if not lod0:
        raise HTTPException(500, "Generování LOD pro oříznutý model selhalo")

    # mobile (2K texture) variant for constrained devices
    from app.services.photogrammetry.postprocess.mobile import _build as build_mobile
    await asyncio.to_thread(build_mobile, lod0.parent)

    ps.model_path = storage.get_relative_path(lod0)
    await db.commit()
    await db.refresh(ps)
    from app.services import tiles as tiles_svc
    tiles_svc.spawn_build(lod0)                # stream the cropped model too
    return PhotoSetOut.model_validate(ps)


# ─── Streamed tiles ───────────────────────────────────────────────────────────

@router.post("/tiles/rebuild", status_code=202)
async def rebuild_tiles(inspection_id: str, db: AsyncSession = Depends(get_db)):
    """(Re)build the streamed 3D tiles of the active model in the background.
    The viewer keeps using the whole-file LODs until the new tileset is ready."""
    from app.config import settings
    from app.services import tiles as tiles_svc

    ps = (await db.execute(
        select(PhotoSet).where(PhotoSet.inspection_id == inspection_id)
    )).scalar_one_or_none()
    if not ps or not ps.model_path or not ps.model_path.endswith("_lod0.gltf"):
        raise HTTPException(400, "Dlaždice lze vytvořit jen pro rekonstruovaný model (glTF LOD).")
    lod0 = settings.upload_path / ps.model_path
    if not lod0.exists():
        raise HTTPException(404, "Soubor modelu nenalezen")
    tiles_svc.spawn_build(lod0)
    return {"status": "building", "tileset": storage.get_relative_path(tiles_svc.tiles_dir_for(lod0) / "tileset.json")}


# ─── Quality report ───────────────────────────────────────────────────────────

@router.get("/quality")
async def quality_report(inspection_id: str, db: AsyncSession = Depends(get_db)):
    """Objective quality report of the reconstruction (metrics + renders from the
    viewpoints of source photos), produced by the quality-report pipeline step."""
    import json as _json
    from app.config import settings

    ps = (await db.execute(
        select(PhotoSet).where(PhotoSet.inspection_id == inspection_id)
    )).scalar_one_or_none()
    if not ps or not ps.model_path:
        raise HTTPException(404, "Model neexistuje")
    qdir = (settings.upload_path / ps.model_path).parent / "quality"
    rep = qdir / "report.json"
    if not rep.exists():
        return {"available": False}
    data = _json.loads(await asyncio.to_thread(rep.read_text))
    for r in data.get("renders", []):
        if r.get("file"):
            r["url"] = f"/uploads/{storage.get_relative_path(qdir / r['file'])}"
    data["available"] = True
    data["engine"] = ps.engine_used
    prep = storage.get_models_dir(inspection_id) / "photo_prep.json"
    if prep.exists():
        data["photo_prep"] = _json.loads(prep.read_text())
    return data


# ─── Measure in photo space ──────────────────────────────────────────────────

class MeasurePhotoIn(BaseModel):
    filename: str
    points: list[dict]   # [{u, v}, ...] normalized image coords


@router.post("/measure-photo")
async def measure_photo(inspection_id: str, body: MeasurePhotoIn, db: AsyncSession = Depends(get_db)):
    """
    Back-project clicked photo pixels onto the mesh and return their 3D
    positions (mesh frame) + pairwise distance in mesh units. The frontend
    converts to meters via the display scale and calibration factor.
    """
    from app.services import sfm
    import math as _math

    await _get_inspection_or_404(inspection_id, db)
    models_dir = storage.get_models_dir(inspection_id)
    sfm_files = list(models_dir.glob("**/cameras.sfm"))
    mesh_file = next(iter(models_dir.glob("**/model_lod0.gltf")), None)
    if not sfm_files or not mesh_file:
        raise HTTPException(404, "Kamery nebo model nejsou k dispozici")

    cameras = sfm.load_cameras(sfm_files[0])
    try:
        results = await asyncio.to_thread(
            sfm.pixel_to_mesh, cameras, mesh_file, body.filename,
            [(p["u"], p["v"]) for p in body.points],
        )
    except ValueError as e:
        raise HTTPException(404, str(e))

    distance = None
    pts = [r["point"] for r in results if r["hit"]]
    if len(pts) == 2:
        distance = _math.dist(pts[0], pts[1])

    return {"results": results, "distance_model": distance}


# ─── Sparse point cloud (SfM landmarks) ──────────────────────────────────────

@router.get("/pointcloud")
async def get_pointcloud(inspection_id: str, db: AsyncSession = Depends(get_db)):
    """
    Sparse SfM landmark cloud in the MESH frame as a compact binary:
    [uint32 count][float32 xyz × count][uint8 rgb × count].
    Built once from sfm.json and cached next to the model.
    """
    await _get_inspection_or_404(inspection_id, db)
    models_dir = storage.get_models_dir(inspection_id)
    sfm_json = next(iter(models_dir.glob("**/sfm.json")), None)
    if not sfm_json:
        raise HTTPException(404, "SfM landmarky nejsou k dispozici — spusťte rekonstrukci znovu")

    cache = sfm_json.parent / "pointcloud.bin"
    if not cache.exists() or cache.stat().st_mtime < sfm_json.stat().st_mtime:
        await asyncio.to_thread(_build_pointcloud, sfm_json, cache)

    return FileResponse(cache, media_type="application/octet-stream")


def _build_pointcloud(sfm_json, out_path):
    import json as _json
    import struct
    import numpy as np

    data = _json.loads(sfm_json.read_text())
    landmarks = data.get("structure", [])
    pos = np.empty((len(landmarks), 3), dtype=np.float32)
    col = np.empty((len(landmarks), 3), dtype=np.uint8)
    for i, lm in enumerate(landmarks):
        x, y, z = (float(v) for v in lm["X"])
        pos[i] = (x, -y, -z)   # SfM frame → mesh frame
        c = lm.get("color", [200, 200, 200])
        col[i] = c
    with open(out_path, "wb") as f:
        f.write(struct.pack("<I", len(landmarks)))
        f.write(pos.tobytes())
        f.write(col.tobytes())


# ─── Camera poses for the viewer (Meshroom-style frustums) ───────────────────

@router.get("/cameras")
async def list_cameras(inspection_id: str, db: AsyncSession = Depends(get_db)):
    """
    Reconstructed camera poses in the MESH (viewer) frame: position, forward
    and up direction, FOV and the photo filename. Used to render frustums.
    """
    from app.services import sfm

    await _get_inspection_or_404(inspection_id, db)
    models_dir = storage.get_models_dir(inspection_id)
    sfm_files = list(models_dir.glob("**/cameras.sfm"))
    if not sfm_files:
        raise HTTPException(404, "Pozice kamer nejsou k dispozici")

    cameras = sfm.load_cameras(sfm_files[0])
    out = []
    for c in cameras:
        r = c.r
        # camera axes in world = columns of R (X_cam = Rᵀ(X−C));
        # convert SfM frame → mesh frame (y/z negated)
        fwd = sfm.mesh_to_sfm(r[2], r[5], r[8])   # +Z = viewing direction (CV)
        up = sfm.mesh_to_sfm(-r[1], -r[4], -r[7])  # −Y = up (CV y points down)
        pos = sfm.mesh_to_sfm(*c.c)
        out.append({
            "filename": c.filename,
            "position": pos,
            "forward": fwd,
            "up": up,
            # principal point offset as a fraction of image size — lets the
            # viewer align a photo overlay with the (centered) render
            "pp": [c.ppx / c.width, c.ppy / c.height],
            "aspect": c.width / c.height,
            "fov_deg": 2 * __import__("math").degrees(
                __import__("math").atan((c.height / 2) / c.fy)
            ),
        })
    return {"cameras": out, "total": len(out)}


# ─── Project a 3D point into source photos ────────────────────────────────────


class ProjectPointIn(BaseModel):
    x: float
    y: float
    z: float
    # surface normal at the click (mesh frame) — cameras viewing the surface
    # from behind are rejected (ray occlusion can't catch back-face views
    # past the building's silhouette)
    nx: float | None = None
    ny: float | None = None
    nz: float | None = None
    # optional polygon boundary (mesh frame) — when given, each returned photo
    # also carries the outline projected into it, so the defect is framed on the
    # photos exactly as on the 3D model
    outline: list[list[float]] | None = None


def _project_point_sync(sfm_path: Path, body: ProjectPointIn) -> dict:
    """CPU-bound part of /project-point (loads the mesh + casts rays). Runs in a
    worker thread — it must NEVER execute on the event loop, because it acquires
    the raycasting lock (``_RAY_LOCK``); blocking on that lock from the loop
    thread freezes the whole server while a timeline/other request holds it."""
    from app.services import sfm

    # The click arrives in the mesh/viewer frame; cameras live in the SfM frame
    sx, sy, sz = sfm.mesh_to_sfm(body.x, body.y, body.z)

    # Verify each marker actually lands on the clicked point (back-projection on
    # the FULL mesh lod0): drops cameras that are occluded OR mis-calibrated, so
    # a photo never shows the marker on some other surface. Coarse LODs would
    # miss fine occluders. Mesh loads fast (skip_materials) and _RAY_LOCK keeps
    # rtree thread-safe; measure-photo uses lod0 too so the cache is shared.
    verify_mesh = next(
        (sfm_path.parent / f"model_{lod}.gltf"
         for lod in ("lod0", "lod1", "lod2")
         if (sfm_path.parent / f"model_{lod}.gltf").exists()),
        None,
    )

    # Surface normal for the visibility-angle filter: use the click's normal, else
    # derive it from the mesh so saved defects (no stored normal) also reject
    # cameras behind / grazing the surface.
    if body.nx is not None:
        normal = sfm.mesh_to_sfm(body.nx, body.ny, body.nz)
    elif verify_mesh is not None:
        nm = sfm.surface_normal(verify_mesh, body.x, body.y, body.z)
        normal = sfm.mesh_to_sfm(*nm) if nm else None
    else:
        normal = None

    cameras = sfm.load_cameras(sfm_path)
    matches = sfm.project_point(cameras, sx, sy, sz, normal=normal)

    occluded = False
    if verify_mesh and matches:
        # Verify the best-ranked candidates (rays are batched — 150 is cheap):
        # after dropping occluded ones there must still be enough good photos.
        head = matches[:150]
        try:
            visible = sfm.filter_by_backprojection(
                list(head), cameras, verify_mesh, body.x, body.y, body.z)
            if visible:
                matches = visible
            else:
                # Nothing verified — the click likely fell through a hole in the
                # mesh (e.g. unreconstructed doorway) to a point inside. Show
                # approximate results (flagged) rather than an empty answer.
                occluded = True
                matches = head
                for m in matches:
                    m.pop("camera_center", None)
        except Exception:
            for m in matches:
                m.pop("camera_center", None)

    # A defect (outline given): prefer photos where it is reasonably large and
    # wholly in frame — not a few pixels at the edge of a distant shot.
    outline_sfm = [sfm.mesh_to_sfm(p[0], p[1], p[2]) for p in body.outline] if body.outline else None
    if outline_sfm and matches and not occluded:
        matches = sfm.rank_by_outline(cameras, matches[:80], outline_sfm)
    for m in matches:
        m.pop("score", None)
    photos = matches[:24]

    # Project the polygon boundary into each returned photo (mesh → SfM frame),
    # so the defect is outlined on the photos just like on the 3D model.
    if outline_sfm and photos:
        outlines = sfm.project_outline(cameras, [m["filename"] for m in photos], outline_sfm)
        for m in photos:
            m["outline"] = outlines.get(m["filename"])

    return {"photos": photos, "total": len(matches), "occluded": occluded}


@router.post("/project-point")
async def project_point(inspection_id: str, body: ProjectPointIn):
    """
    Given a 3D point in original model coordinates, return source photos that
    see the point, with its normalized (u, v) location in each image.
    Requires cameras.sfm saved during Meshroom reconstruction.
    """
    models_dir = storage.get_models_dir(inspection_id)
    sfm_files = list(models_dir.glob("**/cameras.sfm"))
    if not sfm_files:
        raise HTTPException(
            404,
            "Pozice kamer nejsou k dispozici — spusťte rekonstrukci znovu (Meshroom).",
        )
    # Offload the raycasting to a worker thread so the event loop stays free.
    return await asyncio.to_thread(_project_point_sync, sfm_files[0], body)


# ─── Thumbnail generation ─────────────────────────────────────────────────────

def _clear_thumbnail_cache(inspection_id: str):
    thumb_dir = storage.get_inspection_dir(inspection_id) / "thumbnails"
    if thumb_dir.exists():
        for f in thumb_dir.iterdir():
            f.unlink(missing_ok=True)


@router.get("/photos/{filename}/thumbnail")
async def photo_thumbnail(inspection_id: str, filename: str, size: int = 400):
    """
    Return a JPEG thumbnail of a source photo, generated on first request and cached.
    EXIF rotation is applied automatically so phone photos appear upright.
    """
    from PIL import Image, ImageOps

    photos_dir = storage.get_photos_dir(inspection_id)
    src = (photos_dir / filename).resolve()

    # Prevent path traversal
    try:
        src.relative_to(photos_dir.resolve())
    except ValueError:
        raise HTTPException(403)

    if not src.exists() or src.suffix.lower() not in storage.ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(404, "Snímek nenalezen")

    # Clamp size to sane range
    size = max(100, min(size, 1200))

    thumb_dir = storage.get_inspection_dir(inspection_id) / "thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    thumb_path = thumb_dir / f"{size}_{src.stem}.jpg"

    if not thumb_path.exists():
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)   # auto-rotate from EXIF orientation
            img = img.convert("RGB")
            img.thumbnail((size, size), Image.LANCZOS)
            img.save(thumb_path, format="JPEG", quality=82, optimize=True)

    return FileResponse(
        thumb_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
