import { useMemo, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Building2, ArrowLeft, Plus, Calendar, Wrench, ClipboardList,
  MapPin, Trash2, FileText, ChevronRight, Pencil, Check, X,
  Camera, GitCompareArrows, Search, Crosshair,
} from 'lucide-react'
import { structuresApi } from '../api/structures'
import { inspectionsApi, reportsApi } from '../api/inspections'
import { configApi } from '../api/config'
import { STATUS_LABELS, METHOD_LABELS, type InspectionMethod, type InspectionStatus, type StructureMaterial } from '../types'
import { uploadsUrl } from '../api/client'
import MaterialsEditor, { type MaterialRow } from '../components/MaterialsEditor'
import EditStructureModal from '../components/EditStructureModal'
import MeasurementTrends from '../components/MeasurementTrends'
import { useAuthStore } from '../store/authStore'

export default function StructureDetail() {
  const { structureId } = useParams<{ structureId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const canEdit = useAuthStore(s => s.canEdit())
  const [showNewInsp, setShowNewInsp] = useState(false)
  const [showEdit, setShowEdit] = useState(false)
  const [reportLoading, setReportLoading] = useState(false)
  const [editingMaterials, setEditingMaterials] = useState(false)
  const [draftMaterials, setDraftMaterials] = useState<MaterialRow[]>([])
  const [compareMode, setCompareMode] = useState(false)
  const [compareSel, setCompareSel] = useState<string[]>([])
  const thumbRef = useRef<HTMLInputElement>(null)

  // inspection filters
  const [inspSearch, setInspSearch] = useState('')
  const [inspStatus, setInspStatus] = useState('')
  const [inspMethod, setInspMethod] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  const { data: structure, isLoading } = useQuery({
    queryKey: ['structures', structureId],
    queryFn: () => structuresApi.get(structureId!),
    enabled: !!structureId,
  })

  const { data: structureTypes = [] } = useQuery({
    queryKey: ['config', 'structure-types'],
    queryFn: configApi.listStructureTypes,
  })

  const { data: inspections = [] } = useQuery({
    queryKey: ['inspections', structureId],
    queryFn: () => inspectionsApi.list(structureId!),
    enabled: !!structureId,
  })

  const norm = (s: string) => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
  const filteredInspections = useMemo(() => {
    const q = norm(inspSearch.trim())
    return inspections.filter(i => {
      if (inspStatus && i.status !== inspStatus) return false
      if (inspMethod && i.method !== inspMethod) return false
      if (dateFrom && i.inspection_date < dateFrom) return false
      if (dateTo && i.inspection_date > dateTo) return false
      if (!q) return true
      return norm(i.name).includes(q)
        || norm(i.inspector_name ?? '').includes(q)
        || norm(i.description ?? '').includes(q)
    })
  }, [inspections, inspSearch, inspStatus, inspMethod, dateFrom, dateTo])
  const filtersActive = !!(inspSearch || inspStatus || inspMethod || dateFrom || dateTo)

  const deleteStructure = useMutation({
    mutationFn: () => structuresApi.delete(structureId!),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['structures'] }); navigate('/') },
  })

  const deleteInspection = useMutation({
    mutationFn: (id: string) => inspectionsApi.delete(structureId!, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['inspections', structureId] })
      qc.invalidateQueries({ queryKey: ['structures', structureId] })
    },
  })

  const uploadThumb = useMutation({
    mutationFn: (file: File) => structuresApi.uploadThumbnail(structureId!, file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['structures', structureId] })
      qc.invalidateQueries({ queryKey: ['structures'] })
    },
  })

  const toggleCompareSel = (id: string) => {
    setCompareSel(sel =>
      sel.includes(id) ? sel.filter(s => s !== id)
      : sel.length >= 2 ? [sel[1], id]   // keep last selected + new one
      : [...sel, id],
    )
  }

  const updateMaterials = useMutation({
    mutationFn: (materials: MaterialRow[]) =>
      structuresApi.update(structureId!, {
        materials: materials.filter(m => m.percentage > 0).map((m, i) => ({ ...m, sort_order: i })),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['structures', structureId] })
      qc.invalidateQueries({ queryKey: ['structures'] })
      setEditingMaterials(false)
    },
  })

  const handleReport = async () => {
    setReportLoading(true)
    try { await reportsApi.downloadStructure(structureId!) }
    finally { setReportLoading(false) }
  }

  const startEditMaterials = () => {
    setDraftMaterials((structure?.materials ?? []).map(m => ({
      material_code: m.material_code,
      material_name: m.material_name,
      color: m.color,
      percentage: m.percentage,
    })))
    setEditingMaterials(true)
  }

  if (isLoading) return <div className="p-8 text-gray-400">Načítám…</div>
  if (!structure) return <div className="p-8 text-gray-400">Konstrukce nenalezena</div>

  const typeName = structureTypes.find(t => t.code === structure.type)?.name ?? structure.type

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <button onClick={() => navigate('/')} className="btn-ghost mb-4 -ml-2">
        <ArrowLeft size={16} /> Zpět
      </button>

      {/* Header card */}
      <div className="card overflow-hidden mb-6">
        <div className="h-48 bg-gradient-to-br from-slate-200 to-slate-300 relative group">
          {structure.thumbnail_path ? (
            <img src={uploadsUrl(structure.thumbnail_path)} alt={structure.name} className="w-full h-full object-cover" />
          ) : (
            <div className="flex items-center justify-center h-full">
              <Building2 size={60} className="text-slate-300" />
            </div>
          )}
          {canEdit && (
          <button
            onClick={() => thumbRef.current?.click()}
            className="absolute bottom-3 right-3 bg-black/55 hover:bg-black/75 text-white text-xs rounded-lg px-3 py-1.5 flex items-center gap-1.5 transition-colors"
          >
            <Camera size={13} />
            {uploadThumb.isPending ? 'Nahrávám…' : structure.thumbnail_path ? 'Změnit fotku' : 'Nahrát úvodní fotku'}
          </button>
          )}
          <input
            ref={thumbRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={e => {
              const f = e.target.files?.[0]
              if (f) uploadThumb.mutate(f)
              e.target.value = ''
            }}
          />
        </div>
        <div className="p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 mb-1">
                <span className="badge bg-brand-100 text-brand-800">{typeName}</span>
              </div>
              <h1 className="text-2xl font-bold text-gray-900">{structure.name}</h1>
              {structure.location_name && (
                <p className="text-sm text-gray-500 flex items-center gap-1 mt-1">
                  <MapPin size={13} /> {structure.location_name}
                </p>
              )}
            </div>
            <div className="flex gap-2 shrink-0">
              {canEdit && (
                <button onClick={() => setShowEdit(true)} className="btn-secondary text-xs">
                  <Pencil size={14} /> Upravit
                </button>
              )}
              <button onClick={handleReport} disabled={reportLoading} className="btn-secondary text-xs">
                <FileText size={14} /> {reportLoading ? 'Generuji…' : 'Zpráva'}
              </button>
              {canEdit && (
                <button onClick={() => { if (confirm('Smazat konstrukci a všechna data?')) deleteStructure.mutate() }} className="btn-danger text-xs">
                  <Trash2 size={14} /> Smazat
                </button>
              )}
            </div>
          </div>

          {/* Meta row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-4 pt-4 border-t border-gray-100">
            <Meta label="Rok výstavby" value={structure.year_built ?? '—'} />
            <Meta label="Typ" value={typeName} />
            <Meta label="Inspekcí celkem" value={structure.inspection_count} />
            <Meta
              label="Poslední inspekce"
              value={structure.last_inspection_date
                ? new Date(structure.last_inspection_date).toLocaleDateString('cs-CZ')
                : '—'}
            />
          </div>

          {structure.description && (
            <p className="mt-4 text-sm text-gray-600">{structure.description}</p>
          )}

          {/* Materials section */}
          <div className="mt-4 pt-4 border-t border-gray-100">
            {editingMaterials ? (
              <div>
                <MaterialsEditor value={draftMaterials} onChange={setDraftMaterials} />
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={() => updateMaterials.mutate(draftMaterials)}
                    disabled={updateMaterials.isPending}
                    className="btn-primary text-xs"
                  >
                    <Check size={13} /> {updateMaterials.isPending ? 'Ukládám…' : 'Uložit'}
                  </button>
                  <button onClick={() => setEditingMaterials(false)} className="btn-secondary text-xs">
                    <X size={13} /> Zrušit
                  </button>
                </div>
              </div>
            ) : (
              <div>
                <div className="flex items-center justify-between mb-2">
                  <p className="text-sm font-medium text-gray-600">Složení materiálů</p>
                  {canEdit && (
                    <button onClick={startEditMaterials} className="btn-ghost text-xs py-1">
                      <Pencil size={12} /> Upravit
                    </button>
                  )}
                </div>
                {structure.materials.length === 0 ? (
                  <p className="text-xs text-gray-400">Žádné materiály nebyly zadány.</p>
                ) : (
                  <MaterialsBar materials={structure.materials} />
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Degradation trends across inspections */}
      {structureId && (
        <div className="mb-6">
          <MeasurementTrends structureId={structureId} />
        </div>
      )}

      {/* Inspections */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">Inspekce a měření</h2>
        <div className="flex gap-2">
          {inspections.length >= 2 && (
            <button
              onClick={() => { setCompareMode(m => !m); setCompareSel([]) }}
              className={compareMode ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
            >
              <GitCompareArrows size={15} /> {compareMode ? 'Zrušit porovnání' : 'Porovnat'}
            </button>
          )}
          {canEdit && (
            <button onClick={() => setShowNewInsp(true)} className="btn-primary text-sm">
              <Plus size={15} /> Nová inspekce
            </button>
          )}
        </div>
      </div>

      {compareMode && (
        <div className="mb-4 bg-brand-50 border border-brand-200 rounded-lg px-4 py-2.5 flex items-center gap-3 text-sm">
          <GitCompareArrows size={16} className="text-brand-600 shrink-0" />
          <span className="text-brand-800 flex-1">
            Vyberte dvě inspekce k porovnání ({compareSel.length}/2)
          </span>
          <button
            disabled={compareSel.length !== 2}
            onClick={() => {
              // A = starší, B = novější (podle data inspekce)
              const byDate = [...compareSel].sort((x, y) => {
                const ix = inspections.find(i => i.id === x)!
                const iy = inspections.find(i => i.id === y)!
                return ix.inspection_date.localeCompare(iy.inspection_date)
              })
              navigate(`/structures/${structureId}/compare?a=${byDate[0]}&b=${byDate[1]}`)
            }}
            className="btn-primary text-xs disabled:opacity-50"
          >
            Porovnat vybrané
          </button>
        </div>
      )}

      {/* Inspection filters */}
      {inspections.length > 1 && (
        <div className="flex gap-2 mb-4 flex-wrap items-center">
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              className="input pl-8 text-sm py-1.5"
              placeholder="Název, inspektor, poznámka…"
              value={inspSearch}
              onChange={e => setInspSearch(e.target.value)}
            />
          </div>
          <select className="input w-auto text-sm py-1.5" value={inspStatus} onChange={e => setInspStatus(e.target.value)}>
            <option value="">Každý stav</option>
            <option value="draft">Koncept</option>
            <option value="in_progress">Probíhá</option>
            <option value="completed">Dokončená</option>
            <option value="archived">Archivovaná</option>
          </select>
          <select className="input w-auto text-sm py-1.5" value={inspMethod} onChange={e => setInspMethod(e.target.value)}>
            <option value="">Každá metoda</option>
            <option value="drone">Dron</option>
            <option value="mobile">Mobilní</option>
            <option value="manual">Manuální</option>
            <option value="combined">Kombinovaná</option>
          </select>
          <div className="flex items-center gap-1 text-xs text-gray-400">
            <input className="input w-auto text-sm py-1.5" type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)} title="Od data" />
            –
            <input className="input w-auto text-sm py-1.5" type="date" value={dateTo} onChange={e => setDateTo(e.target.value)} title="Do data" />
          </div>
          {filtersActive && (
            <button
              onClick={() => { setInspSearch(''); setInspStatus(''); setInspMethod(''); setDateFrom(''); setDateTo('') }}
              className="btn-ghost text-xs text-gray-500 py-1"
            >
              <X size={12} /> Zrušit filtry ({filteredInspections.length} z {inspections.length})
            </button>
          )}
        </div>
      )}

      {inspections.length === 0 ? (
        <div className="card p-10 text-center">
          <ClipboardList size={36} className="mx-auto text-gray-300 mb-3" />
          <p className="text-gray-500 mb-4">Žádné inspekce{canEdit ? '. Přidejte první měření.' : ' zatím nebyly přidány.'}</p>
          {canEdit && (
            <button onClick={() => setShowNewInsp(true)} className="btn-primary">
              <Plus size={15} /> Přidat inspekci
            </button>
          )}
        </div>
      ) : filteredInspections.length === 0 ? (
        <div className="card p-8 text-center text-sm text-gray-400">
          Žádná inspekce neodpovídá filtrům.
        </div>
      ) : (
        <div className="space-y-3">
          {filteredInspections.map(insp => (
            <div
              key={insp.id}
              role="button"
              tabIndex={0}
              onClick={() => compareMode
                ? toggleCompareSel(insp.id)
                : navigate(`/structures/${structureId}/inspections/${insp.id}`)}
              className={`card p-4 w-full text-left transition-all group flex items-center gap-4 cursor-pointer
                ${compareMode && compareSel.includes(insp.id)
                  ? 'border-brand-400 ring-2 ring-brand-200 shadow-md'
                  : 'hover:shadow-md hover:border-brand-200'}`}
            >
              {compareMode && (
                <div className={`w-5 h-5 rounded border-2 shrink-0 flex items-center justify-center
                  ${compareSel.includes(insp.id) ? 'bg-brand-500 border-brand-500' : 'border-gray-300'}`}>
                  {compareSel.includes(insp.id) && <Check size={13} className="text-white" />}
                </div>
              )}
              <div className="p-2.5 bg-brand-50 rounded-lg">
                <Wrench size={18} className="text-brand-600" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <p className="font-medium text-gray-900 truncate group-hover:text-brand-600">{insp.name}</p>
                  {structure?.reference_inspection_id === insp.id && (
                    <span className="badge bg-amber-100 text-amber-700 text-[10px] flex items-center gap-1 shrink-0" title="Referenční model konstrukce">
                      <Crosshair size={10} /> Reference
                    </span>
                  )}
                  <StatusBadge status={insp.status} />
                </div>
                <div className="flex items-center gap-3 text-xs text-gray-500">
                  <span className="flex items-center gap-1">
                    <Calendar size={11} />
                    {new Date(insp.inspection_date).toLocaleDateString('cs-CZ')}
                  </span>
                  {insp.method && <span>{METHOD_LABELS[insp.method] ?? insp.method}</span>}
                  {insp.inspector_name && <span>{insp.inspector_name}</span>}
                </div>
                <div className="flex items-center gap-3 text-xs text-gray-400 mt-0.5">
                  <span>{insp.annotation_count} poruch</span>
                  <span>{insp.measurement_count} měření</span>
                  {insp.photo_set && (
                    <span className={`font-medium ${insp.photo_set.status === 'completed' ? 'text-green-600' : insp.photo_set.status === 'failed' ? 'text-red-500' : 'text-yellow-600'}`}>
                      3D: {insp.photo_set.status === 'completed' ? 'Hotovo' : insp.photo_set.status === 'failed' ? 'Chyba' : `${insp.photo_set.progress}%`}
                    </span>
                  )}
                </div>
              </div>
              {canEdit && !compareMode && (
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    if (confirm(`Smazat inspekci „${insp.name}" a všechna její data (model, poruchy, měření)? Tuto akci nelze vrátit.`)) {
                      deleteInspection.mutate(insp.id)
                    }
                  }}
                  disabled={deleteInspection.isPending}
                  className="btn-ghost text-gray-400 hover:text-red-500 p-1.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                  title="Smazat inspekci"
                >
                  <Trash2 size={16} />
                </button>
              )}
              <ChevronRight size={18} className="text-gray-400 group-hover:text-brand-500 shrink-0" />
            </div>
          ))}
        </div>
      )}

      {showNewInsp && (
        <NewInspectionModal structureId={structureId!} onClose={() => setShowNewInsp(false)} />
      )}

      {showEdit && structure && (
        <EditStructureModal structure={structure} onClose={() => setShowEdit(false)} />
      )}
    </div>
  )
}

