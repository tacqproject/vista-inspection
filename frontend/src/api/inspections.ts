import { api } from './client'
import type { Inspection, Annotation, Measurement, PhotoSet, Vec3 } from '../types'

export const inspectionsApi = {
  list: (structureId: string) =>
    api.get<Inspection[]>(`/structures/${structureId}/inspections`).then(r => r.data),
  get: (structureId: string, id: string) =>
    api.get<Inspection>(`/structures/${structureId}/inspections/${id}`).then(r => r.data),
  create: (structureId: string, data: Partial<Inspection>) =>
    api.post<Inspection>(`/structures/${structureId}/inspections`, data).then(r => r.data),
  update: (structureId: string, id: string, data: Partial<Inspection>) =>
    api.patch<Inspection>(`/structures/${structureId}/inspections/${id}`, data).then(r => r.data),
  delete: (structureId: string, id: string) =>
    api.delete(`/structures/${structureId}/inspections/${id}`),

  /** Register this inspection's model onto a reference inspection (same structure). */
  align: (structureId: string, id: string, refInspectionId: string) =>
    api.post(`/structures/${structureId}/inspections/${id}/align`, {
      ref_inspection_id: refInspectionId,
    }).then(r => r.data),

  alignManual: (
    structureId: string, id: string, refInspectionId: string,
    pairs: Array<{ a: Vec3; b: Vec3 }>,
  ) =>
    api.post(`/structures/${structureId}/inspections/${id}/align-manual`, {
      ref_inspection_id: refInspectionId, pairs,
    }).then(r => r.data),

  /** Report approval workflow (submit / approve / reject / reopen). */
  setApproval: (structureId: string, id: string, action: 'submit' | 'approve' | 'reject' | 'reopen') =>
    api.post<Inspection>(`/structures/${structureId}/inspections/${id}/approval`, { action }).then(r => r.data),

  /** Mint a read-only share token for this inspection's protocol. */
  createShare: (structureId: string, id: string) =>
    api.post<Inspection>(`/structures/${structureId}/inspections/${id}/share`).then(r => r.data),
  revokeShare: (structureId: string, id: string) =>
    api.delete<Inspection>(`/structures/${structureId}/inspections/${id}/share`).then(r => r.data),

  /** Make `id` the structure's canonical frame; bakes all others into it. */
  setReference: (structureId: string, id: string) =>
    api.post(`/structures/${structureId}/inspections/set-reference`, {
      reference_inspection_id: id,
    }).then(r => r.data),

  /** Fold a manual overlay fine-tune (scene-frame rotate + translate) into the
   *  stored alignment of inspection `id`. */
  adjustOverlay: (
    structureId: string, id: string,
    adjust: { tx: number; ty: number; tz: number; rx: number; ry: number; rz: number },
  ) =>
    api.post(`/structures/${structureId}/inspections/${id}/adjust-overlay`, adjust)
      .then(r => r.data),

  /** Best source photo of one physical spot across every inspection (date-ordered)
   *  — a degradation timeline for the clicked place. */
  timelinePhotos: (
    structureId: string,
    body: { inspection_id: string; x: number; y: number; z: number; canonical?: boolean; nx?: number; ny?: number; nz?: number },
  ): Promise<{ items: Array<{
    inspection_id: string; name: string; date: string
    photos: Array<{ filename: string; u: number; v: number; distance?: number }>
  }> }> =>
    api.post(`/structures/${structureId}/inspections/timeline-photos`, body).then(r => r.data),
}

