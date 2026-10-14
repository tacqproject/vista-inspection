import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { annotationsApi, reportsApi } from '../api/inspections'
import { DEFECT_LABELS, SEVERITY_LABELS, DEFECT_STATUS_LABELS, type DefectType, type Severity, type DefectStatus } from '../types'
import type { Vec3, Annotation } from '../types'
import { isPolygon } from './Viewer3D/AnnotationPolygons'

interface Props {
  inspectionId: string
  scaleFactor?: number
  pendingPosition?: Vec3 | null
  pendingPolygon?: Vec3[] | null
  /** when set, the form edits this existing defect instead of creating one */
  annotation?: Annotation | null
  onClose: () => void
}

function centroidOf(pts: Vec3[]): Vec3 {
  const n = pts.length
  return {
    x: pts.reduce((s, p) => s + p.x, 0) / n,
    y: pts.reduce((s, p) => s + p.y, 0) / n,
    z: pts.reduce((s, p) => s + p.z, 0) / n,
  }
}

/**
 * Surface area of a (possibly non-planar) 3D polygon: fan-triangulate from the
 * centroid and sum the triangle areas — same fan the viewer fills. Result is in
 * the polygon's own (scene) units²; caller scales to m².
 */
function polygonArea3D(pts: Vec3[]): number {
  const c = centroidOf(pts)
  let area = 0
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length]
    const ux = a.x - c.x, uy = a.y - c.y, uz = a.z - c.z
    const vx = b.x - c.x, vy = b.y - c.y, vz = b.z - c.z
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx
    area += 0.5 * Math.hypot(cx, cy, cz)
  }
  return area
}

