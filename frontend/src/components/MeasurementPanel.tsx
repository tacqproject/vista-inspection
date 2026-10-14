import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Ruler, Scale } from 'lucide-react'
import { measurementsApi, inspectionsApi } from '../api/inspections'
import { useViewerStore } from '../store/viewerStore'
import { useAuthStore } from '../store/authStore'
import type { Measurement } from '../types'

interface Props {
  inspectionId: string
  structureId: string
  scaleFactor?: number
}

export default function MeasurementPanel({ inspectionId, structureId, scaleFactor = 1 }: Props) {
  const qc = useQueryClient()
  const canEdit = useAuthStore(s => s.canEdit())
  const { measurePoints, clearMeasurePoints, setTool, setHoveredMeasurementId, focusOn } = useViewerStore()
  useEffect(() => () => setHoveredMeasurementId(null), [setHoveredMeasurementId])
  const [saveName, setSaveName] = useState('')
  const [saveNote, setSaveNote] = useState('')
  const [calibrateLen, setCalibrateLen] = useState('')

  const calibrateMut = useMutation({
    mutationFn: (factor: number) =>
      inspectionsApi.update(structureId, inspectionId, { scale_factor: factor }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
      clearMeasurePoints()
      setCalibrateLen('')
      setTool('view')
    },
  })

  const { data: measurements = [], isLoading } = useQuery({
    queryKey: ['measurements', inspectionId],
    queryFn: () => measurementsApi.list(inspectionId),
  })

  const createMut = useMutation({
    mutationFn: () => {
      const pts = measurePoints.map(p => p.position)
      return measurementsApi.create(inspectionId, {
        name: saveName || undefined,
        measurement_type: 'distance',
        points_json: pts,
        notes: saveNote || undefined,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['measurements', inspectionId] })
      clearMeasurePoints()
      setSaveName('')
      setSaveNote('')
      setTool('view')
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => measurementsApi.delete(inspectionId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['measurements', inspectionId] }),
  })

  // Raw distance in model units + scaled display value
  const rawDist = measurePoints.length === 2 ? (() => {
    const p1 = measurePoints[0].position
    const p2 = measurePoints[1].position
    const dx = p2.x - p1.x, dy = p2.y - p1.y, dz = p2.z - p1.z
    return Math.sqrt(dx*dx + dy*dy + dz*dz)
  })() : null
  const liveDist = rawDist != null ? (rawDist * scaleFactor).toFixed(3) : null
  const liveDelta = measurePoints.length === 2 ? (() => {
    const p1 = measurePoints[0].position, p2 = measurePoints[1].position
    return {
      x: (Math.abs(p2.x - p1.x) * scaleFactor).toFixed(2),
      y: (Math.abs(p2.y - p1.y) * scaleFactor).toFixed(2),
      z: (Math.abs(p2.z - p1.z) * scaleFactor).toFixed(2),
    }
  })() : null

  const handleCalibrate = () => {
    const real = parseFloat(calibrateLen.replace(',', '.'))
    if (!rawDist || !real || real <= 0) return
    calibrateMut.mutate(real / rawDist)
  }

  return (
    <div className="p-3">
      {/* Active measure prompt */}
      {measurePoints.length > 0 && (
        <div className="mb-3 bg-sky-50 border border-sky-200 rounded-lg p-3">
          <p className="text-xs font-semibold text-sky-700 mb-1">
            {measurePoints.length === 1
              ? 'Klikněte na 2. bod na modelu'
              : `Vzdálenost: ${liveDist} m`}
          </p>
          {liveDelta && (
            <p className="text-[11px] font-mono">
              <span className="text-red-500">X {liveDelta.x}</span>{' '}
              <span className="text-green-600">Y {liveDelta.y}</span>{' '}
              <span className="text-blue-500">Z {liveDelta.z}</span> m
            </p>
          )}
          {liveDist && (
            <div className="space-y-2 mt-2">
              {canEdit && (
                <input
                  className="input text-xs"
                  placeholder="Název měření (volitelné)"
                  value={saveName}
                  onChange={e => setSaveName(e.target.value)}
                />
              )}
              <div className="flex gap-2">
                {canEdit && (
                  <button
                    onClick={() => createMut.mutate()}
                    disabled={createMut.isPending}
                    className="btn-primary text-xs flex-1"
                  >
                    Uložit
                  </button>
                )}
                <button
                  onClick={() => { clearMeasurePoints(); setTool('view') }}
                  className="btn-secondary text-xs flex-1"
                >
                  {canEdit ? 'Zrušit' : 'Hotovo'}
                </button>
              </div>

              {/* Scale calibration: enter the real-world length of this distance */}
              {canEdit && (
                <div className="border-t border-sky-200 pt-2 mt-1">
                  <p className="text-[11px] text-sky-600 mb-1.5 flex items-center gap-1">
                    <Scale size={11} /> Znáte skutečnou délku? Zkalibrujte měřítko modelu:
                  </p>
                  <div className="flex gap-2">
                    <input
                      className="input text-xs flex-1"
                      placeholder="Skutečná délka v metrech (např. 2.0)"
                      inputMode="decimal"
                      value={calibrateLen}
                      onChange={e => setCalibrateLen(e.target.value)}
                    />
                    <button
                      onClick={handleCalibrate}
                      disabled={calibrateMut.isPending || !parseFloat(calibrateLen.replace(',', '.'))}
                      className="btn-secondary text-xs whitespace-nowrap"
                    >
                      {calibrateMut.isPending ? '…' : 'Kalibrovat'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {measurePoints.length === 0 && (
        <button
          onClick={() => setTool('measure')}
          className="btn-primary w-full text-sm mb-3"
        >
          <Ruler size={14} /> Nové měření
        </button>
      )}

      {isLoading ? (
        <p className="text-xs text-gray-400 text-center py-4">Načítám…</p>
      ) : measurements.length === 0 ? (
        <div className="text-center py-6">
          <Ruler size={28} className="mx-auto text-gray-300 mb-2" />
          <p className="text-xs text-gray-400">Žádná měření</p>
        </div>
      ) : (
        <div className="space-y-2">
          {measurements.map(m => (
            <MeasurementCard
              key={m.id}
              measurement={m}
              scaleFactor={scaleFactor}
              canEdit={canEdit}
              onHover={(v) => setHoveredMeasurementId(v ? m.id : null)}
              onFocus={() => {
                const pts = m.points_json as any[] | undefined
                if (!Array.isArray(pts) || pts.length < 2) return   // photo measurement — no 3D place
                const [a, b] = pts
                const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, z: (a.z + b.z) / 2 }
                const half = Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z) / 2
                focusOn(mid, half * 1.25 + 0.1)
              }}
              onDelete={() => { if (confirm('Smazat měření?')) deleteMut.mutate(m.id) }}
              onCalibrate={(factor) => calibrateMut.mutate(factor)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function MeasurementCard({ measurement: m, scaleFactor, canEdit, onHover, onFocus, onDelete, onCalibrate }: {
  measurement: Measurement
  scaleFactor: number
  canEdit: boolean
  onHover: (v: boolean) => void
  onFocus: () => void
  onDelete: () => void
  onCalibrate: (factor: number) => void
}) {
  // only 3D distance measurements live on the model — photo measurements are
  // in-image and have no place to fly to
  const is3D = m.measurement_type !== 'photo' && Array.isArray(m.points_json) && m.points_json.length >= 2

  const handleCalibrate = () => {
    if (m.value == null || m.value === 0) return
    const input = prompt('Zadejte skutečnou délku tohoto měření v metrech:')
    if (!input) return
    const real = parseFloat(input.replace(',', '.'))
    if (!real || real <= 0) return
    onCalibrate(real / m.value)
  }

  return (
    <div
      onMouseEnter={is3D ? () => onHover(true) : undefined}
      onMouseLeave={is3D ? () => onHover(false) : undefined}
      className="bg-white border border-gray-200 rounded-lg p-3 flex items-start gap-2"
    >
      <Ruler size={15} className="text-sky-500 shrink-0 mt-0.5" />
      <div
        className={`flex-1 min-w-0 ${is3D ? 'cursor-pointer' : ''}`}
        onClick={is3D ? onFocus : undefined}
        title={is3D ? 'Přiblížit k měření v modelu' : undefined}
      >
        <p className="text-sm font-medium text-gray-800">
          {m.name ?? `Měření ${m.id.slice(0, 6)}`}
        </p>
        {m.value != null && (
          <p className="text-xs text-sky-600 font-semibold">
            {/* photo measurements are absolute (calibrated in-image); only 3D distances scale */}
            {m.measurement_type === 'photo'
              ? `${m.value.toFixed(2)} ${m.unit}`
              : `${(m.value * scaleFactor).toFixed(3)} ${m.unit}`}
          </p>
        )}
        {m.measurement_type !== 'photo' && Array.isArray(m.points_json) && m.points_json.length >= 2 && (() => {
          const [p1, p2] = m.points_json as any[]
          return (
            <p className="text-[10px] font-mono text-gray-500">
              <span className="text-red-500">X {(Math.abs(p2.x - p1.x) * scaleFactor).toFixed(2)}</span>{' '}
              <span className="text-green-600">Y {(Math.abs(p2.y - p1.y) * scaleFactor).toFixed(2)}</span>{' '}
              <span className="text-blue-500">Z {(Math.abs(p2.z - p1.z) * scaleFactor).toFixed(2)}</span> m
            </p>
          )
        })()}
        {m.notes && <p className="text-xs text-gray-500 truncate">{m.notes}</p>}
        <p className="text-[10px] text-gray-400 mt-0.5">
          {new Date(m.created_at).toLocaleDateString('cs-CZ')}
        </p>
      </div>
      {canEdit && (
        <>
          <button
            onClick={handleCalibrate}
            title="Kalibrovat měřítko podle tohoto měření"
            className="text-gray-400 hover:text-sky-500 p-0.5"
          >
            <Scale size={13} />
          </button>
          <button onClick={onDelete} className="text-gray-400 hover:text-red-500 p-0.5">
            <Trash2 size={13} />
          </button>
        </>
      )}
    </div>
  )
}
