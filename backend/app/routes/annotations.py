from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Inspection, Annotation
from app.schemas import AnnotationCreate, AnnotationUpdate, AnnotationOut
from app.services.storage import get_inspection_dir, get_relative_path

router = APIRouter(prefix="/inspections/{inspection_id}/annotations", tags=["annotations"])


async def _check_inspection(inspection_id: str, db: AsyncSession) -> Inspection:
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    return insp


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(inspection_id: str, db: AsyncSession = Depends(get_db)):
    await _check_inspection(inspection_id, db)
    result = await db.execute(
        select(Annotation).where(Annotation.inspection_id == inspection_id)
        .order_by(Annotation.created_at)
    )
    return [AnnotationOut.model_validate(a) for a in result.scalars().all()]


@router.post("", response_model=AnnotationOut, status_code=201)
async def create_annotation(
    inspection_id: str, data: AnnotationCreate, db: AsyncSession = Depends(get_db)
):
    await _check_inspection(inspection_id, db)
    ann = Annotation(inspection_id=inspection_id, **data.model_dump())
    db.add(ann)
    await db.commit()
    await db.refresh(ann)
    return AnnotationOut.model_validate(ann)


@router.get("/{annotation_id}", response_model=AnnotationOut)
async def get_annotation(inspection_id: str, annotation_id: str, db: AsyncSession = Depends(get_db)):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    return AnnotationOut.model_validate(ann)


@router.patch("/{annotation_id}", response_model=AnnotationOut)
async def update_annotation(
    inspection_id: str, annotation_id: str, data: AnnotationUpdate, db: AsyncSession = Depends(get_db)
):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    # exclude_unset (not exclude_none): only touch fields the client actually
    # sent, but allow them to be set to null (e.g. clearing a length/area).
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(ann, k, v)
    await db.commit()
    await db.refresh(ann)
    return AnnotationOut.model_validate(ann)


@router.delete("/{annotation_id}", status_code=204)
async def delete_annotation(inspection_id: str, annotation_id: str, db: AsyncSession = Depends(get_db)):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    await db.delete(ann)
    await db.commit()


# ─── Detail (measurement) photos per annotation ───────────────────────────────
# Commercial workflow: overview photos build the model; close-up photos of each
# defect carry the measurable detail (with a ruler/gauge in frame as reference).

def _detail_dir(inspection_id: str, annotation_id: str):
    return get_inspection_dir(inspection_id) / "annotation_images" / annotation_id


@router.post("/{annotation_id}/photos")
async def upload_detail_photos(
    inspection_id: str,
    annotation_id: str,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")

    from pathlib import Path as _P
    img_dir = _detail_dir(inspection_id, annotation_id)
    img_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = _P(f.filename).name
        if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        dest = img_dir / name
        dest.write_bytes(await f.read())
        saved.append(name)
    return {"saved": saved, "total": len(list(img_dir.iterdir()))}


@router.get("/{annotation_id}/photos")
async def list_detail_photos(inspection_id: str, annotation_id: str, db: AsyncSession = Depends(get_db)):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    img_dir = _detail_dir(inspection_id, annotation_id)
    photos = []
    if img_dir.exists():
        for f in sorted(img_dir.iterdir()):
            if f.is_file():
                photos.append({
                    "filename": f.name,
                    "url": f"/uploads/{get_relative_path(f)}",
                    "size": f.stat().st_size,
                })
    return {"photos": photos, "total": len(photos)}


@router.post("/{annotation_id}/attach-source-photo")
async def attach_source_photo(
    inspection_id: str,
    annotation_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Attach one of the inspection's SOURCE photos (from 'snímky s tímto místem')
    to this defect as a detail photo — copies it into the annotation's image dir."""
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    from pathlib import Path as _P
    filename = _P(str(body.get("filename", ""))).name
    if not filename:
        raise HTTPException(400, "Chybí filename")
    from app.services.storage import get_photos_dir
    src = get_photos_dir(inspection_id) / filename
    if not src.is_file():
        raise HTTPException(404, "Zdrojový snímek nenalezen")
    img_dir = _detail_dir(inspection_id, annotation_id)
    img_dir.mkdir(parents=True, exist_ok=True)
    from app.services.raw_images import display_file, is_raw
    import asyncio
    if is_raw(src):
        filename += '.jpg'
        src = await asyncio.to_thread(display_file, src)
    dest = img_dir / filename
    await asyncio.to_thread(dest.write_bytes, src.read_bytes())
    return {"filename": filename, "total": len(list(img_dir.iterdir()))}


@router.post("/{annotation_id}/analyze-photo")
async def analyze_detail_photo(
    inspection_id: str,
    annotation_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Run the heuristic CV crack detector on one of the annotation's detail
    photos and return suggested severity/type + a highlighted overlay."""
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    from pathlib import Path as _P
    filename = _P(str(body.get("filename", ""))).name
    if not filename:
        raise HTTPException(400, "Chybí filename")
    target = _detail_dir(inspection_id, annotation_id) / filename
    if not target.is_file():
        raise HTTPException(404, "Snímek nenalezen")
    from app.services import defect_detect
    try:
        return defect_detect.analyze_image(target)
    except Exception as e:
        raise HTTPException(500, f"Analýza selhala: {e}")


@router.delete("/{annotation_id}/photos/{filename}", status_code=204)
async def delete_detail_photo(
    inspection_id: str, annotation_id: str, filename: str, db: AsyncSession = Depends(get_db)
):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")
    from pathlib import Path as _P
    target = (_detail_dir(inspection_id, annotation_id) / _P(filename).name)
    if target.exists():
        target.unlink()


@router.post("/{annotation_id}/image")
async def upload_annotation_image(
    inspection_id: str,
    annotation_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    ann = await db.get(Annotation, annotation_id)
    if not ann or ann.inspection_id != inspection_id:
        raise HTTPException(404, "Anotace nenalezena")

    img_dir = get_inspection_dir(inspection_id) / "annotation_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    ext = file.filename[file.filename.rfind("."):]
    dest = img_dir / f"{annotation_id}{ext}"
    content = await file.read()
    dest.write_bytes(content)

    ann.image_path = get_relative_path(dest)
    await db.commit()
    return {"image_path": ann.image_path}
