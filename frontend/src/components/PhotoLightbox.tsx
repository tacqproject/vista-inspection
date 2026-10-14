import { useEffect, useRef, useState, useCallback } from 'react'
import { X, ChevronLeft, ChevronRight, RotateCcw, Ruler } from 'lucide-react'
import { photosApi } from '../api/inspections'
import { useViewerStore } from '../store/viewerStore'

export interface LightboxItem {
  src: string
  filename: string
  thumbSrc?: string                   // low-res placeholder shown while full loads
  marker?: { u: number; v: number }   // point of interest (0–1 image coords)
  outline?: Array<{ u: number; v: number } | null>   // defect polygon boundary
  outlineColor?: string
}

interface Props {
  items: LightboxItem[]
  index: number
  onNavigate: (i: number) => void
  onClose: () => void
  /** enables in-photo measuring by back-projection onto the mesh */
  measure?: { inspectionId: string; scaleFactor: number }
}

/**
 * Photo lightbox for inspecting the marked point of interest:
 *  - wheel zoom stays ANCHORED on the marker (or image center without one),
 *  - ←/→ switch photos; the zoom factor persists and the view re-centers
 *    on the next photo's marker,
 *  - drag pans, reset button / photo switch recenters.
 */
export default function PhotoLightbox({ items, index, onNavigate, onClose, measure }: Props) {
  // clamp — the source list can shrink (new query result) while the index persists
  const item = items[Math.min(index, items.length - 1)]
  const containerRef = useRef<HTMLDivElement>(null)
  const [zoom, setZoom] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })     // manual pan
  const [imgDims, setImgDims] = useState<{ w: number; h: number } | null>(null)
  const [containerDims, setContainerDims] = useState<{ w: number; h: number } | null>(null)
  const [fullLoaded, setFullLoaded] = useState(false)
  const dragRef = useRef<{ x: number; y: number } | null>(null)
  const movedRef = useRef(false)

  // in-photo measuring (back-projection onto the mesh)
  const { modelTransform } = useViewerStore()
  const [measuring, setMeasuring] = useState(false)
  const [measurePts, setMeasurePts] = useState<Array<{ u: number; v: number }>>([])
  const [measureResult, setMeasureResult] = useState<{ meters: number | null; miss: boolean } | null>(null)
  const [measureBusy, setMeasureBusy] = useState(false)

  const resetMeasure = () => { setMeasurePts([]); setMeasureResult(null) }

  const runMeasure = async (pts: Array<{ u: number; v: number }>) => {
    if (!measure || !modelTransform) return
    setMeasureBusy(true)
    try {
      const r = await photosApi.measurePhoto(measure.inspectionId, item.filename, pts)
      if (r.distance_model != null) {
        // mesh units → scene units (display scale) → meters (calibration)
        const meters = r.distance_model * modelTransform.scale * (measure.scaleFactor || 1)
        setMeasureResult({ meters, miss: false })
      } else {
        setMeasureResult({ meters: null, miss: true })
      }
    } catch {
      setMeasureResult({ meters: null, miss: true })
    } finally {
      setMeasureBusy(false)
    }
  }

  const anchor = item.marker ?? { u: 0.5, v: 0.5 }

  // measure container
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const measure = () => setContainerDims({ w: el.clientWidth, h: el.clientHeight })
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // photo switch: keep zoom AND previous dimensions (photos share aspect —
  // nulling dims would blank the view for a few frames), recenter on marker
  useEffect(() => { setOffset({ x: 0, y: 0 }); setFullLoaded(false); resetMeasure() }, [item.src])

  // preload neighbours (thumbs + full) so arrow navigation is instant
  useEffect(() => {
    const preloaded: HTMLImageElement[] = []
    const load = (src?: string) => {
      if (!src) return
      const img = new Image()
      img.src = src
      preloaded.push(img)
    }
    // full-res only for direct neighbours (15MB files — keep memory pressure
    // low, GPU context loss was observed with aggressive preloading);
    // thumbs are cheap, take one more
    for (const d of [1, -1]) {
      const it = items[(index + d + items.length) % items.length]
      if (it && it.src !== item.src) {
        load(it.thumbSrc)
        load(it.src)
      }
    }
    const ahead = items[(index + 2) % items.length]
    if (ahead && ahead.src !== item.src) load(ahead.thumbSrc)
    return () => preloaded.forEach(img => { img.src = '' })
  }, [index, items])

  const navigate = useCallback((dir: number) => {
    onNavigate((index + dir + items.length) % items.length)
  }, [index, items.length, onNavigate])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'ArrowLeft') navigate(-1)
      if (e.key === 'ArrowRight') navigate(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate, onClose])

  // base (contain-fit) size of the image inside the container
  const base = (() => {
    if (!imgDims || !containerDims) return null
    const s = Math.min(containerDims.w / imgDims.w, containerDims.h / imgDims.h)
    return { w: imgDims.w * s, h: imgDims.h * s }
  })()

  // transform: anchor point → container center (+ manual pan)
  const transform = (() => {
    if (!base || !containerDims) return undefined
    const ax = anchor.u * base.w
    const ay = anchor.v * base.h
    const tx = containerDims.w / 2 - ax * zoom + offset.x
    const ty = containerDims.h / 2 - ay * zoom + offset.y
    return { tx, ty }
  })()

  const handleWheel = (e: React.WheelEvent) => {
    const factor = e.deltaY < 0 ? 1.25 : 1 / 1.25
    setZoom(z => Math.min(Math.max(z * factor, 1), 40))
  }

  const markerScreen = transform && base && item.marker
    ? { x: transform.tx + item.marker.u * base.w * zoom, y: transform.ty + item.marker.v * base.h * zoom }
    : null

  // defect polygon boundary, projected into this photo, in screen coords
  const outlineColor = item.outlineColor ?? '#ef4444'
  const outlineScreen = transform && base && item.outline
    ? item.outline
        .filter((p): p is { u: number; v: number } => !!p)
        .map(p => ({ x: transform.tx + p.u * base.w * zoom, y: transform.ty + p.v * base.h * zoom }))
    : null
  const hasOutline = !!(outlineScreen && outlineScreen.length >= 2)

  return (
    <div className="fixed inset-0 z-[100] bg-black/95 flex flex-col" onClick={onClose}>
      {/* Top bar */}
      <div className="flex items-center gap-3 px-4 py-2 text-white/70 text-xs shrink-0" onClick={e => e.stopPropagation()}>
        <span className="font-mono">{item.filename}</span>
        <span className="text-white/40">{index + 1} / {items.length}</span>
        <span className="text-white/40">zoom {zoom.toFixed(1)}×</span>
        <button
          onClick={() => { setZoom(1); setOffset({ x: 0, y: 0 }) }}
          className="btn-ghost text-white/60 hover:text-white text-xs p-1"
          title="Reset zoomu"
        >
          <RotateCcw size={13} />
        </button>
        {measure && (
          <button
            onClick={() => { setMeasuring(m => !m); resetMeasure() }}
            className={`flex items-center gap-1 rounded px-2 py-1 transition-colors
              ${measuring ? 'bg-amber-500 text-black font-semibold' : 'text-white/60 hover:text-white hover:bg-white/10'}`}
            title="Měření na fotce — 2 kliky, vzdálenost se počítá zpětnou projekcí na 3D model"
          >
            <Ruler size={13} /> Měřit
          </button>
        )}
        {measuring && (
          <span className="text-amber-300">
            {measureBusy ? 'Počítám…'
              : measureResult?.miss ? 'Bod mimo model — zkuste jinde'
              : measureResult?.meters != null ? `${measureResult.meters.toFixed(3)} m`
              : measurePts.length === 1 ? 'Klikněte na druhý bod'
              : 'Klikněte na první bod'}
          </span>
        )}
        <span className="flex-1" />
        <span className="text-white/40 hidden sm:block">← → přepínání · kolečko = zoom na bod zájmu · tažení = posun</span>
        <button onClick={onClose} className="text-white/60 hover:text-white p-1"><X size={18} /></button>
      </div>

      {/* Image area */}
      <div
        ref={containerRef}
        className="flex-1 relative overflow-hidden cursor-grab active:cursor-grabbing"
        onClick={e => e.stopPropagation()}
        onWheel={handleWheel}
        onMouseDown={e => { dragRef.current = { x: e.clientX, y: e.clientY }; movedRef.current = false }}
        onMouseMove={e => {
          if (!dragRef.current) return
          // compute the delta BEFORE queueing the state update — the updater
          // runs later, when dragRef may already be nulled by mouseup
          const dx = e.clientX - dragRef.current.x
          const dy = e.clientY - dragRef.current.y
          if (Math.abs(dx) + Math.abs(dy) > 2) movedRef.current = true
          dragRef.current = { x: e.clientX, y: e.clientY }
          setOffset(o => ({ x: o.x + dx, y: o.y + dy }))
        }}
        onMouseUp={e => {
          dragRef.current = null
          if (!measuring || movedRef.current || !transform || !base) return
          // click (not a drag) in measure mode → add a point
          const rect = containerRef.current!.getBoundingClientRect()
          const u = ((e.clientX - rect.left - transform.tx) / zoom) / base.w
          const v = ((e.clientY - rect.top - transform.ty) / zoom) / base.h
          if (u < 0 || v < 0 || u > 1 || v > 1) return
          setMeasureResult(null)
          setMeasurePts(prev => {
            const next = prev.length >= 2 ? [{ u, v }] : [...prev, { u, v }]
            if (next.length === 2) runMeasure(next)
            return next
          })
        }}
        onMouseLeave={() => { dragRef.current = null }}
      >
        {transform && base && (
          <>
            {/* low-res layer — always mounted (no unmount flash), full photo
                crossfades over it when ready */}
            {item.thumbSrc && (
              <img
                src={item.thumbSrc}
                alt=""
                draggable={false}
                className="absolute select-none"
                style={{
                  width: base.w,
                  height: base.h,
                  transform: `translate(${transform.tx}px, ${transform.ty}px) scale(${zoom})`,
                  transformOrigin: '0 0',
                }}
              />
            )}
            <img
              src={item.src}
              alt={item.filename}
              draggable={false}
              onLoad={() => setFullLoaded(true)}
              className="absolute select-none"
              style={{
                width: base.w,
                height: base.h,
                transform: `translate(${transform.tx}px, ${transform.ty}px) scale(${zoom})`,
                transformOrigin: '0 0',
                imageRendering: zoom > 4 ? 'pixelated' : 'auto',
                opacity: fullLoaded ? 1 : 0,
                transition: 'opacity 120ms ease-in',
              }}
            />
          </>
        )}
        {/* hidden loader to get natural dimensions ASAP (thumb wins the race,
            aspect ratio is identical so the fit is already correct) */}
        <img
          src={item.thumbSrc ?? item.src}
          alt=""
          className="hidden"
          onLoad={e => {
            // read synchronously — the synthetic event is recycled before a
            // functional setState updater would run
            const { naturalWidth: w, naturalHeight: h } = e.currentTarget
            setImgDims(d => d ?? { w, h })
          }}
        />

        {/* Defect polygon outline (matches the 3D view) */}
        {hasOutline && (
          <svg className="absolute inset-0 w-full h-full pointer-events-none">
            <polygon
              points={outlineScreen!.map(p => `${p.x},${p.y}`).join(' ')}
              fill={outlineColor} fillOpacity={0.15}
              stroke={outlineColor} strokeWidth={2} strokeLinejoin="round"
            />
          </svg>
        )}

        {/* Marker overlay (fixed screen size, doesn't scale with the image) —
            only for point defects; a polygon shows its outline instead */}
        {markerScreen && !hasOutline && (
          <div
            className="absolute w-8 h-8 -ml-4 -mt-4 rounded-full border-[3px] pointer-events-none"
            style={{ left: markerScreen.x, top: markerScreen.y, borderColor: outlineColor, backgroundColor: `${outlineColor}1a` }}
          />
        )}

        {/* Measure overlay: points + connecting line + label */}
        {measuring && transform && base && measurePts.length > 0 && (() => {
          const sp = measurePts.map(p => ({
            x: transform.tx + p.u * base.w * zoom,
            y: transform.ty + p.v * base.h * zoom,
          }))
          return (
            <svg className="absolute inset-0 w-full h-full pointer-events-none">
              {sp.length === 2 && (
                <line x1={sp[0].x} y1={sp[0].y} x2={sp[1].x} y2={sp[1].y}
                  stroke="#f59e0b" strokeWidth={2} strokeDasharray="6 4" />
              )}
              {sp.map((p, i) => (
                <g key={i}>
                  <circle cx={p.x} cy={p.y} r={6} fill="none" stroke="#f59e0b" strokeWidth={2.5} />
                  <line x1={p.x - 10} y1={p.y} x2={p.x + 10} y2={p.y} stroke="#f59e0b" strokeWidth={1} />
                  <line x1={p.x} y1={p.y - 10} x2={p.x} y2={p.y + 10} stroke="#f59e0b" strokeWidth={1} />
                </g>
              ))}
              {sp.length === 2 && measureResult?.meters != null && (
                <text
                  x={(sp[0].x + sp[1].x) / 2} y={(sp[0].y + sp[1].y) / 2 - 10}
                  fill="#fbbf24" fontSize={14} fontWeight={700} textAnchor="middle"
                  style={{ paintOrder: 'stroke', stroke: 'rgba(0,0,0,0.8)', strokeWidth: 3 }}
                >
                  {measureResult.meters.toFixed(3)} m
                </text>
              )}
            </svg>
          )
        })()}

        {/* Nav arrows */}
        {items.length > 1 && (
          <>
            <button
              onClick={() => navigate(-1)}
              className="absolute left-3 top-1/2 -translate-y-1/2 bg-black/50 hover:bg-black/75 text-white rounded-full p-2.5 transition-colors"
            >
              <ChevronLeft size={22} />
            </button>
            <button
              onClick={() => navigate(1)}
              className="absolute right-3 top-1/2 -translate-y-1/2 bg-black/50 hover:bg-black/75 text-white rounded-full p-2.5 transition-colors"
            >
              <ChevronRight size={22} />
            </button>
          </>
        )}
      </div>
    </div>
  )
}
