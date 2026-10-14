import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, ChevronDown, ChevronUp, Wrench, User, CalendarClock, Coins } from 'lucide-react'
import { annotationsApi } from '../api/inspections'
import {
  DEFECT_LABELS, SEVERITY_LABELS, SEVERITY_BG, SEVERITY_COLORS,
  DEFECT_STATUS_LABELS, DEFECT_STATUS_BG,
  type Severity, type DefectStatus,
} from '../types'
import { useViewerStore } from '../store/viewerStore'
import { useAuthStore } from '../store/authStore'
import type { Annotation, Vec3 } from '../types'
import AnnotationForm from './AnnotationForm'
import DetailPhotos from './DetailPhotos'
import { isPolygon } from './Viewer3D/AnnotationPolygons'

interface Props { inspectionId: string; scaleFactor?: number }

// Fly-to target for an annotation: a point defect focuses on its marker; a
// polygon focuses on its centroid, framed to its extent.
function annotationFocus(a: Annotation): { point: Vec3; radius: number; normal?: Vec3 } | null {
  if (isPolygon(a.geometry_json)) {
    const pts = a.geometry_json as Vec3[]
    const n = pts.length
    const c = pts.reduce((s, p) => ({ x: s.x + p.x, y: s.y + p.y, z: s.z + p.z }), { x: 0, y: 0, z: 0 })
    c.x /= n; c.y /= n; c.z /= n
    let r = 0
    for (const p of pts) r = Math.max(r, Math.hypot(p.x - c.x, p.y - c.y, p.z - c.z))
    // Newell's method → plane normal, so the camera can face the polygon head-on
    let nx = 0, ny = 0, nz = 0
    for (let i = 0; i < n; i++) {
      const p = pts[i], q = pts[(i + 1) % n]
      nx += (p.y - q.y) * (p.z + q.z)
      ny += (p.z - q.z) * (p.x + q.x)
      nz += (p.x - q.x) * (p.y + q.y)
    }
    const len = Math.hypot(nx, ny, nz) || 1
    return { point: c, radius: r * 1.3, normal: { x: nx / len, y: ny / len, z: nz / len } }
  }
  if (a.position_x != null && a.position_y != null && a.position_z != null) {
    return { point: { x: a.position_x, y: a.position_y, z: a.position_z }, radius: 0.22 }
  }
  return null
}

