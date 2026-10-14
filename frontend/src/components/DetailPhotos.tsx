import { useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Camera, Trash2, Ruler, Star, Pencil, Sparkles } from 'lucide-react'
import { annotationPhotosApi, annotationsApi, measurementsApi } from '../api/inspections'
import type { DetailPhoto } from '../api/inspections'
import PhotoMeasureModal from './PhotoMeasureModal'
import PhotoMarkupModal from './PhotoMarkupModal'
import DefectAiModal from './DefectAiModal'

/**
 * Close-up photos attached to a defect annotation. The commercial inspection
 * workflow: overview photos build the 3D model, close-up photos (with a ruler
 * or crack gauge in frame) carry the measurable detail — measured in
 * PhotoMeasureModal after in-photo calibration.
 */
export default function DetailPhotos({ inspectionId, annotationId, reportPhoto }: {
  inspectionId: string
  annotationId: string
  /** filename currently chosen as the defect's representative photo for the PDF */
  reportPhoto?: string
}) {
  const qc = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const [measured, setMeasured] = useState<DetailPhoto | null>(null)
  const [markup, setMarkup] = useState<DetailPhoto | null>(null)
  const [aiPhoto, setAiPhoto] = useState<DetailPhoto | null>(null)

  const { data } = useQuery({
    queryKey: ['annotation-photos', inspectionId, annotationId],
    queryFn: () => annotationPhotosApi.list(inspectionId, annotationId),
  })
  const photos = data?.photos ?? []

  const uploadMut = useMutation({
    mutationFn: (files: File[]) => annotationPhotosApi.upload(inspectionId, annotationId, files),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['annotation-photos', inspectionId, annotationId] }),
  })

  const deleteMut = useMutation({
    mutationFn: (filename: string) => annotationPhotosApi.delete(inspectionId, annotationId, filename),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['annotation-photos', inspectionId, annotationId] }),
  })

  // Mark (or unmark) a photo as the defect's representative shot for the PDF protocol.
  const reportMut = useMutation({
    mutationFn: (filename: string | null) =>
      annotationsApi.update(inspectionId, annotationId, { report_photo: filename }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['annotations', inspectionId] }),
  })

  return (
    <div className="mt-2">
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-xs font-semibold text-gray-600 flex items-center gap-1">
          <Camera size={12} /> Detailní snímky ({photos.length})
        </p>
        <button
          onClick={() => fileRef.current?.click()}
          disabled={uploadMut.isPending}
          className="btn-ghost text-xs text-brand-600 p-1"
        >
          {uploadMut.isPending ? 'Nahrávám…' : '+ Nahrát'}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={e => {
            const files = Array.from(e.target.files ?? [])
            if (files.length) uploadMut.mutate(files)
            e.target.value = ''
          }}
        />
      </div>

      {photos.length === 0 ? (
        <p className="text-[11px] text-gray-400">
          Nahrajte detailní fotky poruchy (s měřítkem/přiloženým metrem v záběru) — půjde na nich měřit.
        </p>
      ) : (
        <>
        <p className="text-[10px] text-gray-400 mb-1 flex items-center gap-1">
          <Star size={10} className="text-amber-400" /> Hvězdičkou označte reprezentativní fotku do protokolu.
        </p></>
      )}
      {photos.length > 0 && (
        <div className="grid grid-cols-3 gap-1.5">
          {photos.map(p => {
            const isReport = reportPhoto === p.filename
            return (
            <div key={p.filename} className="relative group">
              <button
                onClick={() => setMeasured(p)}
                className={`aspect-square w-full bg-slate-100 rounded-md overflow-hidden hover:ring-2 hover:ring-brand-400 ${isReport ? 'ring-2 ring-amber-400' : ''}`}
                title={`${p.filename} — kliknutím otevřete měření`}
              >
                <img src={p.url} alt={p.filename} className="w-full h-full object-cover" loading="lazy" />
                <div className="absolute inset-0 bg-black/0 group-hover:bg-black/25 transition-colors flex items-center justify-center">
                  <Ruler size={14} className="text-white opacity-0 group-hover:opacity-100" />
                </div>
              </button>
              <button
                onClick={() => reportMut.mutate(isReport ? null : p.filename)}
                title={isReport ? 'Reprezentativní fotka do protokolu (kliknutím zrušit)' : 'Použít jako fotku poruchy do protokolu'}
                className={`absolute top-0.5 left-0.5 rounded p-0.5 ${isReport ? 'bg-amber-400 text-white' : 'bg-black/50 text-white/80 opacity-0 group-hover:opacity-100 hover:text-amber-300'}`}
              >
                <Star size={11} fill={isReport ? 'currentColor' : 'none'} />
              </button>
              <button
                onClick={() => setMarkup(p)}
                title="Označit poruchu ve fotce (šipka/kroužek)"
                className="absolute bottom-0.5 left-0.5 bg-black/50 rounded p-0.5 text-white/80 opacity-0 group-hover:opacity-100 hover:text-blue-300"
              >
                <Pencil size={11} />
              </button>
              <button
                onClick={() => setAiPhoto(p)}
                title="AI detekce poruch"
                className="absolute bottom-0.5 right-0.5 bg-black/50 rounded p-0.5 text-white/80 opacity-0 group-hover:opacity-100 hover:text-purple-300"
              >
                <Sparkles size={11} />
              </button>
              <button
                onClick={() => { if (confirm('Smazat snímek?')) deleteMut.mutate(p.filename) }}
                className="absolute top-0.5 right-0.5 bg-black/50 rounded p-0.5 text-white/80 opacity-0 group-hover:opacity-100 hover:text-red-400"
              >
                <Trash2 size={11} />
              </button>
            </div>
            )
          })}
        </div>
      )}

      {measured && (
        <PhotoMeasureModal
          photo={measured}
          onClose={() => setMeasured(null)}
          onSave={async (valueMm, meta) => {
            await measurementsApi.create(inspectionId, {
              name: meta.name || `Měření z ${measured.filename}`,
              measurement_type: 'photo',
              value: valueMm,
              unit: 'mm',
              points_json: {
                annotation_id: annotationId,
                photo: measured.url,
                ...meta.geometry,
              } as any,
              notes: meta.notes || undefined,
            })
            qc.invalidateQueries({ queryKey: ['measurements', inspectionId] })
          }}
        />
      )}

      {markup && (
        <PhotoMarkupModal
          photo={markup}
          onClose={() => setMarkup(null)}
          onSave={async (blob) => {
            const name = `oznaceno_${Date.now()}.jpg`
            const file = new File([blob], name, { type: 'image/jpeg' })
            await annotationPhotosApi.upload(inspectionId, annotationId, [file])
            // The marked copy becomes the defect's representative photo.
            await annotationsApi.update(inspectionId, annotationId, { report_photo: name })
            qc.invalidateQueries({ queryKey: ['annotation-photos', inspectionId, annotationId] })
            qc.invalidateQueries({ queryKey: ['annotations', inspectionId] })
          }}
        />
      )}

      {aiPhoto && (
        <DefectAiModal
          inspectionId={inspectionId}
          annotationId={annotationId}
          photo={aiPhoto}
          onClose={() => setAiPhoto(null)}
        />
      )}
    </div>
  )
}