export default function AnnotationForm({ inspectionId, scaleFactor = 1, pendingPosition, pendingPolygon, annotation, onClose }: Props) {
  const qc = useQueryClient()
  const editing = annotation != null

  const isPolygonMode = editing
    ? isPolygon(annotation!.geometry_json)
    : pendingPolygon != null && pendingPolygon.length >= 3

  // For a polygon the area follows from its geometry — compute it automatically
  // (scene units² × scaleFactor² → m²) instead of asking the user.
  const polyPts = isPolygonMode
    ? (editing ? (annotation!.geometry_json as Vec3[]) : pendingPolygon!)
    : null
  const autoArea = polyPts ? polygonArea3D(polyPts) * scaleFactor * scaleFactor : null

  const [form, setForm] = useState({
    defect_type: (annotation?.defect_type ?? 'crack') as DefectType,
    severity: (annotation?.severity ?? 'medium') as Severity,
    description: annotation?.description ?? '',
    length_m: annotation?.length_m != null ? String(annotation.length_m) : '',
    area_m2: annotation?.area_m2 != null ? String(annotation.area_m2) : '',
    status: (annotation?.status ?? 'new') as DefectStatus,
    recommended_action: annotation?.recommended_action ?? '',
    responsible: annotation?.responsible ?? '',
    due_date: annotation?.due_date ?? '',
    cost_estimate: annotation?.cost_estimate != null ? String(annotation.cost_estimate) : '',
    bridge_part: annotation?.bridge_part ?? '',
    action_urgency: annotation?.action_urgency ?? '',
  })

  // BMS catalogue (bridge parts / measure urgencies) — offered as suggestions,
  // free text still allowed (the protocol prints exactly what is stored)
  const { data: bms } = useQuery({
    queryKey: ['bms-catalogue'],
    queryFn: reportsApi.bmsCatalogue,
    staleTime: Infinity,
  })

  const mut = useMutation({
    mutationFn: () => {
      const payload = {
        defect_type: form.defect_type,
        severity: form.severity,
        description: form.description,
        length_m: form.length_m ? parseFloat(form.length_m) : null,
        // polygon area is derived from geometry; point defects keep the manual value
        area_m2: isPolygonMode ? autoArea : (form.area_m2 ? parseFloat(form.area_m2) : null),
        status: form.status,
        recommended_action: form.recommended_action || null,
        responsible: form.responsible || null,
        due_date: form.due_date || null,
        cost_estimate: form.cost_estimate ? parseFloat(form.cost_estimate) : null,
        bridge_part: form.bridge_part.trim() || null,
        action_urgency: form.action_urgency.trim() || null,
      }
      if (editing) {
        // metadata only — position/geometry stay put
        return annotationsApi.update(inspectionId, annotation!.id, payload)
      }
      const pos = isPolygonMode ? centroidOf(pendingPolygon!) : pendingPosition
      return annotationsApi.create(inspectionId, {
        ...payload,
        position_x: pos?.x,
        position_y: pos?.y,
        position_z: pos?.z,
        geometry_json: isPolygonMode ? pendingPolygon : undefined,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['annotations', inspectionId] })
      onClose()
    },
  })

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="card w-full max-w-md mx-4 p-5 max-h-[92vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-gray-900">{editing ? 'Upravit poruchu' : 'Nová porucha'}</h3>
          <button onClick={onClose} className="btn-ghost p-1"><X size={16} /></button>
        </div>

        {/* Position / polygon info */}
        {isPolygonMode ? (
          <div className="mb-3 text-xs bg-amber-50 border border-amber-200 rounded px-3 py-2 text-amber-800">
            <span className="font-medium">Polygon</span> · {(editing ? (annotation!.geometry_json as Vec3[]) : pendingPolygon!).length} bodů
            {editing && <span className="text-amber-600"> · poloha se nemění</span>}
          </div>
        ) : (editing ? annotation!.position_x != null : pendingPosition) && (
          <div className="mb-3 text-xs text-gray-400 font-mono bg-gray-50 rounded px-2 py-1">
            {(() => {
              const p = editing
                ? { x: annotation!.position_x!, y: annotation!.position_y!, z: annotation!.position_z! }
                : pendingPosition!
              return `Pozice: [${p.x.toFixed(3)}, ${p.y.toFixed(3)}, ${p.z.toFixed(3)}]`
            })()}
          </div>
        )}

        <div className="space-y-3">
          <div>
            <label className="label">Typ poruchy *</label>
            <select className="input" value={form.defect_type} onChange={e => set('defect_type', e.target.value)}>
              {Object.entries(DEFECT_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="label">Závažnost *</label>
            <div className="grid grid-cols-4 gap-2">
              {(['low', 'medium', 'high', 'critical'] as Severity[]).map(s => {
                const cls = {
                  low: 'border-green-400 bg-green-50 text-green-800',
                  medium: 'border-yellow-400 bg-yellow-50 text-yellow-800',
                  high: 'border-orange-400 bg-orange-50 text-orange-800',
                  critical: 'border-red-500 bg-red-50 text-red-800',
                }[s]
                return (
                  <button
                    key={s}
                    type="button"
                    onClick={() => set('severity', s)}
                    className={`text-xs py-1.5 rounded-lg border-2 font-medium transition-all
                      ${form.severity === s ? cls : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'}`}
                  >
                    {SEVERITY_LABELS[s]}
                  </button>
                )
              })}
            </div>
          </div>

          <div>
            <label className="label">Část mostu (BMS)</label>
            <input
              className="input"
              list="bms-parts"
              value={form.bridge_part}
              onChange={e => set('bridge_part', e.target.value)}
              placeholder="např. 2.3 Mostní závěry"
            />
            <datalist id="bms-parts">
              {bms?.parts.map(p => <option key={p.code} value={p.label} />)}
            </datalist>
          </div>

          <div>
            <label className="label">Popis</label>
            <textarea
              className="input h-16 resize-none"
              value={form.description}
              onChange={e => set('description', e.target.value)}
              placeholder="Popis poruchy, umístění…"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">Délka (m)</label>
              <input
                className="input"
                type="number"
                step="0.01"
                value={form.length_m}
                onChange={e => set('length_m', e.target.value)}
                placeholder="0.00"
              />
            </div>
            <div>
              <label className="label">Plocha (m²)</label>
              {isPolygonMode ? (
                <>
                  <div className="input bg-slate-50 text-slate-700 flex items-center">
                    {autoArea != null ? autoArea.toFixed(autoArea < 1 ? 4 : 2) : '—'}
                  </div>
                  <p className="text-[10px] text-slate-400 mt-0.5">
                    Vypočteno z polygonu{scaleFactor === 1 ? ' (nezkalibrováno — zkalibrujte měřítko)' : ''}
                  </p>
                </>
              ) : (
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  value={form.area_m2}
                  onChange={e => set('area_m2', e.target.value)}
                  placeholder="0.00"
                />
              )}
            </div>
          </div>

          {/* ─── Lifecycle / workflow ─────────────────────────────────────── */}
          <div className="pt-2 border-t border-gray-100 space-y-3">
            <div>
              <label className="label">Stav řešení</label>
              <div className="grid grid-cols-4 gap-2">
                {(['new', 'monitoring', 'repaired', 'dismissed'] as DefectStatus[]).map(s => {
                  const cls = {
                    new: 'border-blue-400 bg-blue-50 text-blue-800',
                    monitoring: 'border-amber-400 bg-amber-50 text-amber-800',
                    repaired: 'border-green-400 bg-green-50 text-green-800',
                    dismissed: 'border-gray-400 bg-gray-50 text-gray-700',
                  }[s]
                  return (
                    <button
                      key={s}
                      type="button"
                      onClick={() => set('status', s)}
                      className={`text-xs py-1.5 rounded-lg border-2 font-medium transition-all
                        ${form.status === s ? cls : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'}`}
                    >
                      {DEFECT_STATUS_LABELS[s]}
                    </button>
                  )
                })}
              </div>
            </div>

            <div>
              <label className="label">Doporučené opatření</label>
              <textarea
                className="input h-14 resize-none"
                value={form.recommended_action}
                onChange={e => set('recommended_action', e.target.value)}
                placeholder="Např. injektáž trhliny, sanace, monitoring…"
              />
            </div>

            <div>
              <label className="label">Naléhavost opatření (BMS)</label>
              <input
                className="input"
                list="bms-urgencies"
                value={form.action_urgency}
                onChange={e => set('action_urgency', e.target.value)}
                placeholder="např. odstranění nutno do 1 roku"
              />
              <datalist id="bms-urgencies">
                {bms?.urgencies.map(u => <option key={u} value={u} />)}
              </datalist>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className="label">Odpovědná osoba</label>
                <input
                  className="input"
                  value={form.responsible}
                  onChange={e => set('responsible', e.target.value)}
                  placeholder="Jméno"
                />
              </div>
              <div>
                <label className="label">Termín</label>
                <input
                  className="input"
                  type="date"
                  value={form.due_date}
                  onChange={e => set('due_date', e.target.value)}
                />
              </div>
              <div>
                <label className="label">Odhad nákladů (Kč)</label>
                <input
                  className="input"
                  type="number"
                  step="100"
                  value={form.cost_estimate}
                  onChange={e => set('cost_estimate', e.target.value)}
                  placeholder="0"
                />
              </div>
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary">Zrušit</button>
          <button
            onClick={() => mut.mutate()}
            disabled={mut.isPending}
            className="btn-primary disabled:opacity-50"
          >
            {mut.isPending ? 'Ukládám…' : editing ? 'Uložit změny' : 'Uložit poruchu'}
          </button>
        </div>
      </div>
    </div>
  )
}
