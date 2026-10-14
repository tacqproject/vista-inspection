import math
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Inspection, Measurement
from app.schemas import MeasurementCreate, MeasurementUpdate, MeasurementOut

router = APIRouter(prefix="/inspections/{inspection_id}/measurements", tags=["measurements"])


def _calc_distance(pts: list) -> float | None:
    if not pts or len(pts) < 2:
        return None
    p1, p2 = pts[0], pts[1]
    dx = p2["x"] - p1["x"]
    dy = p2["y"] - p1["y"]
    dz = p2["z"] - p1["z"]
    return round(math.sqrt(dx**2 + dy**2 + dz**2), 4)


@router.get("", response_model=list[MeasurementOut])
async def list_measurements(inspection_id: str, db: AsyncSession = Depends(get_db)):
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    result = await db.execute(
        select(Measurement).where(Measurement.inspection_id == inspection_id).order_by(Measurement.created_at)
    )
    return [MeasurementOut.model_validate(m) for m in result.scalars().all()]


@router.post("", response_model=MeasurementOut, status_code=201)
async def create_measurement(
    inspection_id: str, data: MeasurementCreate, db: AsyncSession = Depends(get_db)
):
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")

    payload = data.model_dump()

    # Auto-calculate distance if points provided and no value set
    if payload.get("measurement_type") == "distance" and payload.get("points_json") and not payload.get("value"):
        payload["value"] = _calc_distance(payload["points_json"])

    m = Measurement(inspection_id=inspection_id, **payload)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return MeasurementOut.model_validate(m)


@router.get("/{measurement_id}", response_model=MeasurementOut)
async def get_measurement(inspection_id: str, measurement_id: str, db: AsyncSession = Depends(get_db)):
    m = await db.get(Measurement, measurement_id)
    if not m or m.inspection_id != inspection_id:
        raise HTTPException(404, "Měření nenalezeno")
    return MeasurementOut.model_validate(m)


@router.patch("/{measurement_id}", response_model=MeasurementOut)
async def update_measurement(
    inspection_id: str, measurement_id: str, data: MeasurementUpdate, db: AsyncSession = Depends(get_db)
):
    m = await db.get(Measurement, measurement_id)
    if not m or m.inspection_id != inspection_id:
        raise HTTPException(404, "Měření nenalezeno")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(m, k, v)
    await db.commit()
    await db.refresh(m)
    return MeasurementOut.model_validate(m)


@router.delete("/{measurement_id}", status_code=204)
async def delete_measurement(inspection_id: str, measurement_id: str, db: AsyncSession = Depends(get_db)):
    m = await db.get(Measurement, measurement_id)
    if not m or m.inspection_id != inspection_id:
        raise HTTPException(404, "Měření nenalezeno")
    await db.delete(m)
    await db.commit()