export const uploadsApi = {
  /** Uploads in batches (≤30 files / ~300 MB each) so hundreds of drone photos
   *  don't ride on one multi-GB request; each batch is retried twice. The last
   *  batch starts the reconstruction. */
  uploadPhotos: async (
    inspectionId: string,
    files: File[],
    onProgress?: (pct: number) => void,
    engine?: string,
  ) => {
    const batches: File[][] = []
    let cur: File[] = [], curSize = 0
    for (const f of files) {
      if (cur.length && (cur.length >= 30 || curSize + f.size > 300e6)) {
        batches.push(cur); cur = []; curSize = 0
      }
      cur.push(f); curSize += f.size
    }
    if (cur.length || !batches.length) batches.push(cur)
    const total = files.reduce((s, f) => s + f.size, 0) || 1
    let done = 0
    let last: PhotoSet | undefined
    for (let i = 0; i < batches.length; i++) {
      const b = batches[i]
      const bytes = b.reduce((s, f) => s + f.size, 0)
      for (let attempt = 0; ; attempt++) {
        const fd = new FormData()
        b.forEach(f => fd.append('files', f))
        if (engine) fd.append('engine', engine)
        try {
          last = await api.post<PhotoSet>(`/inspections/${inspectionId}/upload`, fd, {
            headers: { 'Content-Type': 'multipart/form-data' },
            params: { append: i > 0, start: i === batches.length - 1 },
            onUploadProgress: e => {
              if (onProgress) onProgress(Math.round(((done + (e.loaded / (e.total || bytes)) * bytes) / total) * 100))
            },
          }).then(r => r.data)
          break
        } catch (err) {
          if (attempt >= 2) throw err
          await new Promise(r => setTimeout(r, 2000 * (attempt + 1)))
        }
      }
      done += bytes
      onProgress?.(Math.round((done / total) * 100))
    }
    return last as PhotoSet
  },
  getPhotoSet: (inspectionId: string) =>
    api.get<PhotoSet>(`/inspections/${inspectionId}/photo-set`).then(r => r.data),
  reprocess: (inspectionId: string, options?: {
    quality?: 'draft' | 'standard' | 'high' | 'max'
    keep_largest_mesh?: boolean
    max_points?: number
    texture_size?: number
    refine_mesh?: boolean
    refine_faces?: number
    pipeline?: string          // filename of an imported Meshroom .mg template
    web_postprocess?: boolean  // textures/LODs/mobile variant chain
    ai_normals?: boolean       // max: AI (DSINE) normal-guided denoising
    ai_defects?: boolean       // AI defect detection on photos → annotations
    photo_prep?: boolean       // exposure balance, shadow lift, cull blurred/duplicates
  }) =>
    api.post<PhotoSet>(`/inspections/${inspectionId}/reprocess`, options ?? {}).then(r => r.data),

  cropModel: (inspectionId: string, points: Array<{ x: number; y: number; z: number }>) =>
    api.post<PhotoSet>(`/inspections/${inspectionId}/crop-model`, { points }).then(r => r.data),

  restoreModel: (inspectionId: string) =>
    api.post<PhotoSet>(`/inspections/${inspectionId}/crop-model`, { restore: true }).then(r => r.data),
}

export interface EngineInfo {
  available: boolean
  path?: string
  version?: string
  hint?: string
  url?: string
}

export interface EngineStatus {
  current: string
  available: string[]
  meshroom: EngineInfo
}

export const configApi = {
  engines: () => api.get<EngineStatus>('/config/engines').then(r => r.data),

  // Meshroom .mg pipeline templates (exported from Meshroom GUI)
  listPipelines: (): Promise<{ pipelines: Array<{ filename: string; size: number }> }> =>
    api.get('/config/pipelines').then(r => r.data),
  uploadPipeline: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post('/config/pipelines', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(r => r.data)
  },
  deletePipeline: (filename: string) =>
    api.delete(`/config/pipelines/${encodeURIComponent(filename)}`),
}

// null explicitly clears an optional field on update (undefined leaves it)
type AnnotationInput = Omit<Partial<Annotation>,
  'length_m' | 'area_m2' | 'report_photo' | 'recommended_action' | 'responsible' | 'due_date' | 'cost_estimate'
  | 'bridge_part' | 'action_urgency'> & {
  length_m?: number | null
  area_m2?: number | null
  report_photo?: string | null
  recommended_action?: string | null
  responsible?: string | null
  due_date?: string | null
  cost_estimate?: number | null
  bridge_part?: string | null
  action_urgency?: string | null
}

export const annotationsApi = {
  list: (inspectionId: string) =>
    api.get<Annotation[]>(`/inspections/${inspectionId}/annotations`).then(r => r.data),
  create: (inspectionId: string, data: AnnotationInput) =>
    api.post<Annotation>(`/inspections/${inspectionId}/annotations`, data).then(r => r.data),
  update: (inspectionId: string, id: string, data: AnnotationInput) =>
    api.patch<Annotation>(`/inspections/${inspectionId}/annotations/${id}`, data).then(r => r.data),
  delete: (inspectionId: string, id: string) =>
    api.delete(`/inspections/${inspectionId}/annotations/${id}`),
}

