from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Structure, Inspection, StructureMaterial, Measurement
from app.schemas import StructureCreate, StructureUpdate, StructureOut, StructureMaterialIn
from app.services.storage import get_structure_dir

router = APIRouter(prefix="/structures", tags=["structures"])


async def _enrich(s: Structure, db: AsyncSession) -> StructureOut:
    r2 = await db.execute(
        select(func.count(Inspection.id), func.max(Inspection.inspection_date))
        .where(Inspection.structure_id == s.id)
    )
    count, last_date = r2.one()
    item = StructureOut.model_validate(s)
    item.inspection_count = count or 0
    item.last_inspection_date = last_date
    return item


@router.get("", response_model=list[StructureOut])
async def list_structures(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Structure)
        .options(selectinload(Structure.materials))
        .order_by(Structure.created_at.desc())
    )
    return [await _enrich(s, db) for s in result.scalars().all()]


@router.post("", response_model=StructureOut, status_code=201)
async def create_structure(data: StructureCreate, db: AsyncSession = Depends(get_db)):
    payload = data.model_dump(exclude={"materials"})
    s = Structure(**payload)
    db.add(s)
    await db.flush()  # get s.id

    for i, mat in enumerate(data.materials):
        db.add(StructureMaterial(
            structure_id=s.id,
            material_code=mat.material_code,
            material_name=mat.material_name,
            color=mat.color,
            percentage=mat.percentage,
            sort_order=i,
        ))

    await db.commit()
    await db.refresh(s)
    # Reload with eager materials
    result = await db.execute(
        select(Structure).where(Structure.id == s.id).options(selectinload(Structure.materials))
    )
    s = result.scalar_one()
    return await _enrich(s, db)


@router.get("/{structure_id}", response_model=StructureOut)
async def get_structure(structure_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Structure).where(Structure.id == structure_id).options(selectinload(Structure.materials))
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Konstrukce nenalezena")
    return await _enrich(s, db)


@router.patch("/{structure_id}", response_model=StructureOut)
async def update_structure(structure_id: str, data: StructureUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Structure).where(Structure.id == structure_id).options(selectinload(Structure.materials))
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Konstrukce nenalezena")

    payload = data.model_dump(exclude_none=True, exclude={"materials"})
    for k, v in payload.items():
        setattr(s, k, v)

    # Replace materials if provided
    if data.materials is not None:
        for existing_mat in list(s.materials):
            await db.delete(existing_mat)
        await db.flush()
        for i, mat in enumerate(data.materials):
            db.add(StructureMaterial(
                structure_id=s.id,
                material_code=mat.material_code,
                material_name=mat.material_name,
                color=mat.color,
                percentage=mat.percentage,
                sort_order=i,
            ))

    await db.commit()

    result = await db.execute(
        select(Structure).where(Structure.id == structure_id).options(selectinload(Structure.materials))
    )
    s = result.scalar_one()
    return await _enrich(s, db)


@router.delete("/{structure_id}", status_code=204)
async def delete_structure(structure_id: str, db: AsyncSession = Depends(get_db)):
    s = await db.get(Structure, structure_id)
    if not s:
        raise HTTPException(404, "Konstrukce nenalezena")
    await db.delete(s)
    await db.commit()


@router.post("/{structure_id}/thumbnail")
async def upload_thumbnail(
    structure_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(Structure, structure_id)
    if not s:
        raise HTTPException(404, "Konstrukce nenalezena")

    struct_dir = get_structure_dir(structure_id)
    ext = file.filename[file.filename.rfind("."):]
    dest = struct_dir / f"thumbnail{ext}"
    content = await file.read()
    dest.write_bytes(content)

    from app.services.storage import get_relative_path
    s.thumbnail_path = get_relative_path(dest)
    await db.commit()
    return {"thumbnail_path": s.thumbnail_path}


@router.get("/{structure_id}/measurement-trends")
async def measurement_trends(structure_id: str, db: AsyncSession = Depends(get_db)):
    """Group measurements by name across the structure's inspections and report the
    time series + trend, so a repeatedly-measured quantity (e.g. crack width) can
    be tracked and flagged when it worsens (grows) over time."""
    s = await db.get(Structure, structure_id)
    if not s:
        raise HTTPException(404, "Konstrukce nenalezena")

    r = await db.execute(
        select(Measurement, Inspection.name, Inspection.inspection_date)
        .join(Inspection, Measurement.inspection_id == Inspection.id)
        .where(Inspection.structure_id == structure_id)
        .order_by(Inspection.inspection_date)
    )
    rows = r.all()

    groups: dict[str, dict] = {}
    for m, insp_name, insp_date in rows:
        if m.value is None:
            continue
        key = (m.name or "Bez názvu").strip().lower()
        g = groups.setdefault(key, {
            "name": m.name or "Bez názvu",
            "unit": m.unit or "",
            "points": [],
        })
        g["points"].append({
            "value": m.value,
            "unit": m.unit or "",
            "inspection_id": m.inspection_id,
            "inspection_name": insp_name,
            "date": str(insp_date) if insp_date else None,
        })

    series = []
    for g in groups.values():
        pts = g["points"]
        if len(pts) < 2:
            continue                         # a trend needs at least two points
        first, last = pts[0]["value"], pts[-1]["value"]
        change = last - first
        change_pct = (change / abs(first) * 100) if first else 0.0
        trend = "up" if change > 1e-9 else "down" if change < -1e-9 else "flat"
        series.append({**g, "first": first, "last": last,
                       "change": change, "change_pct": change_pct, "trend": trend})

    # worsening (growing) series first
    series.sort(key=lambda x: -x["change_pct"])
    return {"series": series, "total": len(series)}
