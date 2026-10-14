from datetime import datetime, date
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict, field_validator


# ─── Config ───────────────────────────────────────────────────────────────────

class StructureTypeConfigBase(BaseModel):
    name: str
    code: str
    icon: str = "building"
    sort_order: int = 0


class StructureTypeConfigCreate(StructureTypeConfigBase):
    pass


class StructureTypeConfigUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None


class StructureTypeConfigOut(StructureTypeConfigBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


class MaterialConfigBase(BaseModel):
    name: str
    code: str
    color: str = "#6b7280"
    sort_order: int = 0


class MaterialConfigCreate(MaterialConfigBase):
    pass


class MaterialConfigUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


class MaterialConfigOut(MaterialConfigBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


# ─── Structure material ───────────────────────────────────────────────────────

class StructureMaterialIn(BaseModel):
    material_code: str
    material_name: str
    color: str = "#6b7280"
    percentage: float
    sort_order: int = 0

    @field_validator("percentage")
    @classmethod
    def pct_range(cls, v: float) -> float:
        if v < 0 or v > 100:
            raise ValueError("Procento musí být 0–100")
        return round(v, 2)


class StructureMaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    material_code: str
    material_name: str
    color: str
    percentage: float
    sort_order: int


# ─── Structure ───────────────────────────────────────────────────────────────

class StructureBase(BaseModel):
    name: str
    type: str
    description: Optional[str] = None
    location_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    year_built: Optional[int] = None
    evidence_number: Optional[str] = None
    district: Optional[str] = None
    road_number: Optional[str] = None
    chainage_km: Optional[str] = None
    chainage_direction: Optional[str] = None
    load_vn: Optional[str] = None
    load_vr: Optional[str] = None
    load_ve: Optional[str] = None
    max_axle_load: Optional[str] = None
    administrator: Optional[str] = None


class StructureCreate(StructureBase):
    materials: list[StructureMaterialIn] = []


class StructureUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    location_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    year_built: Optional[int] = None
    evidence_number: Optional[str] = None
    district: Optional[str] = None
    road_number: Optional[str] = None
    chainage_km: Optional[str] = None
    chainage_direction: Optional[str] = None
    load_vn: Optional[str] = None
    load_vr: Optional[str] = None
    load_ve: Optional[str] = None
    max_axle_load: Optional[str] = None
    administrator: Optional[str] = None
    materials: Optional[list[StructureMaterialIn]] = None


class StructureOut(StructureBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    thumbnail_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    inspection_count: int = 0
    last_inspection_date: Optional[date] = None
    reference_inspection_id: Optional[str] = None
    materials: list[StructureMaterialOut] = []


# ─── Inspection ───────────────────────────────────────────────────────────────

class InspectionBase(BaseModel):
    name: str
    inspection_date: date
    method: Optional[str] = None
    status: str = "draft"
    description: Optional[str] = None
    weather: Optional[str] = None
    inspector_name: Optional[str] = None
    scale_factor: Optional[float] = 1.0
    # BMS protocol
    inspection_kind: Optional[str] = "bezna"
    inspector_company: Optional[str] = None
    protocol_note: Optional[str] = None
    access_method: Optional[str] = None
    air_temperature: Optional[str] = None
    structure_temperature: Optional[str] = None
    state_substructure: Optional[str] = None
    state_superstructure: Optional[str] = None
    usability: Optional[str] = None
    care_assessment: Optional[str] = None
    negotiation_date: Optional[str] = None
    reference_number: Optional[str] = None
    negotiation_note: Optional[str] = None
    authority_opinion: Optional[str] = None


class InspectionCreate(InspectionBase):
    pass


class InspectionUpdate(InspectionBase):
    name: Optional[str] = None
    inspection_date: Optional[date] = None
    model_orient: Optional[Any] = None


class InspectionOut(InspectionBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    structure_id: str
    align_ref_id: Optional[str] = None
    align_matrix: Optional[Any] = None
    align_fitness: Optional[float] = None
    align_rmse: Optional[float] = None
    align_status: Optional[str] = None
    align_error: Optional[str] = None
    canonical: bool = False
    model_orient: Optional[Any] = None
    approval_status: str = "draft"
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    share_token: Optional[str] = None
    created_at: datetime
    photo_set: Optional["PhotoSetOut"] = None
    annotation_count: int = 0
    measurement_count: int = 0


# ─── PhotoSet ───────────────────────────────────────────────────────────────

class PhotoSetOut(BaseModel):
    # protected_namespaces=(): field "model_path" would clash with pydantic's "model_" namespace
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: str
    inspection_id: str
    status: str
    photo_count: int
    model_path: Optional[str] = None
    point_cloud_path: Optional[str] = None
    orthophoto_path: Optional[str] = None
    progress: int
    engine_used: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


InspectionOut.model_rebuild()


# ─── Annotation ───────────────────────────────────────────────────────────────

class AnnotationBase(BaseModel):
    defect_type: str
    severity: str
    description: Optional[str] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    position_z: Optional[float] = None
    geometry_json: Optional[Any] = None
    area_m2: Optional[float] = None
    length_m: Optional[float] = None
    report_photo: Optional[str] = None
    status: Optional[str] = None
    recommended_action: Optional[str] = None
    responsible: Optional[str] = None
    due_date: Optional[str] = None
    cost_estimate: Optional[float] = None
    bridge_part: Optional[str] = None
    action_urgency: Optional[str] = None


class AnnotationCreate(AnnotationBase):
    pass


class AnnotationUpdate(AnnotationBase):
    defect_type: Optional[str] = None
    severity: Optional[str] = None


class AnnotationOut(AnnotationBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    inspection_id: str
    image_path: Optional[str] = None
    ai_detected: bool = False
    ai_confidence: Optional[float] = None
    created_at: datetime


# ─── Measurement ───────────────────────────────────────────────────────────────

class MeasurementBase(BaseModel):
    name: Optional[str] = None
    measurement_type: str = "distance"
    value: Optional[float] = None
    unit: str = "m"
    points_json: Optional[Any] = None
    notes: Optional[str] = None


class MeasurementCreate(MeasurementBase):
    pass


class MeasurementUpdate(MeasurementBase):
    pass


class MeasurementOut(MeasurementBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    inspection_id: str
    created_at: datetime


# ─── Report ───────────────────────────────────────────────────────────────────

class ReportRequest(BaseModel):
    title: Optional[str] = None
    include_photos: bool = True
    include_measurements: bool = True
    include_annotations: bool = True
    severity_filter: Optional[list[str]] = None
    notes: Optional[str] = None
    # ─── Branding / protocol header (all optional) ────────────────────────────
    company_name: Optional[str] = None
    protocol_number: Optional[str] = None
    logo: Optional[str] = None            # data-URL or base64 PNG/JPEG of a logo
    prepared_by: Optional[str] = None     # zpracoval (jméno pro podpisový blok)
    # 3D snapshot with numbered defect markers, captured on the client (data-URL)
    scene_snapshot: Optional[str] = None
    # face-on 3D close-up per defect, keyed by annotation id → data-URL
    defect_snapshots: Optional[dict] = None


class ComparisonReportRequest(BaseModel):
    inspection_a: str
    inspection_b: str
    snapshot_a: Optional[str] = None    # data-URL 3D snapshot of inspection A
    snapshot_b: Optional[str] = None


# ─── Users & auth ─────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    full_name: Optional[str] = None
    role: str
    is_active: bool
    must_change_password: bool = False
    created_at: datetime
    last_login: Optional[datetime] = None


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "viewer"

    @field_validator("username")
    @classmethod
    def _username_ok(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Uživatelské jméno musí mít alespoň 3 znaky")
        return v

    @field_validator("password")
    @classmethod
    def _password_ok(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Heslo musí mít alespoň 6 znaků")
        return v

    @field_validator("role")
    @classmethod
    def _role_ok(cls, v: str) -> str:
        if v not in ("admin", "inspector", "viewer"):
            raise ValueError("Neplatná role")
        return v


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _role_ok(cls, v):
        if v is not None and v not in ("admin", "inspector", "viewer"):
            raise ValueError("Neplatná role")
        return v


class PasswordChange(BaseModel):
    current_password: Optional[str] = None  # required when changing own password
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _len(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Heslo musí mít alespoň 6 znaků")
        return v


TokenOut.model_rebuild()
