// ─── Domain types ────────────────────────────────────────────────────────────

export type StructureType =
  | 'building' | 'bridge' | 'tower' | 'chimney' | 'mast' | 'dam' | 'tunnel' | 'other'

export type InspectionMethod = 'drone' | 'mobile' | 'manual' | 'combined'
export type InspectionStatus = 'draft' | 'in_progress' | 'completed' | 'archived'
export type PhotoSetStatus = 'pending' | 'uploading' | 'processing' | 'completed' | 'failed'

export type DefectType =
  | 'crack' | 'spalling' | 'stain' | 'moss' | 'corrosion'
  | 'deformation' | 'missing_material' | 'water_damage' | 'efflorescence' | 'void' | 'other'

export type Severity = 'low' | 'medium' | 'high' | 'critical'
export type DefectStatus = 'new' | 'monitoring' | 'repaired' | 'dismissed'
export type MeasurementType = 'distance' | 'area' | 'volume' | 'angle' | 'photo'

export interface Vec3 { x: number; y: number; z: number }

// ─── API types ───────────────────────────────────────────────────────────────

export interface StructureMaterial {
  id: string
  material_code: string
  material_name: string
  color: string
  percentage: number
  sort_order: number
}

export interface Structure {
  id: string
  name: string
  type: string           // dynamic code from StructureTypeConfig
  description?: string
  location_name?: string
  latitude?: number
  longitude?: number
  year_built?: number
  thumbnail_path?: string
  created_at: string
  updated_at: string
  inspection_count: number
  last_inspection_date?: string
  reference_inspection_id?: string   // canonical frame of the structure
  materials: StructureMaterial[]
  // bridge register (BMS / mostní list) — printed in the BMS protocol
  evidence_number?: string
  district?: string
  road_number?: string
  chainage_km?: string
  chainage_direction?: string
  load_vn?: string
  load_vr?: string
  load_ve?: string
  max_axle_load?: string
  administrator?: string
}

export interface PhotoSet {
  id: string
  inspection_id: string
  status: PhotoSetStatus
  photo_count: number
  model_path?: string
  point_cloud_path?: string
  orthophoto_path?: string
  progress: number
  engine_used?: string
  error_message?: string
  created_at: string
  completed_at?: string
}

export interface Inspection {
  id: string
  structure_id: string
  name: string
  inspection_date: string
  method?: InspectionMethod
  status: InspectionStatus
  description?: string
  weather?: string
  inspector_name?: string
  scale_factor?: number
  align_ref_id?: string
  align_matrix?: number[]        // row-major 4×4: X_ref_mesh = M · X_this_mesh
  align_fitness?: number
  align_rmse?: number
  align_status?: 'processing' | 'done' | 'failed'
  align_error?: string
  canonical?: boolean            // baked into the structure's canonical frame
  model_orient?: number[]        // [x,y,z,w] display re-orientation quaternion
  approval_status?: 'draft' | 'submitted' | 'approved' | 'rejected'
  approved_by?: string
  approved_at?: string
  share_token?: string
  // BMS inspection protocol
  inspection_kind?: 'bezna' | 'hlavni' | 'prvni_hlavni' | 'mimoradna'
  inspector_company?: string
  protocol_note?: string
  access_method?: string
  air_temperature?: string
  structure_temperature?: string
  state_substructure?: string
  state_superstructure?: string
  usability?: string
  care_assessment?: string
  negotiation_date?: string
  reference_number?: string
  negotiation_note?: string
  authority_opinion?: string
  created_at: string
  photo_set?: PhotoSet
  annotation_count: number
  measurement_count: number
}

export interface Annotation {
  id: string
  inspection_id: string
  defect_type: DefectType
  severity: Severity
  description?: string
  position_x?: number
  position_y?: number
  position_z?: number
  geometry_json?: unknown
  area_m2?: number
  length_m?: number
  image_path?: string
  report_photo?: string
  status?: DefectStatus
  recommended_action?: string
  responsible?: string
  due_date?: string
  cost_estimate?: number
  bridge_part?: string       // BMS část mostu, e.g. "2.3 Mostní závěry"
  action_urgency?: string    // naléhavost opatření
  ai_detected: boolean
  ai_confidence?: number
  created_at: string
}

export interface Measurement {
  id: string
  inspection_id: string
  name?: string
  measurement_type: MeasurementType
  value?: number
  unit: string
  points_json?: Vec3[]
  notes?: string
  created_at: string
}

// ─── Labels ──────────────────────────────────────────────────────────────────

// Structure type labels are now dynamic (from /api/config/structure-types).
// Kept as a fallback for display before config loads.
export const FALLBACK_TYPE_LABEL = (code: string): string => code

export const DEFECT_LABELS: Record<DefectType, string> = {
  crack: 'Prasklina',
  spalling: 'Opadání betonu',
  stain: 'Skvrna',
  moss: 'Mech / vegetace',
  corrosion: 'Koroze / rez',
  deformation: 'Deformace',
  missing_material: 'Chybějící materiál',
  water_damage: 'Poškození vodou',
  efflorescence: 'Výkvět solí',
  void: 'Dutina / hloubková trhlina',
  other: 'Jiné',
}

export const SEVERITY_LABELS: Record<Severity, string> = {
  low: 'Nízká',
  medium: 'Střední',
  high: 'Vysoká',
  critical: 'Kritická',
}

export const SEVERITY_COLORS: Record<Severity, string> = {
  low: '#16a34a',
  medium: '#ca8a04',
  high: '#ea580c',
  critical: '#dc2626',
}

export const DEFECT_STATUS_LABELS: Record<DefectStatus, string> = {
  new: 'Nová',
  monitoring: 'Sledovaná',
  repaired: 'Opravená',
  dismissed: 'Zamítnutá',
}

export const DEFECT_STATUS_BG: Record<DefectStatus, string> = {
  new: 'bg-blue-100 text-blue-800',
  monitoring: 'bg-amber-100 text-amber-800',
  repaired: 'bg-green-100 text-green-800',
  dismissed: 'bg-gray-100 text-gray-600',
}

export const SEVERITY_BG: Record<Severity, string> = {
  low: 'bg-green-100 text-green-800',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-orange-100 text-orange-800',
  critical: 'bg-red-100 text-red-800',
}

export const ROLE_LABELS: Record<string, string> = {
  admin: 'Administrátor',
  inspector: 'Inspektor',
  viewer: 'Prohlížející',
}

export const ROLE_DESCRIPTIONS: Record<string, string> = {
  admin: 'Plný přístup včetně správy uživatelů a katalogů',
  inspector: 'Vytváření a úpravy konstrukcí, inspekcí a poruch',
  viewer: 'Pouze prohlížení, bez možnosti úprav',
}

export const METHOD_LABELS: Record<InspectionMethod, string> = {
  drone: 'Dron',
  mobile: 'Mobilní zařízení',
  manual: 'Manuální',
  combined: 'Kombinovaná',
}

export const STATUS_LABELS: Record<InspectionStatus, string> = {
  draft: 'Návrh',
  in_progress: 'Probíhá',
  completed: 'Dokončeno',
  archived: 'Archivováno',
}
