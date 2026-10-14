import { api } from './client'
import type { Structure, StructureMaterial } from '../types'

interface StructureCreatePayload {
  name: string
  type: string
  description?: string
  location_name?: string
  latitude?: number
  longitude?: number
  year_built?: number
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
  materials?: Array<{
    material_code: string
    material_name: string
    color: string
    percentage: number
    sort_order?: number
  }>
}

interface StructureUpdatePayload extends Partial<StructureCreatePayload> {}

export const structuresApi = {
  list: () => api.get<Structure[]>('/structures').then(r => r.data),
  get: (id: string) => api.get<Structure>(`/structures/${id}`).then(r => r.data),
  create: (data: StructureCreatePayload) =>
    api.post<Structure>('/structures', data).then(r => r.data),
  update: (id: string, data: StructureUpdatePayload) =>
    api.patch<Structure>(`/structures/${id}`, data).then(r => r.data),
  delete: (id: string) => api.delete(`/structures/${id}`),
  uploadThumbnail: (id: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post(`/structures/${id}/thumbnail`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(r => r.data)
  },
  measurementTrends: (id: string): Promise<{ series: MeasurementTrend[]; total: number }> =>
    api.get(`/structures/${id}/measurement-trends`).then(r => r.data),
}

export interface MeasurementTrend {
  name: string
  unit: string
  first: number
  last: number
  change: number
  change_pct: number
  trend: 'up' | 'down' | 'flat'
  points: Array<{ value: number; unit: string; inspection_id: string; inspection_name: string; date: string | null }>
}
