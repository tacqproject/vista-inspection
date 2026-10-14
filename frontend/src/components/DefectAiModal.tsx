import { useEffect, useState } from 'react'
import { X, Sparkles, Check } from 'lucide-react'
import { annotationPhotosApi, annotationsApi, type DetailPhoto, type DefectAnalysis } from '../api/inspections'
import { SEVERITY_LABELS, DEFECT_LABELS, type DefectType } from '../types'
import { useQueryClient } from '@tanstack/react-query'

/**
 * Runs the heuristic CV crack detector on a detail photo and shows the
 * highlighted result. The inspector can accept the suggestion, which pre-fills
 * the defect's severity/type/description and flags it ai_detected.
 */
export default function DefectAiModal({ inspectionId, annotationId, photo, onClose }: {
  inspectionId: string
  annotationId: string
  photo: DetailPhoto
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [res, setRes] = useState<DefectAnalysis | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [applying, setApplying] = useState(false)

  useEffect(() => {
    let alive = true
    annotationPhotosApi.analyze(inspectionId, annotationId, photo.filename)
      .then(r => alive && setRes(r))
      .catch(() => alive && setErr('Analýza selhala.'))
    return () => { alive = false }
  }, [inspectionId, annotationId, photo.filename])

  const apply = async () => {
    if (!res) return
    setApplying(true)
    try {
      const desc = `Automatická detekce: ${res.count} trhlin(a), max. šířka ~${res.max_width_px}px, `
        + `celková délka ~${Math.round(res.total_length_px)}px.`
      await annotationsApi.update(inspectionId, annotationId, {
        defect_type: res.defect_type_guess as DefectType,
        severity: res.severity_guess,
        description: desc,
        ai_detected: true,
        ai_confidence: res.confidence,
      })
      qc.invalidateQueries({ queryKey: ['annotations', inspectionId] })
      onClose()
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="card w-full max-w-2xl mx-4 p-4 max-h-[95vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-gray-900 flex items-center gap-2">
            <Sparkles size={16} className="text-purple-600" /> AI detekce poruch
          </h3>
          <button onClick={onClose} className="btn-ghost p-1"><X size={16} /></button>
        </div>

        {err ? (
          <p className="text-sm text-red-600 py-8 text-center">{err}</p>
        ) : !res ? (
          <div className="py-12 text-center text-gray-400 text-sm">
            <div className="animate-spin rounded-full h-6 w-6 border-2 border-purple-500 border-t-transparent mx-auto mb-3" />
            Analyzuji snímek…
          </div>
        ) : (
          <>
            <div className="bg-slate-100 rounded-lg overflow-hidden flex items-center justify-center mb-3" style={{ maxHeight: '55vh' }}>
              <img src={res.overlay} alt="detekce" className="max-w-full" style={{ maxHeight: '55vh' }} />
            </div>

            {res.found ? (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-center text-xs mb-3">
                <Stat label="Nálezů" value={String(res.count)} />
                <Stat label="Max. šířka" value={`~${res.max_width_px} px`} />
                <Stat label="Navržená závažnost" value={SEVERITY_LABELS[res.severity_guess]} />
                <Stat label="Jistota" value={`${Math.round(res.confidence * 100)} %`} />
              </div>
            ) : (
              <p className="text-sm text-gray-500 text-center py-4">Žádné výrazné trhliny nebyly nalezeny.</p>
            )}

            <p className="text-[11px] text-amber-600 mb-3">
              Jde o heuristický odhad — vždy ověřte. Návrh: {DEFECT_LABELS[res.defect_type_guess as DefectType] ?? res.defect_type_guess}.
            </p>

            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="btn-secondary text-sm">Zavřít</button>
              {res.found && (
                <button onClick={apply} disabled={applying} className="btn-primary text-sm disabled:opacity-50">
                  <Check size={14} /> {applying ? 'Ukládám…' : 'Předvyplnit poruchu'}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-50 rounded-lg py-2">
      <p className="font-semibold text-gray-800">{value}</p>
      <p className="text-gray-400">{label}</p>
    </div>
  )
}
