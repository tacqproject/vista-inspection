"""
AI defect-segmentation endpoints: run the external model over all photos of an
inspection and read back per-image class coverage + overlays.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Inspection
from app.services import defect_segmentation as seg

router = APIRouter(prefix="/inspections/{inspection_id}/ai-segment", tags=["ai"])


async def _get_inspection_or_404(inspection_id: str, db: AsyncSession) -> Inspection:
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    return insp


@router.get("")
async def get_segmentation(inspection_id: str, db: AsyncSession = Depends(get_db)):
    await _get_inspection_or_404(inspection_id, db)
    result = seg.load_results(inspection_id)
    result["engine"] = seg.check()
    result["class_labels"] = seg.CLASS_LABELS
    result["damage_classes"] = seg.DAMAGE_CLASSES
    return result


@router.post("", status_code=202)
async def start_segmentation(
    inspection_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    await _get_inspection_or_404(inspection_id, db)
    info = seg.check()
    if not info.get("available"):
        raise HTTPException(400, info.get("hint") or "AI model není dostupný")
    background_tasks.add_task(_run, inspection_id)
    return {"status": "processing"}


async def _run(inspection_id: str):
    from app.ws_manager import ws_manager

    async def on_progress(pct: int, message: str):
        await ws_manager.broadcast(
            inspection_id, {"type": "ai_progress", "progress": pct, "message": message}
        )

    try:
        await seg.run_segmentation(inspection_id, on_progress)
        await ws_manager.broadcast(inspection_id, {"type": "ai_completed"})
    except Exception as e:
        # run_segmentation already recorded status=failed; just notify the client
        await ws_manager.broadcast(inspection_id, {"type": "ai_error", "message": str(e)})


# ─── Automatic AI defect inspection (photos → defects in the 3D model) ───────

defects_router = APIRouter(prefix="/inspections/{inspection_id}/ai-defects", tags=["ai"])
_DEFECT_TASKS: dict = {}


@defects_router.get("")
async def defects_status(inspection_id: str, db: AsyncSession = Depends(get_db)):
    from app.services import defects_auto as D
    from app.services.storage import get_relative_path
    await _get_inspection_or_404(inspection_id, db)
    st = D.status(inspection_id)
    d = D.out_dir(inspection_id)
    st["overlays"] = []
    f = d / "detections.json"
    if f.exists():
        import json as _json
        try:
            for im in _json.loads(f.read_text())["images"]:
                if im["detections"]:
                    st["overlays"].append({"photo": im["filename"], "n": len(im["detections"]),
                                           "url": f"/uploads/{get_relative_path(d / im['overlay'])}"})
        except Exception:  # noqa: BLE001
            pass
    return st


@defects_router.post("", status_code=202)
async def defects_run(inspection_id: str, redetect: bool = False, db: AsyncSession = Depends(get_db)):
    """Detect defects on all photos (unless already done) and place them in
    the 3D model as AI annotations (replaces untouched earlier AI defects)."""
    import asyncio
    from app.services import defects_auto as D
    await _get_inspection_or_404(inspection_id, db)
    if D.status(inspection_id).get("status") == "detecting":
        raise HTTPException(409, "AI detekce už běží")

    async def job():
        try:
            if redetect:
                await asyncio.to_thread(D.detect, inspection_id)
            else:
                await asyncio.to_thread(D.detect_if_needed, inspection_id)
            await D.annotate(inspection_id)
        except Exception as e:  # noqa: BLE001
            D._set_status(inspection_id, status="failed", error=str(e)[:300])

    D._set_status(inspection_id, status="detecting", progress=0)
    _DEFECT_TASKS[inspection_id] = asyncio.create_task(job())
    return {"status": "processing"}
