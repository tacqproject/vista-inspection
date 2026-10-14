import asyncio

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select

from app.database import get_db
from app.models import Structure, Inspection, Annotation, Measurement, PhotoSet
from app.schemas import ReportRequest, ComparisonReportRequest
from app.services import report_generator

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("/inspection/{inspection_id}")
async def generate_inspection_report(
    inspection_id: str,
    req: ReportRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a PDF report for a single inspection."""
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")

    struct = await db.get(Structure, insp.structure_id)

    r_ann = await db.execute(
        select(Annotation).where(Annotation.inspection_id == inspection_id).order_by(Annotation.created_at)
    )
    annotations = r_ann.scalars().all()

    if req.severity_filter:
        annotations = [a for a in annotations if a.severity in req.severity_filter]

    r_meas = await db.execute(
        select(Measurement).where(Measurement.inspection_id == inspection_id).order_by(Measurement.created_at)
    )
    measurements = r_meas.scalars().all()

    r_ps = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == inspection_id))
    photo_set = r_ps.scalar_one_or_none()

    pdf_bytes = await asyncio.to_thread(
        report_generator.generate_inspection_report,
        structure=_to_dict(struct),
        inspection=_to_dict(insp),
        annotations=[_to_dict(a) for a in annotations],
        measurements=[_to_dict(m) for m in measurements],
        photo_set=_to_dict(photo_set) if photo_set else None,
        options=req.model_dump(),
    )

    safe_name = f"inspekce_{inspection_id[:8]}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.post("/structure/{structure_id}")
async def generate_structure_report(
    structure_id: str,
    req: ReportRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a comprehensive PDF report for an entire structure."""
    struct = await db.get(Structure, structure_id)
    if not struct:
        raise HTTPException(404, "Konstrukce nenalezena")

    r_insp = await db.execute(
        select(Inspection).where(Inspection.structure_id == structure_id).order_by(Inspection.inspection_date.desc())
    )
    inspections = r_insp.scalars().all()

    all_annotations = []
    all_measurements = []
    for insp in inspections:
        r_ann = await db.execute(
            select(Annotation).where(Annotation.inspection_id == insp.id)
        )
        anns = r_ann.scalars().all()
        if req.severity_filter:
            anns = [a for a in anns if a.severity in req.severity_filter]
        all_annotations.extend([{**_to_dict(a), "inspection_id": insp.id} for a in anns])

        r_meas = await db.execute(
            select(Measurement).where(Measurement.inspection_id == insp.id)
        )
        all_measurements.extend([_to_dict(m) for m in r_meas.scalars().all()])

    pdf_bytes = await asyncio.to_thread(
        report_generator.generate_structure_report,
        structure=_to_dict(struct),
        inspections=[_to_dict(i) for i in inspections],
        all_annotations=all_annotations,
        all_measurements=all_measurements,
        options=req.model_dump(),
    )

    safe_name = f"konstrukce_{struct.name[:20].replace(' ','_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.post("/ai-segmentation/{inspection_id}")
async def generate_ai_segmentation_report(inspection_id: str, db: AsyncSession = Depends(get_db)):
    """PDF overview of the AI defect segmentation for an inspection."""
    from app.services import defect_segmentation as seg
    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    results = seg.load_results(inspection_id)
    if results.get("status") != "completed" or not results.get("images"):
        raise HTTPException(400, "Pro tuto inspekci není hotová AI analýza")
    struct = await db.get(Structure, insp.structure_id)
    pdf_bytes = await asyncio.to_thread(
        report_generator.generate_ai_segmentation_report,
        structure=_to_dict(struct),
        inspection=_to_dict(insp),
        results=results,
        class_labels=seg.CLASS_LABELS,
        damage_classes=seg.DAMAGE_CLASSES,
    )
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="ai_analyza_{inspection_id[:8]}.pdf"'},
    )


@router.get("/shared/{token}")
async def shared_inspection_report(token: str, db: AsyncSession = Depends(get_db)):
    """Public, read-only protocol PDF for a shared inspection — no login needed."""
    r = await db.execute(select(Inspection).where(Inspection.share_token == token))
    insp = r.scalar_one_or_none()
    if not insp or not insp.share_token:
        raise HTTPException(404, "Sdílený odkaz neexistuje nebo byl zrušen")

    struct = await db.get(Structure, insp.structure_id)
    r_ann = await db.execute(
        select(Annotation).where(Annotation.inspection_id == insp.id).order_by(Annotation.created_at)
    )
    annotations = r_ann.scalars().all()
    r_meas = await db.execute(
        select(Measurement).where(Measurement.inspection_id == insp.id).order_by(Measurement.created_at)
    )
    measurements = r_meas.scalars().all()
    r_ps = await db.execute(select(PhotoSet).where(PhotoSet.inspection_id == insp.id))
    photo_set = r_ps.scalar_one_or_none()

    pdf_bytes = await asyncio.to_thread(
        report_generator.generate_inspection_report,
        structure=_to_dict(struct),
        inspection=_to_dict(insp),
        annotations=[_to_dict(a) for a in annotations],
        measurements=[_to_dict(m) for m in measurements],
        photo_set=_to_dict(photo_set) if photo_set else None,
        options={},
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="protokol_{insp.id[:8]}.pdf"'},
    )