export interface DetailPhoto { filename: string; url: string; size: number }

export const annotationPhotosApi = {
  list: (inspectionId: string, annotationId: string): Promise<{ photos: DetailPhoto[]; total: number }> =>
    api.get(`/inspections/${inspectionId}/annotations/${annotationId}/photos`).then(r => r.data),
  upload: (inspectionId: string, annotationId: string, files: File[]) => {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    return api.post(`/inspections/${inspectionId}/annotations/${annotationId}/photos`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(r => r.data)
  },
  delete: (inspectionId: string, annotationId: string, filename: string) =>
    api.delete(`/inspections/${inspectionId}/annotations/${annotationId}/photos/${encodeURIComponent(filename)}`),
  analyze: (inspectionId: string, annotationId: string, filename: string): Promise<DefectAnalysis> =>
    api.post(`/inspections/${inspectionId}/annotations/${annotationId}/analyze-photo`, { filename })
      .then(r => r.data),
  /** Attach an existing SOURCE photo (from "snímky s tímto místem") as a detail photo. */
  attachSource: (inspectionId: string, annotationId: string, filename: string) =>
    api.post(`/inspections/${inspectionId}/annotations/${annotationId}/attach-source-photo`, { filename })
      .then(r => r.data),
}

export interface DefectAnalysis {
  found: boolean
  count: number
  regions: Array<{ bbox: [number, number, number, number]; length_px: number; width_px: number; elongation: number }>
  max_width_px: number
  total_length_px: number
  crack_ratio: number
  severity_guess: 'low' | 'medium' | 'high' | 'critical'
  defect_type_guess: string
  confidence: number
  image_width: number
  image_height: number
  overlay: string
}

export const measurementsApi = {
  list: (inspectionId: string) =>
    api.get<Measurement[]>(`/inspections/${inspectionId}/measurements`).then(r => r.data),
  create: (inspectionId: string, data: Partial<Measurement>) =>
    api.post<Measurement>(`/inspections/${inspectionId}/measurements`, data).then(r => r.data),
  update: (inspectionId: string, id: string, data: Partial<Measurement>) =>
    api.patch<Measurement>(`/inspections/${inspectionId}/measurements/${id}`, data).then(r => r.data),
  delete: (inspectionId: string, id: string) =>
    api.delete(`/inspections/${inspectionId}/measurements/${id}`),
}

export interface ProjectedPhoto {
  filename: string
  u: number
  v: number
  distance: number
  centrality: number
  // polygon boundary projected into this photo (null entries = vertices behind
  // the lens); present only when an outline was sent
  outline?: Array<{ u: number; v: number } | null> | null
}

export const photosApi = {
  list: (inspectionId: string): Promise<{ photos: Array<{ filename: string; url: string; size: number }>; total: number }> =>
    api.get(`/inspections/${inspectionId}/photos`).then(r => r.data),

  projectPoint: (
    inspectionId: string,
    pt: { x: number; y: number; z: number; nx?: number; ny?: number; nz?: number; outline?: number[][] },
  ): Promise<{ photos: ProjectedPhoto[]; total: number; occluded?: boolean }> =>
    api.post(`/inspections/${inspectionId}/project-point`, pt).then(r => r.data),

  measurePhoto: (inspectionId: string, filename: string, points: Array<{ u: number; v: number }>): Promise<{
    results: Array<{ hit: boolean; point: [number, number, number] | null }>
    distance_model: number | null
  }> =>
    api.post(`/inspections/${inspectionId}/measure-photo`, { filename, points }).then(r => r.data),

  cameras: (inspectionId: string): Promise<{ cameras: Array<{
    filename: string
    position: [number, number, number]
    forward: [number, number, number]
    up: [number, number, number]
    pp: [number, number]
    aspect: number
    fov_deg: number
  }>; total: number }> =>
    api.get(`/inspections/${inspectionId}/cameras`).then(r => r.data),

  thumbnailUrl: (inspectionId: string, filename: string, size = 400) =>
    `/api/inspections/${inspectionId}/photos/${encodeURIComponent(filename)}/thumbnail?size=${size}`,

  importModel: (inspectionId: string, files: File[], onProgress?: (pct: number) => void) => {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    return api.post(`/inspections/${inspectionId}/import-model`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: e => {
        if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100))
      },
    }).then(r => r.data)
  },
}

