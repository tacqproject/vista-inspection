import { useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { X, ScanSearch, RefreshCw } from 'lucide-react'
import { api } from '../api/client'

/**
 * Automatic AI defect inspection: every photo goes through the defect detector,
 * findings are placed in the 3D model (same defect from several photos merged,
 * measured in metres when the model scale is known) and appear as AI
 * annotations with status "Nová" — confirm or dismiss them in the defect list.
 * Runs by itself after each reconstruction; here it can be re-run.
 */
type Status = {
  status: string
  progress?: number
  error?: string
  n_defects?: number
  n_detections?: number
  metric?: boolean
  scale_note?: string | null
  by_type?: Record<string, number>
  overlays?: { photo: string; n: number; url: string }[]
}

const STATE_LABEL: Record<string, string> = {
  none: 'Zatím nespuštěno',
  detecting: 'Probíhá detekce na fotkách…',
  detected: 'Fotky prohledány, čeká se na 3D model',
  completed: 'Hotovo',
  failed: 'Selhalo',
}

export default function AiDefectsModal({ inspectionId, hasModel, onClose }: {
  inspectionId: string; hasModel: boolean; onClose: () => void
}) {
  const qc = useQueryClient()
  const { data } = useQuery<Status>({
    queryKey: ['ai-defects', inspectionId],
    queryFn: () => api.get(`/inspections/${inspectionId}/ai-defects`).then(r => r.data),
    refetchInterval: q => (q.state.data?.status === 'detecting' ? 3000 : false),
  })
  const run = useMutation({
    mutationFn: (redetect: boolean) =>
      api.post(`/inspections/${inspectionId}/ai-defects`, null, { params: { redetect } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ai-defects', inspectionId] })
    },
  })
  // refresh the defect list once a run completes
  useEffect(() => {
    if (data?.status === 'completed') qc.invalidateQueries({ queryKey: ['annotations', inspectionId] })
  }, [data?.status, data?.n_defects, inspectionId, qc])

  const busy = data?.status === 'detecting' || run.isPending
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={onClose}>
      <div className="card w-full max-w-4xl mx-4 p-6 max-h-[92vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold flex items-center gap-2"><ScanSearch size={18} /> AI inspekce poruch</h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>
        <p className="text-xs text-gray-500 mb-4">
          AI projde všechny fotky, najde trhliny a výlomy betonu, promítne je do 3D modelu a zapíše je jako
          poruchy se stavem „Nová“. Stejná porucha z více fotek se sloučí, rozměry se měří v metrech, když je
          známé měřítko modelu (kalibrace nebo GPS z dronu). Každý nález je návrh — potvrďte ho nebo zamítněte
          v seznamu poruch.
        </p>

        <div className="flex items-center gap-3 mb-4">
          <span className="text-sm font-medium">{STATE_LABEL[data?.status ?? 'none'] ?? data?.status}</span>
          {data?.status === 'detecting' && (
            <div className="flex-1 h-1.5 bg-gray-100 rounded">
              <div className="h-1.5 bg-blue-500 rounded" style={{ width: `${data.progress ?? 0}%` }} />
            </div>
          )}
          <div className="ml-auto flex gap-2">
            <button className="btn-primary text-xs" disabled={busy || !hasModel} onClick={() => run.mutate(false)}
              title={hasModel ? '' : 'Nejdřív je potřeba 3D model'}>
              <ScanSearch size={14} /> {data?.status === 'completed' ? 'Znovu zanést do modelu' : 'Spustit'}
            </button>
            <button className="btn-secondary text-xs" disabled={busy || !hasModel} onClick={() => run.mutate(true)}
              title="Znovu prohledat všechny fotky (např. po aktualizaci modelu AI)">
              <RefreshCw size={14} /> Znovu prohledat fotky
            </button>
          </div>
        </div>
        {data?.error && <p className="text-xs text-red-600 mb-3">{data.error}</p>}

        {data?.status === 'completed' && (
          <div className="mb-5 text-sm">
            <p className="mb-1">
              <b>{data.n_defects}</b> poruch zapsáno do modelu (z {data.n_detections} nálezů na fotkách;
              nálezy vidět jen na jedné fotce se nepočítají).
            </p>
            <p className="text-xs text-gray-500 mb-2">
              {data.metric
                ? `Rozměry v metrech${data.scale_note ? ` — ${data.scale_note}` : ''}.`
                : 'Měřítko modelu není známé — délky a plochy se doplní po kalibraci (Měření → Kalibrovat).'}
            </p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(data.by_type ?? {}).map(([k, v]) => (
                <span key={k} className="badge bg-gray-100 text-gray-700 text-xs">{k}: {v}</span>
              ))}
            </div>
          </div>
        )}

        {!!data?.overlays?.length && (
          <>
            <p className="text-xs font-semibold text-gray-600 mb-2">
              Fotky s nálezy ({data.overlays.length}) — červeně obrys, zeleně osa trhliny
            </p>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
              {data.overlays.map(o => (
                <a key={o.photo} href={o.url} target="_blank" rel="noreferrer" className="block">
                  <img src={o.url} loading="lazy" className="w-full rounded border border-gray-200" />
                  <div className="text-[11px] text-gray-500 mt-0.5">{o.photo} · {o.n} nálezů</div>
                </a>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