@router.post("/compare")
async def generate_comparison_report(
    req: ComparisonReportRequest,
    db: AsyncSession = Depends(get_db),
):
    """Side-by-side degradation report between two inspections of one structure."""
    insp_a = await db.get(Inspection, req.inspection_a)
    insp_b = await db.get(Inspection, req.inspection_b)
    if not insp_a or not insp_b:
        raise HTTPException(404, "Inspekce nenalezena")
    if insp_a.structure_id != insp_b.structure_id:
        raise HTTPException(400, "Inspekce musí patřit stejné konstrukci")

    struct = await db.get(Structure, insp_a.structure_id)

    # A = the older of the two so deltas read as degradation over time.
    if (insp_a.inspection_date or "") > (insp_b.inspection_date or ""):
        insp_a, insp_b = insp_b, insp_a
        req.snapshot_a, req.snapshot_b = req.snapshot_b, req.snapshot_a

    async def _anns(insp_id):
        r = await db.execute(
            select(Annotation).where(Annotation.inspection_id == insp_id).order_by(Annotation.created_at)
        )
        return [_to_dict(a) for a in r.scalars().all()]

    pdf_bytes = await asyncio.to_thread(
        report_generator.generate_comparison_report,
        structure=_to_dict(struct),
        inspection_a=_to_dict(insp_a),
        inspection_b=_to_dict(insp_b),
        annotations_a=await _anns(insp_a.id),
        annotations_b=await _anns(insp_b.id),
        options=req.model_dump(),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="porovnani.pdf"'},
    )


def _to_dict(obj) -> dict:
    if obj is None:
        return {}
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ─── BMS inspection protocol (.docx / .pdf, ŘSD layout) ───────────────────────

class BmsRequest(BaseModel):
    format: str = "docx"                      # docx | pdf
    severity_filter: Optional[list[str]] = None
    # face-on 3D close-ups per defect (annotation id → data-URL), optional
    defect_snapshots: Optional[dict] = None


@router.get("/bms/catalogue")
async def bms_catalogue():
    """Bridge parts and measure urgencies offered by the defect form."""
    from app.services import bms_protocol as B
    return {
        "parts": [{"code": c, "name": n, "label": f"{c} {n}"} for c, n in B.BRIDGE_PARTS],
        "groups": B.PART_GROUPS,
        "urgencies": B.URGENCY_ORDER,
        "kinds": {k: v[1] for k, v in B.KINDS.items()},
    }


@router.post("/inspection/{inspection_id}/bms")
async def bms_protocol(inspection_id: str, req: BmsRequest, db: AsyncSession = Depends(get_db)):
    """Inspection protocol in the ŘSD BMS layout, as .docx or .pdf (the PDF is
    rendered from the same .docx, so both look identical)."""
    from app.services import bms_protocol as B

    insp = await db.get(Inspection, inspection_id)
    if not insp:
        raise HTTPException(404, "Inspekce nenalezena")
    struct = await db.get(Structure, insp.structure_id)
    anns = (await db.execute(
        select(Annotation).where(
            Annotation.inspection_id == inspection_id,
            # NULL-safe: `status != 'dismissed'` alone would drop rows with NULL status
            or_(Annotation.status.is_(None), Annotation.status != "dismissed"),
        )
        .order_by(Annotation.created_at)
    )).scalars().all()
    if req.severity_filter:
        anns = [a for a in anns if a.severity in req.severity_filter]

    s, i = _to_dict(struct), _to_dict(insp)
    docx = await asyncio.to_thread(
        B.build_protocol, s, i, [_to_dict(a) for a in anns], req.defect_snapshots or {},
    )
    if req.format == "pdf":
        try:
            data = await asyncio.to_thread(B.docx_to_pdf, docx)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"Převod do PDF selhal: {e}")
        media, ext = "application/pdf", "pdf"
    else:
        data = docx
        media, ext = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    return Response(content=data, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{B.protocol_filename(s, i, ext)}"'})


# ─── Appendix photos (general views: "Pohled ve směru staničení", …) ──────────

@router.get("/inspection/{inspection_id}/appendix-photos")
async def list_appendix_photos(inspection_id: str):
    from app.services import bms_protocol as B
    from app.services.storage import get_relative_path
    d = B.appendix_dir(inspection_id)
    files = sorted((f for f in d.iterdir() if f.is_file()), key=lambda f: B._natural(f.name)) if d.is_dir() else []
    return {"photos": [{"filename": f.name, "url": f"/uploads/{get_relative_path(f)}"} for f in files]}


@router.post("/inspection/{inspection_id}/appendix-photos")
async def upload_appendix_photos(inspection_id: str, files: list[UploadFile] = File(...),
                                 db: AsyncSession = Depends(get_db)):
    from pathlib import Path as _P
    from app.services import bms_protocol as B
    if not await db.get(Inspection, inspection_id):
        raise HTTPException(404, "Inspekce nenalezena")
    d = B.appendix_dir(inspection_id)
    d.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = _P(f.filename or "").name
        if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        content = await f.read()
        await asyncio.to_thread((d / name).write_bytes, content)
        saved.append(name)
    return {"saved": saved}


@router.delete("/inspection/{inspection_id}/appendix-photos/{filename}", status_code=204)
async def delete_appendix_photo(inspection_id: str, filename: str):
    from pathlib import Path as _P
    from app.services import bms_protocol as B
    f = B.appendix_dir(inspection_id) / _P(filename).name
    if f.is_file():
        f.unlink()