export default function AnnotationPanel({ inspectionId, scaleFactor = 1 }: Props) {
  const qc = useQueryClient()
  const canEdit = useAuthStore(s => s.canEdit())
  const [editing, setEditing] = useState<Annotation | null>(null)
  const { setHoveredAnnotationId, focusOn } = useViewerStore()
  useEffect(() => () => setHoveredAnnotationId(null), [setHoveredAnnotationId])
  const {
    selectedAnnotation,
    setSelectedAnnotation,
    annotationFormOpen,
    setAnnotationFormOpen,
    pendingPosition,
    pendingPolygon,
    setPendingPolygon,
  } = useViewerStore()

  const { data: annotations = [], isLoading } = useQuery({
    queryKey: ['annotations', inspectionId],
    queryFn: () => annotationsApi.list(inspectionId),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => annotationsApi.delete(inspectionId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['annotations', inspectionId] }),
  })

  const [filterSeverity, setFilterSeverity] = useState<Severity | 'all'>('all')
  const [filterStatus, setFilterStatus] = useState<DefectStatus | 'all' | 'ai_new'>('all')

  const counts = annotations.reduce((acc, a) => {
    acc[a.severity] = (acc[a.severity] || 0) + 1
    return acc
  }, {} as Record<Severity, number>)

  const filtered = useMemo(() => annotations.filter(a =>
    (filterSeverity === 'all' || a.severity === filterSeverity) &&
    (filterStatus === 'all'
      || (filterStatus === 'ai_new' ? a.ai_detected && (a.status ?? 'new') === 'new'
        : (a.status ?? 'new') === filterStatus)),
  ), [annotations, filterSeverity, filterStatus])

  // Total estimated remediation cost across the currently shown defects.
  const totalCost = filtered.reduce((s, a) => s + (a.cost_estimate ?? 0), 0)

  const handleFormClose = () => {
    setAnnotationFormOpen(false)
    useViewerStore.getState().setPendingPosition(null)
    setPendingPolygon(null)
  }

  return (
    <div className="p-3">
      {/* Summary badges */}
      <div className="flex flex-wrap gap-1.5 mb-3">
        {(['critical', 'high', 'medium', 'low'] as Severity[]).map(s =>
          counts[s] ? (
            <span key={s} className={`badge ${SEVERITY_BG[s]}`}>
              {SEVERITY_LABELS[s]}: {counts[s]}
            </span>
          ) : null,
        )}
        {annotations.length === 0 && (
          <span className="text-xs text-gray-400">Žádné anotace</span>
        )}
      </div>

      {/* Filters */}
      {annotations.length > 0 && (
        <div className="flex gap-2 mb-3">
          <select
            className="input text-xs py-1 flex-1"
            value={filterSeverity}
            onChange={e => setFilterSeverity(e.target.value as Severity | 'all')}
          >
            <option value="all">Všechny závažnosti</option>
            {(['critical', 'high', 'medium', 'low'] as Severity[]).map(s => (
              <option key={s} value={s}>{SEVERITY_LABELS[s]}</option>
            ))}
          </select>
          <select
            className="input text-xs py-1 flex-1"
            value={filterStatus}
            onChange={e => setFilterStatus(e.target.value as DefectStatus | 'all' | 'ai_new')}
          >
            <option value="all">Všechny stavy</option>
            <option value="ai_new">AI návrhy k ověření ({annotations.filter(a => a.ai_detected && (a.status ?? 'new') === 'new').length})</option>
            {(['new', 'monitoring', 'repaired', 'dismissed'] as DefectStatus[]).map(s => (
              <option key={s} value={s}>{DEFECT_STATUS_LABELS[s]}</option>
            ))}
          </select>
        </div>
      )}

      {totalCost > 0 && (
        <div className="mb-3 text-xs text-gray-600 bg-gray-50 rounded px-2.5 py-1.5 flex items-center gap-1.5">
          <Coins size={13} className="text-amber-500" />
          Odhad nákladů{filterSeverity !== 'all' || filterStatus !== 'all' ? ' (filtr)' : ''}:
          <span className="font-semibold">{totalCost.toLocaleString('cs-CZ')} Kč</span>
        </div>
      )}

      {/* Add button */}
      {canEdit && (
        <button
          onClick={() => setAnnotationFormOpen(true)}
          className="btn-primary w-full text-sm mb-3"
        >
          <Plus size={14} /> Přidat poruchu
        </button>
      )}

      {/* List */}
      {isLoading ? (
        <p className="text-xs text-gray-400 text-center py-4">Načítám…</p>
      ) : (
        <div className="space-y-2">
          {filtered.length === 0 && (
            <p className="text-xs text-gray-400 text-center py-4">Žádná porucha neodpovídá filtru.</p>
          )}
          {filtered.map(a => (
            <AnnotationCard
              key={a.id}
              annotation={a}
              inspectionId={inspectionId}
              selected={selectedAnnotation?.id === a.id}
              onSelect={() => {
                const select = selectedAnnotation?.id !== a.id
                setSelectedAnnotation(select ? a : null)
                const f = annotationFocus(a)
                if (select && f) focusOn(f.point, f.radius, true, f.normal ?? null)
              }}
              onHover={(v) => setHoveredAnnotationId(v ? a.id : null)}
              onEdit={canEdit ? () => setEditing(a) : undefined}
              onDelete={canEdit ? () => {
                if (confirm('Smazat anotaci?')) deleteMut.mutate(a.id)
              } : undefined}
            />
          ))}
        </div>
      )}

      {/* Create form modal */}
      {annotationFormOpen && (
        <AnnotationForm
          inspectionId={inspectionId}
          scaleFactor={scaleFactor}
          pendingPosition={pendingPosition}
          pendingPolygon={pendingPolygon}
          onClose={handleFormClose}
        />
      )}

      {/* Edit form modal */}
      {editing && (
        <AnnotationForm
          inspectionId={inspectionId}
          scaleFactor={scaleFactor}
          annotation={editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}

function AnnotationCard({ annotation: a, inspectionId, selected, onSelect, onHover, onEdit, onDelete }: {
  annotation: Annotation
  inspectionId: string
  selected: boolean
  onSelect: () => void
  onHover: (v: boolean) => void
  onEdit?: () => void
  onDelete?: () => void
}) {
  const qc = useQueryClient()
  const dot = SEVERITY_COLORS[a.severity]
  const isPoly = isPolygon(a.geometry_json)
  const status = (a.status ?? 'new') as DefectStatus

  const statusMut = useMutation({
    mutationFn: (s: DefectStatus) => annotationsApi.update(inspectionId, a.id, { status: s }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['annotations', inspectionId] }),
  })

  return (
    <div
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
      className={`rounded-lg border transition-all cursor-pointer
        ${selected ? 'border-brand-300 bg-brand-50' : 'border-gray-200 bg-white hover:border-gray-300'}`}
    >
      <button onClick={onSelect} className="w-full text-left p-2.5">
        <div className="flex items-start gap-2">
          <div className="mt-1 shrink-0 w-2.5 h-2.5 rounded-full" style={{ background: dot }} />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-gray-800 truncate">
              {DEFECT_LABELS[a.defect_type] ?? a.defect_type}
            </p>
            <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
              <span className={`badge text-[10px] ${SEVERITY_BG[a.severity]}`}>
                {SEVERITY_LABELS[a.severity]}
              </span>
              <span className={`badge text-[10px] ${DEFECT_STATUS_BG[status]}`}>
                {DEFECT_STATUS_LABELS[status]}
              </span>
              {isPoly && (
                <span className="badge bg-amber-100 text-amber-700 text-[10px]">
                  Polygon {(a.geometry_json as Vec3[]).length}b
                </span>
              )}
              {a.ai_detected && (
                <span className="badge bg-purple-100 text-purple-700 text-[10px]">AI</span>
              )}
            </div>
          </div>
          <span className="text-gray-400 shrink-0">
            {selected ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </span>
        </div>
      </button>

      {selected && (
        <div className="px-2.5 pb-2.5 pt-0 border-t border-gray-100">
          {a.description && (
            <p className="text-xs text-gray-600 mb-2 mt-2">{a.description}</p>
          )}
          {a.ai_detected && status === 'new' && onEdit && (
            <div className="flex gap-2 mb-2">
              <button className="btn-primary text-xs py-1 flex-1" disabled={statusMut.isPending}
                onClick={() => statusMut.mutate('monitoring')} title="Porucha je skutečná — převést na sledovanou">
                Potvrdit
              </button>
              <button className="btn-secondary text-xs py-1 flex-1" disabled={statusMut.isPending}
                onClick={() => statusMut.mutate('dismissed')} title="AI se spletla — zamítnout">
                Zamítnout
              </button>
            </div>
          )}
          <div className="grid grid-cols-2 gap-1 text-xs text-gray-500 mb-2">
            {a.length_m != null && <span>Délka: {a.length_m.toFixed(2)} m</span>}
            {a.area_m2 != null && <span>Plocha: {a.area_m2.toFixed(2)} m²</span>}
            {isPoly ? (
              <span className="col-span-2 text-[10px] text-gray-400">
                {(a.geometry_json as Vec3[]).length} vrcholů polygonu
              </span>
            ) : a.position_x != null && (
              <span className="col-span-2 font-mono text-[10px] text-gray-400">
                [{a.position_x.toFixed(2)}, {a.position_y?.toFixed(2)}, {a.position_z?.toFixed(2)}]
              </span>
            )}
          </div>
          {/* Lifecycle / remediation */}
          {(a.recommended_action || a.responsible || a.due_date || a.cost_estimate != null) && (
            <div className="mb-2 space-y-1 text-xs">
              {a.recommended_action && (
                <div className="flex gap-1.5 text-gray-600">
                  <Wrench size={12} className="mt-0.5 shrink-0 text-gray-400" />
                  <span>{a.recommended_action}</span>
                </div>
              )}
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-gray-500">
                {a.responsible && (
                  <span className="flex items-center gap-1"><User size={11} /> {a.responsible}</span>
                )}
                {a.due_date && (
                  <span className="flex items-center gap-1"><CalendarClock size={11} /> {a.due_date}</span>
                )}
                {a.cost_estimate != null && (
                  <span className="flex items-center gap-1"><Coins size={11} /> {a.cost_estimate.toLocaleString('cs-CZ')} Kč</span>
                )}
              </div>
            </div>
          )}

          {/* Quick status change */}
          {onEdit && (
            <div className="mb-2">
              <label className="text-[10px] text-gray-400 block mb-0.5">Změnit stav</label>
              <select
                className="input text-xs py-1"
                value={status}
                disabled={statusMut.isPending}
                onChange={e => statusMut.mutate(e.target.value as DefectStatus)}
              >
                {(['new', 'monitoring', 'repaired', 'dismissed'] as DefectStatus[]).map(s => (
                  <option key={s} value={s}>{DEFECT_STATUS_LABELS[s]}</option>
                ))}
              </select>
            </div>
          )}

          <DetailPhotos inspectionId={inspectionId} annotationId={a.id} reportPhoto={a.report_photo} />

          {(onEdit || onDelete) && (
            <div className="flex items-center gap-1 mt-2">
              {onEdit && (
                <button onClick={onEdit} className="btn-ghost text-brand-600 text-xs p-1">
                  <Pencil size={12} /> Upravit
                </button>
              )}
              {onDelete && (
                <button onClick={onDelete} className="btn-ghost text-red-500 text-xs p-1">
                  <Trash2 size={12} /> Smazat
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