// ─── Materials bar ────────────────────────────────────────────────────────────

function MaterialsBar({ materials }: { materials: StructureMaterial[] }) {
  const total = materials.reduce((s, m) => s + m.percentage, 0)
  return (
    <div>
      {/* Bar */}
      <div className="flex h-3 rounded-full overflow-hidden gap-px mb-2">
        {materials.map((m, i) => (
          <div
            key={m.id}
            title={`${m.material_name}: ${m.percentage}%`}
            style={{ width: `${m.percentage}%`, background: m.color }}
          />
        ))}
        {total < 100 && (
          <div className="flex-1 bg-gray-100" title="Nespecifikováno" />
        )}
      </div>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {materials.map(m => (
          <div key={m.id} className="flex items-center gap-1.5 text-xs text-gray-600">
            <div className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: m.color }} />
            <span>{m.material_name}</span>
            <span className="font-semibold text-gray-800">{m.percentage}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function Meta({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <p className="text-xs text-gray-400">{label}</p>
      <p className="text-sm font-medium text-gray-800">{value}</p>
    </div>
  )
}

function StatusBadge({ status }: { status: InspectionStatus }) {
  const cls: Record<InspectionStatus, string> = {
    draft: 'bg-gray-100 text-gray-600',
    in_progress: 'bg-blue-100 text-blue-700',
    completed: 'bg-green-100 text-green-700',
    archived: 'bg-slate-100 text-slate-500',
  }
  return <span className={`badge ${cls[status]}`}>{STATUS_LABELS[status]}</span>
}

function NewInspectionModal({ structureId, onClose }: { structureId: string; onClose: () => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [form, setForm] = useState({
    name: '', inspection_date: new Date().toISOString().split('T')[0],
    method: 'drone' as InspectionMethod, inspector_name: '',
    weather: '', description: '',
  })

  const mut = useMutation({
    mutationFn: () => inspectionsApi.create(structureId, form),
    onSuccess: (insp) => {
      qc.invalidateQueries({ queryKey: ['inspections', structureId] })
      qc.invalidateQueries({ queryKey: ['structures', structureId] })
      onClose()
      navigate(`/structures/${structureId}/inspections/${insp.id}`)
    },
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="card w-full max-w-lg mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold">Nová inspekce</h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>
        <div className="space-y-4">
          <div>
            <label className="label">Název inspekce *</label>
            <input className="input" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="Roční inspekce 2025" autoFocus />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Datum *</label>
              <input className="input" type="date" value={form.inspection_date} onChange={e => setForm(f => ({ ...f, inspection_date: e.target.value }))} />
            </div>
            <div>
              <label className="label">Metoda</label>
              <select className="input" value={form.method} onChange={e => setForm(f => ({ ...f, method: e.target.value as InspectionMethod }))}>
                <option value="drone">Dron</option>
                <option value="mobile">Mobilní zařízení</option>
                <option value="manual">Manuální</option>
                <option value="combined">Kombinovaná</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Inspektor</label>
              <input className="input" value={form.inspector_name} onChange={e => setForm(f => ({ ...f, inspector_name: e.target.value }))} placeholder="Jméno inspektora" />
            </div>
            <div>
              <label className="label">Počasí</label>
              <input className="input" value={form.weather} onChange={e => setForm(f => ({ ...f, weather: e.target.value }))} placeholder="Jasno, 18 °C" />
            </div>
          </div>
          <div>
            <label className="label">Poznámky</label>
            <textarea className="input h-16 resize-none" value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-6">
          <button onClick={onClose} className="btn-secondary">Zrušit</button>
          <button onClick={() => mut.mutate()} disabled={!form.name || mut.isPending} className="btn-primary disabled:opacity-50">
            {mut.isPending ? 'Ukládám…' : 'Vytvořit inspekci'}
          </button>
        </div>
      </div>
    </div>
  )
}