export interface AiSegmentImage {
  filename: string
  overlay: string
  overlay_url?: string
  coverage: Record<string, number>
}
export interface AiSegmentResult {
  status: 'none' | 'processing' | 'completed' | 'failed'
  progress?: number
  message?: string
  error?: string
  n_images?: number
  n_with_damage?: number
  classes?: string[]
  images?: AiSegmentImage[]
  class_labels?: Record<string, string>
  damage_classes?: string[]
  engine?: { available: boolean; hint?: string; cmd?: string }
}

export const aiApi = {
  get: (inspectionId: string): Promise<AiSegmentResult> =>
    api.get(`/inspections/${inspectionId}/ai-segment`).then(r => r.data),
  start: (inspectionId: string): Promise<{ status: string }> =>
    api.post(`/inspections/${inspectionId}/ai-segment`).then(r => r.data),
}

export interface BmsCatalogue {
  parts: { code: string; name: string; label: string }[]
  groups: Record<string, string>
  urgencies: string[]
  kinds: Record<string, string>
}

function saveBlob(data: BlobPart, type: string, fallbackName: string, disposition?: string) {
  const m = /filename="([^"]+)"/.exec(disposition || '')
  const url = URL.createObjectURL(new Blob([data], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = m?.[1] || fallbackName
  a.click()
  URL.revokeObjectURL(url)
}

export const reportsApi = {
  bmsCatalogue: (): Promise<BmsCatalogue> => api.get('/reports/bms/catalogue').then(r => r.data),
  /** ŘSD BMS protocol (.docx, or .pdf rendered from the same .docx). */
  downloadBms: async (inspectionId: string, body: { format: 'docx' | 'pdf'; defect_snapshots?: Record<string, string> }) => {
    const r = await api.post(`/reports/inspection/${inspectionId}/bms`, body, { responseType: 'blob' })
    const type = body.format === 'pdf' ? 'application/pdf'
      : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    saveBlob(r.data, type, `protokol_${inspectionId.slice(0, 8)}.${body.format}`, r.headers['content-disposition'])
  },
  appendixPhotos: (inspectionId: string): Promise<{ photos: { filename: string; url: string }[] }> =>
    api.get(`/reports/inspection/${inspectionId}/appendix-photos`).then(r => r.data),
  uploadAppendixPhotos: (inspectionId: string, files: File[]) => {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    return api.post(`/reports/inspection/${inspectionId}/appendix-photos`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(r => r.data)
  },
  deleteAppendixPhoto: (inspectionId: string, filename: string) =>
    api.delete(`/reports/inspection/${inspectionId}/appendix-photos/${encodeURIComponent(filename)}`),
  downloadAiSegmentation: async (inspectionId: string) => {
    const r = await api.post(`/reports/ai-segmentation/${inspectionId}`, {}, { responseType: 'blob' })
    const url = URL.createObjectURL(new Blob([r.data], { type: 'application/pdf' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `ai_analyza_${inspectionId.slice(0, 8)}.pdf`
    a.click()
    URL.revokeObjectURL(url)
  },
  downloadInspection: async (inspectionId: string, options: object = {}) => {
    const r = await api.post(`/reports/inspection/${inspectionId}`, options, {
      responseType: 'blob',
    })
    const url = URL.createObjectURL(new Blob([r.data], { type: 'application/pdf' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `inspekce_${inspectionId.slice(0, 8)}.pdf`
    a.click()
    URL.revokeObjectURL(url)
  },
  downloadStructure: async (structureId: string, options: object = {}) => {
    const r = await api.post(`/reports/structure/${structureId}`, options, {
      responseType: 'blob',
    })
    const url = URL.createObjectURL(new Blob([r.data], { type: 'application/pdf' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `konstrukce_${structureId.slice(0, 8)}.pdf`
    a.click()
    URL.revokeObjectURL(url)
  },
  downloadComparison: async (body: {
    inspection_a: string; inspection_b: string
    snapshot_a?: string; snapshot_b?: string
  }) => {
    const r = await api.post('/reports/compare', body, { responseType: 'blob' })
    const url = URL.createObjectURL(new Blob([r.data], { type: 'application/pdf' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'porovnani_inspekci.pdf'
    a.click()
    URL.revokeObjectURL(url)
  },
}
