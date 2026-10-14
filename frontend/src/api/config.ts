import { api } from './client'

export interface StructureTypeConfig {
  id: string
  name: string
  code: string
  icon: string
  sort_order: number
  created_at: string
}

export interface MaterialConfig {
  id: string
  name: string
  code: string
  color: string
  sort_order: number
  created_at: string
}

export const configApi = {
  // Structure types
  listStructureTypes: () =>
    api.get<StructureTypeConfig[]>('/config/structure-types').then(r => r.data),
  createStructureType: (data: Omit<StructureTypeConfig, 'id' | 'created_at'>) =>
    api.post<StructureTypeConfig>('/config/structure-types', data).then(r => r.data),
  updateStructureType: (id: string, data: Partial<StructureTypeConfig>) =>
    api.patch<StructureTypeConfig>(`/config/structure-types/${id}`, data).then(r => r.data),
  deleteStructureType: (id: string) =>
    api.delete(`/config/structure-types/${id}`),

  // Materials
  listMaterials: () =>
    api.get<MaterialConfig[]>('/config/materials').then(r => r.data),
  createMaterial: (data: Omit<MaterialConfig, 'id' | 'created_at'>) =>
    api.post<MaterialConfig>('/config/materials', data).then(r => r.data),
  updateMaterial: (id: string, data: Partial<MaterialConfig>) =>
    api.patch<MaterialConfig>(`/config/materials/${id}`, data).then(r => r.data),
  deleteMaterial: (id: string) =>
    api.delete(`/config/materials/${id}`),
}
