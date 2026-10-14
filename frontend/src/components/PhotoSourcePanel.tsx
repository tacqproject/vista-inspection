import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { X, ZoomIn, MousePointer, ChevronDown, Crosshair, History, ImagePlus, Check } from 'lucide-react'
import { photosApi, inspectionsApi, annotationPhotosApi } from '../api/inspections'
import type { ProjectedPhoto } from '../api/inspections'
import { useViewerStore, type ClickedFaceInfo } from '../store/viewerStore'
import PhotoLightbox, { type LightboxItem } from './PhotoLightbox'

const PAGE = 24   // thumbnails per page

interface PhotoMeta { filename: string; url: string; size: number }

interface Props {
  inspectionId: string
  structureId?: string
  canonical?: boolean
  clickedFace: ClickedFaceInfo | null
  scaleFactor?: number
}

export default function PhotoSourcePanel({ inspectionId, structureId, canonical, clickedFace, scaleFactor = 1 }: Props) {
  const [lightbox, setLightbox] = useState<{ items: LightboxItem[]; index: number } | null>(null)
  // when the photos are shown for a SELECTED defect, source photos can be
  // attached to it as detail photos ("either upload or pick one of these")
  const selectedAnnotation = useViewerStore(s => s.selectedAnnotation)
  const attachId = clickedFace?.fromAnnotation ? selectedAnnotation?.id : undefined

  const { data, isLoading } = useQuery({
    queryKey: ['inspection-photos', inspectionId],
    queryFn: () => photosApi.list(inspectionId),
  })

  const mp = clickedFace?.modelPoint
  const mn = clickedFace?.modelNormal
  const outline = clickedFace?.outline
  const outlineColor = clickedFace?.outlineColor ?? '#ef4444'
  const { data: projected, isLoading: projLoading, error: projError } = useQuery({
    queryKey: ['project-point', inspectionId, mp?.x, mp?.y, mp?.z, outline?.length],
    queryFn: () => photosApi.projectPoint(inspectionId, {
      ...mp!, nx: mn?.x, ny: mn?.y, nz: mn?.z,
      outline: outline?.map(p => [p.x, p.y, p.z]),
    }),
    enabled: !!mp,
    retry: false,
    staleTime: Infinity,
  })

  // Same spot across every inspection, date-ordered → degradation timeline
  const { data: timeline } = useQuery({
    queryKey: ['timeline-photos', structureId, mp?.x, mp?.y, mp?.z],
    queryFn: () => inspectionsApi.timelinePhotos(structureId!, {
      inspection_id: inspectionId, x: mp!.x, y: mp!.y, z: mp!.z,
      canonical, nx: mn?.x, ny: mn?.y, nz: mn?.z,
    }),
    enabled: !!mp && !!structureId,
    retry: false,
    staleTime: Infinity,
  })

  const photos = data?.photos ?? []

  return (
    <div className="p-3 space-y-3">
      {/* No click yet — show hint */}
      {!clickedFace && (
        <div className="flex flex-col items-center justify-center py-6 text-center">
          <MousePointer size={28} className="text-gray-300 mb-2" />
          <p className="text-sm text-gray-500 font-medium">Klikněte na model</p>
          <p className="text-xs text-gray-400 mt-1 max-w-[200px]">
            Ve "view" módu klikněte na místo na modelu — najdou se snímky, kde je toto místo vidět.
          </p>
        </div>
      )}

      {/* Same spot across inspections over time — degradation timeline */}
      {clickedFace && mp && timeline && timeline.items.length > 1 && (() => {
        // flatten to one lightbox reel across all inspections & angles
        const flat = timeline.items.flatMap(it => it.photos.map(ph => ({
          ...ph, inspection_id: it.inspection_id, name: it.name, date: it.date,
        })))
        const lbItems: LightboxItem[] = flat.map(ph => ({
          src: `/uploads/inspections/${ph.inspection_id}/photos/${ph.filename}`,
          thumbSrc: photosApi.thumbnailUrl(ph.inspection_id, ph.filename, 1200),
          filename: `${ph.name} · ${new Date(ph.date).toLocaleDateString('cs-CZ')}`,
          marker: { u: ph.u, v: ph.v },
          outlineColor,
        }))
        return (
          <div>
            <p className="text-xs font-semibold text-gray-600 mb-1.5 flex items-center gap-1">
              <History size={12} /> Časová osa místa
            </p>
            <div className="space-y-2">
              {timeline.items.map(it => (
                <div key={it.inspection_id}>
                  <div className="text-[10px] mb-1 truncate">
                    <span className="font-semibold text-gray-600">{new Date(it.date).toLocaleDateString('cs-CZ')}</span>
                    <span className="text-gray-400"> · {it.name} · {it.photos.length} sn.</span>
                  </div>
                  <div className="flex gap-1.5 overflow-x-auto pb-1">
                    {it.photos.map((ph) => {
                      const gi = flat.findIndex(f => f.inspection_id === it.inspection_id && f.filename === ph.filename)
                      return (
                        <button
                          key={ph.filename}
                          onClick={() => setLightbox({ items: lbItems, index: gi })}
                          className="shrink-0 w-24 group"
                        >
                          <div className="relative rounded-md overflow-hidden border border-gray-200 group-hover:border-brand-300">
                            <img
                              src={photosApi.thumbnailUrl(it.inspection_id, ph.filename, 400)}
                              className="w-24 h-20 object-cover bg-gray-100"
                              loading="lazy"
                            />
                            <span
                              className="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] rounded-full border-2 border-white shadow"
                              style={{ left: `${ph.u * 100}%`, top: `${ph.v * 100}%`, background: outlineColor }}
                            />
                          </div>
                        </button>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )
      })()}

      {/* Photos that see the clicked point, best views first */}
      {clickedFace && mp && (
        <div>
          <p className="text-xs font-semibold text-gray-600 mb-1.5 flex items-center gap-1">
            <Crosshair size={12} /> Snímky s tímto místem
            {projected ? ` (${projected.total})` : ''}
          </p>
          {projLoading ? (
            <p className="text-xs text-gray-400 py-3 text-center">Hledám snímky…</p>
          ) : projError ? (
            <div className="bg-amber-50 border border-amber-200 rounded-lg p-2.5 text-xs text-amber-700">
              Pozice kamer nejsou pro tento model k dispozici. Spusťte rekonstrukci
              znovu — nové běhy je už ukládají.
            </div>
          ) : projected && projected.photos.length === 0 ? (
            <p className="text-xs text-gray-400 py-3 text-center">
              Toto místo není vidět na žádném snímku.
            </p>
          ) : projected && (
            <>
              {projected.occluded && (
                <div className="bg-amber-50 border border-amber-200 rounded-lg p-2 text-[11px] text-amber-700 mb-1.5">
                  Kliknuté místo je v zákrytu nebo v díře modelu (např. nezrekonstruované
                  dveře) — vyznačené pozice na snímcích jsou přibližné.
                </div>
              )}
              <ProjectedGrid
                inspectionId={inspectionId}
                items={projected.photos}
                color={outlineColor}
                attachId={attachId}
                onOpen={(i) => setLightbox({
                  items: projected.photos.map(p => ({
                    src: `/uploads/inspections/${inspectionId}/photos/${p.filename}`,
                    thumbSrc: photosApi.thumbnailUrl(inspectionId, p.filename, 1200),
                    filename: p.filename,
                    marker: { u: p.u, v: p.v },
                    outline: p.outline ?? undefined,
                    outlineColor,
                  })),
                  index: i,
                })}
              />
            </>
          )}
        </div>
      )}

      {/* Source photo grid */}
      <div>
        <p className="text-xs font-semibold text-gray-600 mb-1.5">
          Zdrojové snímky ({photos.length})
        </p>
        {isLoading ? (
          <p className="text-xs text-gray-400 py-4 text-center">Načítám…</p>
        ) : photos.length === 0 ? (
          <p className="text-xs text-gray-400 py-4 text-center">Žádné snímky nebyly nahrány.</p>
        ) : (
          <PhotoGrid
            inspectionId={inspectionId}
            photos={photos}
            onOpen={(i) => setLightbox({
              items: photos.map(p => ({
                src: p.url,
                thumbSrc: photosApi.thumbnailUrl(inspectionId, p.filename, 1200),
                filename: p.filename,
              })),
              index: i,
            })}
          />
        )}
      </div>

      {lightbox && (
        <PhotoLightbox
          items={lightbox.items}
          index={lightbox.index}
          onNavigate={(i) => setLightbox(lb => lb ? { ...lb, index: i } : lb)}
          onClose={() => setLightbox(null)}
          measure={{ inspectionId, scaleFactor }}
        />
      )}
    </div>
  )
}

// ─── Projected photo grid (photos that see the clicked 3D point) ─────────────

function ProjectedGrid({ inspectionId, items, color = '#ef4444', onOpen, attachId }: {
  inspectionId: string
  items: ProjectedPhoto[]
  color?: string
  onOpen: (index: number) => void
  attachId?: string   // when set, source photos can be attached to this defect
}) {
  const [shown, setShown] = useState(6)
  const qc = useQueryClient()
  const [attached, setAttached] = useState<Set<string>>(new Set())
  const attachMut = useMutation({
    mutationFn: (filename: string) => annotationPhotosApi.attachSource(inspectionId, attachId!, filename),
    onSuccess: (_d, filename) => {
      setAttached(s => new Set(s).add(filename))
      qc.invalidateQueries({ queryKey: ['annotation-photos', inspectionId, attachId] })
    },
  })

  return (
    <div>
      {attachId && (
        <p className="text-[10px] text-gray-400 mb-1 flex items-center gap-1">
          <ImagePlus size={10} className="text-brand-500" /> Přiřaď fotku k poruše tlačítkem v rohu.
        </p>
      )}
      <div className="grid grid-cols-2 gap-1.5">
        {items.slice(0, shown).map((item, i) => (
          <div
            key={item.filename}
            role="button"
            onClick={() => onOpen(i)}
            className="relative aspect-[3/2] bg-slate-100 rounded-md overflow-hidden hover:ring-2 hover:ring-brand-400 transition-all cursor-pointer group"
            title={`${item.filename} · vzdálenost ${item.distance} j.`}
          >
            <img
              src={photosApi.thumbnailUrl(inspectionId, item.filename, 400)}
              alt={item.filename}
              className="w-full h-full object-cover"
              loading="lazy"
              decoding="async"
            />
            <ProjectionOverlay item={item} color={color} />
            {attachId && (
              <button
                onClick={(e) => { e.stopPropagation(); if (!attached.has(item.filename)) attachMut.mutate(item.filename) }}
                disabled={attachMut.isPending}
                title={attached.has(item.filename) ? 'Přiřazeno k poruše' : 'Přiřadit tuto fotku k poruše'}
                className={`absolute top-1 right-1 rounded p-1 text-white shadow ${attached.has(item.filename) ? 'bg-green-600' : 'bg-brand-600/90 hover:bg-brand-600'}`}
              >
                {attached.has(item.filename) ? <Check size={12} /> : <ImagePlus size={12} />}
              </button>
            )}
            <div className="absolute bottom-0 inset-x-0 bg-black/50 text-white/90 text-[9px] px-1 py-0.5 truncate text-left">
              {item.filename}
            </div>
          </div>
        ))}
      </div>
      {shown < items.length && (
        <button
          onClick={() => setShown(s => s + 6)}
          className="mt-1.5 w-full text-xs text-gray-500 hover:text-gray-700 flex items-center justify-center gap-1 py-1"
        >
          <ChevronDown size={13} /> Další snímky ({items.length - shown})
        </button>
      )}
    </div>
  )
}

/** Draws the defect on a projected photo: the polygon outline if present,
    otherwise a point marker — matching how it's shown on the 3D model. */
function ProjectionOverlay({ item, color }: { item: ProjectedPhoto; color: string }) {
  const verts = (item.outline ?? []).filter((p): p is { u: number; v: number } => !!p)
  if (verts.length >= 2) {
    const pointsAttr = verts.map(p => `${p.u * 100},${p.v * 100}`).join(' ')
    return (
      <svg
        className="absolute inset-0 w-full h-full pointer-events-none"
        viewBox="0 0 100 100" preserveAspectRatio="none"
      >
        <polygon
          points={pointsAttr}
          fill={color} fillOpacity={0.18}
          stroke={color} strokeWidth={1.2}
          strokeLinejoin="round" vectorEffect="non-scaling-stroke"
        />
      </svg>
    )
  }
  // point defect → circular marker
  return (
    <div
      className="absolute w-4 h-4 -ml-2 -mt-2 rounded-full border-2 pointer-events-none shadow"
      style={{ left: `${item.u * 100}%`, top: `${item.v * 100}%`, borderColor: color, backgroundColor: `${color}40` }}
    />
  )
}

// ─── Photo Grid with pagination ───────────────────────────────────────────────

function PhotoGrid({ inspectionId, photos, onOpen }: {
  inspectionId: string
  photos: PhotoMeta[]
  onOpen: (index: number) => void
}) {
  const [shown, setShown] = useState(PAGE)

  return (
    <div>
      <div className="grid grid-cols-3 gap-1.5">
        {photos.slice(0, shown).map((p, i) => (
          <Thumb
            key={p.filename}
            thumbUrl={photosApi.thumbnailUrl(inspectionId, p.filename, 300)}
            fullUrl={p.url}
            name={p.filename}
            onOpen={() => onOpen(i)}
          />
        ))}
      </div>

      {shown < photos.length && (
        <button
          onClick={() => setShown(s => s + PAGE)}
          className="mt-2 w-full text-xs text-gray-500 hover:text-gray-700 flex items-center justify-center gap-1 py-1.5"
        >
          <ChevronDown size={13} />
          Zobrazit další ({photos.length - shown} zbývá)
        </button>
      )}
    </div>
  )
}

function Thumb({ thumbUrl, fullUrl, name, onOpen }: {
  thumbUrl: string; fullUrl: string; name: string; onOpen: () => void
}) {
  const [errored, setErrored] = useState(false)

  return (
    <button
      onClick={onOpen}
      className="aspect-square bg-slate-100 rounded-md overflow-hidden hover:ring-2 hover:ring-brand-400 transition-all group relative"
      title={name}
    >
      <img
        src={errored ? fullUrl : thumbUrl}  // fallback to full if thumbnail fails
        alt={name}
        onError={() => setErrored(true)}
        className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
        loading="lazy"
        decoding="async"
      />
      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/20 transition-colors flex items-center justify-center">
        <ZoomIn size={16} className="text-white opacity-0 group-hover:opacity-100 drop-shadow" />
      </div>
    </button>
  )
}
