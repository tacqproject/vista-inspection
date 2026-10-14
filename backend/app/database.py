from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        # create_all never ALTERs existing tables — add newer columns by hand
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn):
    """Idempotently add columns introduced after a table already existed."""
    from sqlalchemy import text
    wanted = {
        "structures": [("reference_inspection_id", "VARCHAR")] + [
            (c, "VARCHAR") for c in (
                "evidence_number", "district", "road_number", "chainage_km",
                "chainage_direction", "load_vn", "load_vr", "load_ve",
                "max_axle_load", "administrator")
        ],
        "inspections": [
            ("canonical", "BOOLEAN DEFAULT 0"),
            ("approval_status", "VARCHAR DEFAULT 'draft'"),
            ("approved_by", "VARCHAR"),
            ("approved_at", "VARCHAR"),
            ("share_token", "VARCHAR"),
            ("model_orient", "JSON"),
            ("inspection_kind", "VARCHAR DEFAULT 'bezna'"),
            ("inspector_company", "VARCHAR"),
            ("protocol_note", "TEXT"),
            ("access_method", "VARCHAR"),
            ("air_temperature", "VARCHAR"),
            ("structure_temperature", "VARCHAR"),
            ("state_substructure", "VARCHAR"),
            ("state_superstructure", "VARCHAR"),
            ("usability", "VARCHAR"),
            ("care_assessment", "TEXT"),
            ("negotiation_date", "VARCHAR"),
            ("reference_number", "VARCHAR"),
            ("negotiation_note", "TEXT"),
            ("authority_opinion", "TEXT"),
        ],
        "annotations": [
            ("report_photo", "VARCHAR"),
            ("status", "VARCHAR DEFAULT 'new'"),
            ("recommended_action", "TEXT"),
            ("responsible", "VARCHAR"),
            ("due_date", "VARCHAR"),
            ("cost_estimate", "FLOAT"),
            ("bridge_part", "VARCHAR"),
            ("action_urgency", "VARCHAR"),
        ],
    }
    for table, cols in wanted.items():
        existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
        for name, decl in cols:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
