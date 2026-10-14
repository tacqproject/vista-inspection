"""
Config API – správa typů staveb a materiálů.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models import StructureTypeConfig, MaterialConfig, Structure
from app.schemas import (
    StructureTypeConfigCreate, StructureTypeConfigUpdate, StructureTypeConfigOut,
    MaterialConfigCreate, MaterialConfigUpdate, MaterialConfigOut,
)

router = APIRouter(prefix="/config", tags=["config"])


# ─── Photogrammetry engine status ─────────────────────────────────────────────

@router.get("/engines")
async def get_engine_status():
    """Return availability of photogrammetry engines and current configuration."""
    from app.services.photogrammetry import check_engine_availability
    return await check_engine_availability()


# ─── Meshroom pipeline templates (.mg) ────────────────────────────────────────

from fastapi import UploadFile, File
from app.config import settings as app_settings
import json as _json


def _pipelines_dir():
    d = app_settings.upload_path / "pipelines"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/pipelines")
async def list_pipelines():
    """Imported Meshroom .mg node-graph templates usable for reconstruction."""
    files = [
        {"filename": f.name, "size": f.stat().st_size}
        for f in sorted(_pipelines_dir().glob("*.mg"))
    ]
    return {"pipelines": files}


@router.post("/pipelines", status_code=201)
async def upload_pipeline(file: UploadFile = File(...)):
    """Import a pipeline exported from Meshroom GUI (File → Save As… .mg)."""
    from pathlib import Path as _P
    name = _P(file.filename).name
    if not name.endswith(".mg"):
        raise HTTPException(400, "Očekávám soubor .mg (projekt/šablona z Meshroomu)")
    content = await file.read()
    try:
        graph = _json.loads(content)
        if "graph" not in graph:
            raise ValueError("chybí klíč 'graph'")
    except Exception as e:
        raise HTTPException(400, f"Neplatný .mg soubor: {e}")
    (_pipelines_dir() / name).write_bytes(content)
    return {"filename": name, "nodes": len(graph.get("graph", {}))}


@router.delete("/pipelines/{filename}", status_code=204)
async def delete_pipeline(filename: str):
    from pathlib import Path as _P
    target = _pipelines_dir() / _P(filename).name
    if target.exists():
        target.unlink()


# ─── Pipeline introspection (for the docs page) ──────────────────────────────

# Meshroom internal stages shown as sub-nodes of the meshroom engine
_MESHROOM_STAGE_DOCS = [
    ("CameraInit", "Načtení EXIF metadat, seskupení snímků podle kamer a inicializace vnitřních parametrů (ohnisko, senzor)."),
    ("FeatureExtraction", "Extrakce SIFT/DSP-SIFT příznaků z každého snímku — výrazné body, které lze najít napříč fotkami."),
    ("ImageMatching", "Předvýběr dvojic snímků, které se pravděpodobně překrývají (slovníkový strom)."),
    ("FeatureMatching", "Párování příznaků mezi vybranými dvojicemi + geometrické ověření."),
    ("StructureFromMotion", "Inkrementální rekonstrukce: pozice kamer a řídké mračno bodů (bundle adjustment)."),
    ("PrepareDenseScene", "Undistorce snímků pro hustou rekonstrukci."),
    ("DepthMap", "Hloubková mapa pro každý snímek (GPU, CUDA) — nejnáročnější krok."),
    ("DepthMapFilter", "Filtrace hloubkových map — konzistence mezi pohledy."),
    ("Meshing", "Fúze hloubkových map do husté 3D sítě."),
    ("MeshFiltering", "Vyhlazení a odstranění největších artefaktů surové sítě."),
    ("Texturing", "UV unwrap a projekce fotek na síť → texturovaný model. (Při zapnutém PyMeshLab čištění se tento krok pouští až na vyčištěnou síť.)"),
]


@router.get("/pipeline")
async def get_pipeline_docs():
    """
    Live introspection of the reconstruction pipeline: registered engines and
    post-processing steps with their order and documentation. Drives the
    Meshroom-style node graph on the /docs page.
    """
    from app.services.photogrammetry.base import ENGINES, POSTPROCESS_STEPS
    import inspect
    import sys

    engines = []
    for name, spec in ENGINES.items():
        module = sys.modules.get(spec.run.__module__)
        engines.append({
            "name": name,
            "doc": inspect.getdoc(module) or inspect.getdoc(spec.run) or "",
            "stages": [
                {"name": s, "doc": d} for s, d in _MESHROOM_STAGE_DOCS
            ] if name == "meshroom" else [],
        })

    steps = []
    for s in POSTPROCESS_STEPS:
        module = sys.modules.get(s.run.__module__)
        steps.append({
            "name": s.name,
            "order": s.order,
            "doc": inspect.getdoc(module) or inspect.getdoc(s.run) or "",
        })

    return {"engines": engines, "postprocess": steps}


# ─── Structure types ──────────────────────────────────────────────────────────

@router.get("/structure-types", response_model=list[StructureTypeConfigOut])
async def list_structure_types(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(StructureTypeConfig).order_by(StructureTypeConfig.sort_order, StructureTypeConfig.name)
    )
    return result.scalars().all()


@router.post("/structure-types", response_model=StructureTypeConfigOut, status_code=201)
async def create_structure_type(data: StructureTypeConfigCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(StructureTypeConfig).where(StructureTypeConfig.code == data.code))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Typ s kódem '{data.code}' již existuje")
    obj = StructureTypeConfig(**data.model_dump())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.patch("/structure-types/{type_id}", response_model=StructureTypeConfigOut)
async def update_structure_type(
    type_id: str, data: StructureTypeConfigUpdate, db: AsyncSession = Depends(get_db)
):
    obj = await db.get(StructureTypeConfig, type_id)
    if not obj:
        raise HTTPException(404, "Typ nenalezen")
    payload = data.model_dump(exclude_none=True)
    if "code" in payload and payload["code"] != obj.code:
        existing = await db.execute(select(StructureTypeConfig).where(StructureTypeConfig.code == payload["code"]))
        if existing.scalar_one_or_none():
            raise HTTPException(400, f"Kód '{payload['code']}' již existuje")
    for k, v in payload.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/structure-types/{type_id}", status_code=204)
async def delete_structure_type(type_id: str, db: AsyncSession = Depends(get_db)):
    obj = await db.get(StructureTypeConfig, type_id)
    if not obj:
        raise HTTPException(404, "Typ nenalezen")
    # Warn if in use (don't block – user might want to keep old data)
    await db.delete(obj)
    await db.commit()


# ─── Materials ────────────────────────────────────────────────────────────────

@router.get("/materials", response_model=list[MaterialConfigOut])
async def list_materials(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MaterialConfig).order_by(MaterialConfig.sort_order, MaterialConfig.name)
    )
    return result.scalars().all()


@router.post("/materials", response_model=MaterialConfigOut, status_code=201)
async def create_material(data: MaterialConfigCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(MaterialConfig).where(MaterialConfig.code == data.code))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Materiál s kódem '{data.code}' již existuje")
    obj = MaterialConfig(**data.model_dump())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.patch("/materials/{material_id}", response_model=MaterialConfigOut)
async def update_material(
    material_id: str, data: MaterialConfigUpdate, db: AsyncSession = Depends(get_db)
):
    obj = await db.get(MaterialConfig, material_id)
    if not obj:
        raise HTTPException(404, "Materiál nenalezen")
    payload = data.model_dump(exclude_none=True)
    if "code" in payload and payload["code"] != obj.code:
        existing = await db.execute(select(MaterialConfig).where(MaterialConfig.code == payload["code"]))
        if existing.scalar_one_or_none():
            raise HTTPException(400, f"Kód '{payload['code']}' již existuje")
    for k, v in payload.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/materials/{material_id}", status_code=204)
async def delete_material(material_id: str, db: AsyncSession = Depends(get_db)):
    obj = await db.get(MaterialConfig, material_id)
    if not obj:
        raise HTTPException(404, "Materiál nenalezen")
    await db.delete(obj)
    await db.commit()
