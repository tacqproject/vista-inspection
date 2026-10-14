import { useEffect, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { X, Sparkles, Play, FileText, AlertTriangle } from 'lucide-react'
import { aiApi, reportsApi, type AiSegmentResult, type AiSegmentImage } from '../api/inspections'

/**
 * Run the dacl10k defect-segmentation model over every photo of the inspection
 * and browse the per-image overlays + class coverage. Heavy model runs as an
 * external command (see ai/README.md); this only drives it.
 */
export default function AiSegmentModal({ inspectionId, onClose }: {
  inspectionId: string
  onClose: () => void
}) {
  const [pdfBusy, setPdfBusy] = useState(false)

  const { data, refetch } = useQuery<AiSegmentResult>({
    queryKey: ['ai-segment', inspectionId],
    queryFn: () => aiApi.get(inspectionId),
    // poll while a run is in progress
    refetchInterval: q => (q.state.data?.status === 'processing' ? 2000 : false),
  })

  const startMut = useMutation({
    mutationFn: () => aiApi.start(inspectionId),
    onSuccess: () => refetch(),
  })

  const status = data?.status ?? 'none'
  const available = data?.engine?.available
  const labels = data?.class_labels ?? {}
  const damage = new Set(data?.damage_classes ?? [])
  const images = data?.images ?? []
  const flagged = images.filter(im => Object.entries(im.coverage).some(([c, v]) => damage.has(c) && v > 0.001))

  const downloadPdf = async () => {
    setPdfBusy(true)
    try { await reportsApi.downloadAiSegmentation(inspectionId) } finally { setPdfBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="card w-full max-w-5xl mx-4 p-4 max-h-[94vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-gray-900 flex items-center gap-2">
            <Sparkles size={16} className="text-purple-600" /> AI analýza poruch (dacl10k)
          </h3>
          <button onClick={onClose} className="btn-ghost p-1"><X size={16} /></button>
        </div>

        {!available && status !== 'completed' && status !== 'processing' && (
          <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800 flex items-start gap-2 mb-3">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <div>
              AI model není nakonfigurován ({data?.engine?.hint ?? 'nastavte DACL10K_CMD'}). Viz <code>ai/README.md</code>.
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 mb-3">
          <button
            onClick={() => startMut.mutate()}
            disabled={!available || status === 'processing' || startMut.isPending}
            className="btn-primary text-sm disabled:opacity-50"
          >
            <Play size={14} /> {status === 'completed' ? 'Spustit znovu' : 'Spustit analýzu všech snímků'}
          </button>
          {status === 'completed' && flagged.length > 0 && (
            <button onClick={downloadPdf} disabled={pdfBusy} className="btn-secondary text-sm">
              <FileText size={14} /> {pdfBusy ? '…' : 'PDF přehled'}
            </button>
          )}
          <p className="text-xs text-gray-400 ml-auto">
            Orientační podklad k odborné revizi. Trhliny/kaverny model pozná slabě.
          </p>
        </div>

        {status === 'processing' && (
          <div className="py-8 text-center text-sm text-gray-500">
            <div className="animate-spin rounded-full h-6 w-6 border-2 border-purple-500 border-t-transparent mx-auto mb-3" />
            {data?.message ?? 'Zpracovávám…'} {data?.progress != null && `(${data.progress} %)`}
          </div>
        )}

        {status === 'failed' && (
          <p className="text-sm text-red-600 py-4">Analýza selhala: {data?.error}</p>
        )}

        {status === 'completed' && (
          <>
            <p className="text-sm text-gray-600 mb-3">
              Porucha detekována na <b>{flagged.length}</b> z <b>{images.length}</b> snímků.
            </p>
            {flagged.length === 0 ? (
              <p className="text-sm text-gray-400 py-4">Na žádném snímku nebyla detekována porucha nad prahem.</p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {flagged.map(im => <Card key={im.filename} im={im} labels={labels} damage={damage} />)}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function Card({ im, labels, damage }: {
  im: AiSegmentImage
  labels: Record<string, string>
  damage: Set<string>
}) {
  const found = Object.entries(im.coverage)
    .filter(([c, v]) => damage.has(c) && v > 0.001)
    .sort((a, b) => b[1] - a[1])
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      {im.overlay_url && (
        <img src={im.overlay_url} alt={im.filename} className="w-full aspect-[4/3] object-cover" loading="lazy" />
      )}
      <div className="p-2">
        <p className="text-xs font-medium text-gray-700 truncate">{im.filename}</p>
        <div className="mt-1 space-y-0.5">
          {found.map(([c, v]) => (
            <div key={c} className="flex items-center justify-between text-[11px]">
              <span className="text-gray-600">{labels[c] ?? c}</span>
              <span className="font-mono text-gray-500">{(v * 100).toFixed(2)} %</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
