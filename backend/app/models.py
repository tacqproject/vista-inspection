import uuid
from datetime import datetime, date
from sqlalchemy import (
    Column, String, Text, Float, Integer, Boolean,
    DateTime, Date, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.sqlite import JSON as SQLJSON

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


# ─── Users & access control ───────────────────────────────────────────────────

# Roles, from most to least privileged. "admin" also manages users and catalogs;
# "inspector" may create/edit inspection data; "viewer" is read-only.
ROLES = ("admin", "inspector", "viewer")


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_uuid)
    username = Column(String, nullable=False, unique=True)
    full_name = Column(String)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="viewer")  # one of ROLES
    is_active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)


# ─── Configurables ────────────────────────────────────────────────────────────

class StructureTypeConfig(Base):
    """User-editable list of structure types."""
    __tablename__ = "structure_type_configs"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)       # "Budova"
    code = Column(String, nullable=False, unique=True)  # "building"
    icon = Column(String, default="building")   # lucide icon name (optional)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class MaterialConfig(Base):
    """User-editable list of materials."""
    __tablename__ = "material_configs"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)       # "Beton"
    code = Column(String, nullable=False, unique=True)  # "concrete"
    color = Column(String, default="#6b7280")   # hex color for visualization
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── Structure ────────────────────────────────────────────────────────────────

class Structure(Base):
    __tablename__ = "structures"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # references StructureTypeConfig.code
    description = Column(Text)
    location_name = Column(String)
    latitude = Column(Float)
    longitude = Column(Float)
    year_built = Column(Integer)
    thumbnail_path = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Canonical frame of the structure: the inspection whose model coordinates all
    # other inspections are baked into, so views/coords/scale line up everywhere.
    reference_inspection_id = Column(String)
    # ─── Bridge register (BMS / mostní list) — used by the BMS inspection protocol
    evidence_number = Column(String)      # ev. č. mostu, e.g. "16-045a"
    district = Column(String)             # okres
    road_number = Column(String)          # číslo komunikace
    chainage_km = Column(String)          # staničení [km]
    chainage_direction = Column(String)   # staničení ve směru (cíl)
    load_vn = Column(String)              # normální zatížitelnost
    load_vr = Column(String)              # výhradní zatížitelnost
    load_ve = Column(String)              # výjimečná zatížitelnost
    max_axle_load = Column(String)        # max. nápravový tlak
    administrator = Column(String)        # správce mostu

    inspections = relationship("Inspection", back_populates="structure", cascade="all, delete-orphan")
    materials = relationship("StructureMaterial", back_populates="structure",
                             cascade="all, delete-orphan", order_by="StructureMaterial.sort_order")


class StructureMaterial(Base):
    """Material composition of a structure (multiple entries, each with %)."""
    __tablename__ = "structure_materials"

    id = Column(String, primary_key=True, default=gen_uuid)
    structure_id = Column(String, ForeignKey("structures.id"), nullable=False)
    material_code = Column(String, nullable=False)   # references MaterialConfig.code
    material_name = Column(String, nullable=False)   # denormalized for display/report
    color = Column(String, default="#6b7280")        # copied from MaterialConfig at creation
    percentage = Column(Float, nullable=False)       # 0–100
    sort_order = Column(Integer, default=0)

    structure = relationship("Structure", back_populates="materials")


class Inspection(Base):
    __tablename__ = "inspections"

    id = Column(String, primary_key=True, default=gen_uuid)
    structure_id = Column(String, ForeignKey("structures.id"), nullable=False)
    name = Column(String, nullable=False)
    inspection_date = Column(Date, nullable=False)
    method = Column(String)  # drone|mobile|manual|combined
    status = Column(String, default="draft")  # draft|in_progress|completed|archived
    description = Column(Text)
    weather = Column(String)
    inspector_name = Column(String)
    scale_factor = Column(Float, default=1.0)  # model units → meters (set via calibration)

    # Spatial registration onto another inspection of the same structure — lets a
    # partial re-scan be dropped into the reference model's frame for comparison.
    align_ref_id = Column(String)      # reference inspection
    align_matrix = Column(SQLJSON)     # row-major 4×4: X_ref_mesh = M · X_this_mesh
    align_fitness = Column(Float)      # overlap ratio (partial scans are < 1)
    align_rmse = Column(Float)         # metres
    align_status = Column(String)      # processing | done | failed
    align_error = Column(Text)

    # True once this inspection's model has been baked into the structure's
    # canonical (reference) frame — the viewer then skips its own normalization.
    canonical = Column(Boolean, default=False)

    # Display-only re-orientation of the whole model, [x,y,z,w] quaternion, applied
    # in the viewer (e.g. a Z-up reality scan flipped upright). Non-destructive.
    model_orient = Column(SQLJSON)

    # ─── Report approval workflow ─────────────────────────────────────────────
    approval_status = Column(String, default="draft")  # draft|submitted|approved|rejected
    approved_by = Column(String)                        # username who approved/rejected
    approved_at = Column(String)                        # ISO timestamp
    # opaque token that grants read-only public access to this inspection's report
    share_token = Column(String)
    # ─── BMS inspection protocol (ČSN 73 6221) ────────────────────────────────
    inspection_kind = Column(String, default="bezna")   # bezna|hlavni|prvni_hlavni|mimoradna
    inspector_company = Column(String)     # firma, která prohlídku provedla
    protocol_note = Column(Text)           # Poznámka (oprávnění, rámcová smlouva, zkratky…)
    access_method = Column(String)         # Způsob zpřístupnění
    air_temperature = Column(String)       # Teplota vzduchu
    structure_temperature = Column(String) # Teplota NK
    state_substructure = Column(String)    # Stav mostu – spodní stavba (I–VII)
    state_superstructure = Column(String)  # Stav mostu – nosná konstrukce (I–VII)
    usability = Column(String)             # Použitelnost
    care_assessment = Column(Text)         # D. Hodnocení péče o most…
    negotiation_date = Column(String)      # F. Datum projednání
    reference_number = Column(String)      # F. Číslo jednací
    negotiation_note = Column(Text)        # F. Poznámka
    authority_opinion = Column(Text)       # H. Stanovisko nadřízeného orgánu

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    structure = relationship("Structure", back_populates="inspections")
    photo_set = relationship("PhotoSet", back_populates="inspection", uselist=False, cascade="all, delete-orphan")
    annotations = relationship("Annotation", back_populates="inspection", cascade="all, delete-orphan")
    measurements = relationship("Measurement", back_populates="inspection", cascade="all, delete-orphan")


class PhotoSet(Base):
    __tablename__ = "photo_sets"

    id = Column(String, primary_key=True, default=gen_uuid)
    inspection_id = Column(String, ForeignKey("inspections.id"), nullable=False, unique=True)
    status = Column(String, default="pending")  # pending|uploading|processing|completed|failed
    photo_count = Column(Integer, default=0)
    upload_dir = Column(String)
    model_path = Column(String)           # relative path to OBJ/GLB model
    point_cloud_path = Column(String)     # relative path to point cloud
    orthophoto_path = Column(String)      # relative path to orthophoto
    progress = Column(Integer, default=0)
    engine_used = Column(String)          # mock|odm|colmap
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

    inspection = relationship("Inspection", back_populates="photo_set")


DEFECT_TYPES = [
    "crack",          # prasklina
    "spalling",       # opadání betonu
    "stain",          # skvrna
    "moss",           # mech/vegetace
    "corrosion",      # koroze/rez
    "deformation",    # deformace
    "missing_material",  # chybějící materiál
    "water_damage",   # poškození vodou
    "efflorescence",  # výkvět solí
    "void",           # dutina/trhlina v hloubce
    "other",
]

SEVERITY_LEVELS = ["low", "medium", "high", "critical"]


class Annotation(Base):
    __tablename__ = "annotations"

    id = Column(String, primary_key=True, default=gen_uuid)
    inspection_id = Column(String, ForeignKey("inspections.id"), nullable=False)
    defect_type = Column(String, nullable=False)
    severity = Column(String, nullable=False, default="low")
    position_x = Column(Float)
    position_y = Column(Float)
    position_z = Column(Float)
    geometry_json = Column(SQLJSON)   # polygon or bbox points array
    description = Column(Text)
    image_path = Column(String)       # screenshot of defect
    report_photo = Column(String)     # filename of the detail photo chosen for the PDF protocol
    area_m2 = Column(Float)
    length_m = Column(Float)
    # ─── Lifecycle / workflow ─────────────────────────────────────────────────
    status = Column(String, default="new")   # new | monitoring | repaired | dismissed
    recommended_action = Column(Text)        # doporučené opatření
    responsible = Column(String)             # odpovědná osoba
    due_date = Column(String)                # ISO date the action is due
    cost_estimate = Column(Float)            # odhad nákladů (Kč)
    bridge_part = Column(String)             # část mostu dle BMS, e.g. "2.3 Mostní závěry"
    action_urgency = Column(String)          # naléhavost opatření, e.g. "odstranění nutno do 1 roku"
    ai_detected = Column(Boolean, default=False)
    ai_confidence = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

    inspection = relationship("Inspection", back_populates="annotations")


class Measurement(Base):
    __tablename__ = "measurements"

    id = Column(String, primary_key=True, default=gen_uuid)
    inspection_id = Column(String, ForeignKey("inspections.id"), nullable=False)
    name = Column(String)
    measurement_type = Column(String, default="distance")  # distance|area|volume|angle
    value = Column(Float)
    unit = Column(String, default="m")
    points_json = Column(SQLJSON)  # [{x,y,z}, ...]
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    inspection = relationship("Inspection", back_populates="measurements")
