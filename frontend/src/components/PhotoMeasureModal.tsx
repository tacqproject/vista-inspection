import { useEffect, useRef, useState, useCallback } from 'react'
import { X, Ruler, Crosshair, RotateCcw, Save } from 'lucide-react'
import type { DetailPhoto } from '../api/inspections'
import { errorMessage } from '../api/client'

type Pt = { x: number; y: number }   // pixel coords in the ORIGINAL image

interface Props {
  photo: DetailPhoto
  onClose: () => void
  onSave: (valueMm: number, meta: {
    name: string
    notes: string
    geometry: { calibration_px: Pt[]; calibration_mm: number; points_px: Pt[] }
  }) => Promise<void>
}

/**
 * Measure on a close-up photo:
 *  1. calibration — click both ends of a known reference (ruler/gauge) and
 *     enter its real length in mm,
 *  2. measurement — click two points, distance is reported in mm.
 * Wheel = zoom to cursor (needed for sub-mm precision), drag = pan.
 */
export default function PhotoMeasureModal({ photo, onClose, onSave }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const viewRef = useRef({ scale: 1, ox: 0, oy: 0 })   // image→canvas transform
  const dragRef = useRef<{ x: number; y: number } | null>(null)
  const movedRef = useRef(false)

  const [phase, setPhase] = useState<'calibrate' | 'measure'>('calibrate')
  const [calibPts, setCalibPts] = useState<Pt[]>([])
  const [calibMm, setCalibMm] = useState('')
  const [measPts, setMeasPts] = useState<Pt[]>([])
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  const [imgReady, setImgReady] = useState(false)

  const pxPerMm = (() => {
    const mm = parseFloat(calibMm.replace(',', '.'))
    if (calibPts.length !== 2 || !mm || mm <= 0) return null
    const d = Math.hypot(calibPts[1].x - calibPts[0].x, calibPts[1].y - calibPts[0].y)
    return d / mm
  })()

  const measuredMm = (pxPerMm && measPts.length === 2)
    ? Math.hypot(measPts[1].x - measPts[0].x, measPts[1].y - measPts[0].y) / pxPerMm
    : null

  const redraw = useCallback(() => {
    const canvas = canvasRef.current, img = imgRef.current
    if (!canvas || !img) return
    const ctx = canvas.getContext('2d')!
    const { scale, ox, oy } = viewRef.current
    ctx.fillStyle = '#0f172a'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.imageSmoothingEnabled = scale < 3
    ctx.drawImage(img, ox, oy, img.naturalWidth * scale, img.naturalHeight * scale)

    const toCanvas = (p: Pt) => ({ x: p.x * scale + ox, y: p.y * scale + oy })
    const drawSeg = (pts: Pt[], color: string, label?: string) => {
      ctx.strokeStyle = color
      ctx.fillStyle = color
      ctx.lineWidth = 2
      const cpts = pts.map(toCanvas)
      for (const c of cpts) {
        ctx.beginPath(); ctx.arc(c.x, c.y, 5, 0, Math.PI * 2); ctx.stroke()
        ctx.beginPath()
        ctx.moveTo(c.x - 9, c.y); ctx.lineTo(c.x + 9, c.y)
        ctx.moveTo(c.x, c.y - 9); ctx.lineTo(c.x, c.y + 9)
        ctx.stroke()
      }
      if (cpts.length === 2) {
        ctx.beginPath(); ctx.moveTo(cpts[0].x, cpts[0].y); ctx.lineTo(cpts[1].x, cpts[1].y); ctx.stroke()
        if (label) {
          const mx = (cpts[0].x + cpts[1].x) / 2, my = (cpts[0].y + cpts[1].y) / 2
          ctx.font = 'bold 13px system-ui'
          const w = ctx.measureText(label).width
          ctx.fillStyle = 'rgba(0,0,0,0.75)'
          ctx.fillRect(mx - w / 2 - 5, my - 22, w + 10, 19)
          ctx.fillStyle = color
          ctx.fillText(label, mx - w / 2, my - 8)
        }
      }
    }
    drawSeg(calibPts, '#38bdf8', pxPerMm && calibPts.length === 2 ? `${calibMm} mm (reference)` : undefined)
    drawSeg(measPts, '#f59e0b', measuredMm != null ? `${measuredMm.toFixed(2)} mm` : undefined)
  }, [calibPts, measPts, calibMm, pxPerMm, measuredMm])

  useEffect(() => { redraw() }, [redraw, imgReady])

  // load image + initial fit
  useEffect(() => {
    const img = new Image()
    img.onload = () => {
      imgRef.current = img
      const canvas = canvasRef.current
      if (canvas) {
        const s = Math.min(canvas.width / img.naturalWidth, canvas.height / img.naturalHeight)
        viewRef.current = {
          scale: s,
          ox: (canvas.width - img.naturalWidth * s) / 2,
          oy: (canvas.height - img.naturalHeight * s) / 2,
        }
      }
      setImgReady(true)
    }
    img.src = photo.url
  }, [photo.url])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const canvasToImage = (cx: number, cy: number): Pt => {
    const { scale, ox, oy } = viewRef.current
    return { x: (cx - ox) / scale, y: (cy - oy) / scale }
  }

  const handleWheel = (e: React.WheelEvent) => {
    const canvas = canvasRef.current!
    const rect = canvas.getBoundingClientRect()
    const cx = e.clientX - rect.left, cy = e.clientY - rect.top
    const v = viewRef.current
    const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2
    const ns = Math.min(Math.max(v.scale * factor, 0.02), 40)
    // zoom to cursor: keep the image point under the cursor fixed
    v.ox = cx - (cx - v.ox) * (ns / v.scale)
    v.oy = cy - (cy - v.oy) * (ns / v.scale)
    v.scale = ns
    redraw()
  }

  const handleDown = (e: React.MouseEvent) => {
    dragRef.current = { x: e.clientX, y: e.clientY }
    movedRef.current = false
  }
  const handleMove = (e: React.MouseEvent) => {
    if (!dragRef.current) return
    const dx = e.clientX - dragRef.current.x, dy = e.clientY - dragRef.current.y
    if (Math.abs(dx) + Math.abs(dy) > 3) movedRef.current = true
    viewRef.current.ox += dx
    viewRef.current.oy += dy
    dragRef.current = { x: e.clientX, y: e.clientY }
    redraw()
  }
  const handleUp = (e: React.MouseEvent) => {
    dragRef.current = null
    if (movedRef.current) return   // was a pan, not a click
    const rect = canvasRef.current!.getBoundingClientRect()
    const p = canvasToImage(e.clientX - rect.left, e.clientY - rect.top)
    const img = imgRef.current
    if (!img || p.x < 0 || p.y < 0 || p.x > img.naturalWidth || p.y > img.naturalHeight) return
    if (phase === 'calibrate') {
      setCalibPts(prev => prev.length >= 2 ? [p] : [...prev, p])
    } else {
      setMeasPts(prev => prev.length >= 2 ? [p] : [...prev, p])
    }
  }

  const save = async () => {
    if (measuredMm == null) return
    setSaving(true)
    try {
      await onSave(Math.round(measuredMm * 100) / 100, {
        name,
        notes: `Kalibrace: ${calibMm} mm reference, foto ${photo.filename}`,
        geometry: {
          calibration_px: calibPts,
          calibration_mm: parseFloat(calibMm.replace(',', '.')),
          points_px: measPts,
        },
      })
      setMeasPts([])
      setName('')
    } catch (err: any) {
      alert(errorMessage(err, 'Uložení měření selhalo'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[110] bg-black/85 flex items-center justify-center" onClick={onClose}>
      <div
        className="bg-slate-900 rounded-xl shadow-2xl overflow-hidden flex flex-col"
        style={{ width: 'min(96vw, 1200px)', height: 'min(94vh, 860px)' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-2 bg-slate-800 shrink-0">
          <Ruler size={15} className="text-brand-400" />
          <p className="text-sm text-white/90 font-medium truncate flex-1">{photo.filename}</p>
          <button onClick={onClose} className="text-white/60 hover:text-white p-1"><X size={17} /></button>
        </div>

        {/* Toolbar */}
        <div className="flex items-center gap-3 px-4 py-2 bg-slate-800/60 shrink-0 flex-wrap">
          <div className="flex rounded-lg overflow-hidden border border-slate-600">
            <button
              onClick={() => setPhase('calibrate')}
              className={`px-3 py-1.5 text-xs flex items-center gap-1.5
                ${phase === 'calibrate' ? 'bg-sky-600 text-white' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
            >
              <Crosshair size={12} /> 1. Kalibrace
            </button>
            <button
              onClick={() => setPhase('measure')}
              disabled={!pxPerMm}
              className={`px-3 py-1.5 text-xs flex items-center gap-1.5 disabled:opacity-40
                ${phase === 'measure' ? 'bg-amber-600 text-white' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
            >
              <Ruler size={12} /> 2. Měření
            </button>
          </div>

          {phase === 'calibrate' && (
            <div className="flex items-center gap-2 text-xs text-slate-300">
              <span>{calibPts.length < 2 ? `Klikněte na ${calibPts.length === 0 ? 'začátek' : 'konec'} reference (pravítko/měrka)` : 'Délka reference:'}</span>
              {calibPts.length === 2 && (
                <input
                  className="w-24 bg-slate-700 text-white text-xs rounded px-2 py-1 border border-slate-600"
                  placeholder="mm"
                  inputMode="decimal"
                  value={calibMm}
                  onChange={e => setCalibMm(e.target.value)}
                  autoFocus
                />
              )}
              {pxPerMm && <span className="text-sky-400">({pxPerMm.toFixed(1)} px/mm — rozlišení {(1 / pxPerMm).toFixed(3)} mm/px)</span>}
            </div>
          )}

          {phase === 'measure' && (
            <div className="flex items-center gap-2 text-xs text-slate-300 flex-1 min-w-0">
              {measuredMm == null
                ? <span>Klikněte na {measPts.length === 0 ? 'první' : 'druhý'} bod měření</span>
                : <span className="text-amber-400 font-semibold text-sm">{measuredMm.toFixed(2)} mm</span>}
              {measuredMm != null && (
                <>
                  <input
                    className="w-44 bg-slate-700 text-white text-xs rounded px-2 py-1 border border-slate-600"
                    placeholder="Název (např. šířka praskliny)"
                    value={name}
                    onChange={e => setName(e.target.value)}
                  />
                  <button onClick={save} disabled={saving} className="btn-primary text-xs py-1">
                    <Save size={12} /> {saving ? '…' : 'Uložit měření'}
                  </button>
                </>
              )}
            </div>
          )}

          <button
            onClick={() => { phase === 'calibrate' ? (setCalibPts([]), setCalibMm('')) : setMeasPts([]) }}
            className="btn-ghost text-slate-300 text-xs p-1 ml-auto"
            title="Znovu"
          >
            <RotateCcw size={13} />
          </button>
        </div>

        {/* Canvas */}
        <div className="flex-1 min-h-0">
          <canvas
            ref={canvasRef}
            width={1160}
            height={700}
            className="w-full h-full cursor-crosshair"
            onWheel={handleWheel}
            onMouseDown={handleDown}
            onMouseMove={handleMove}
            onMouseUp={handleUp}
            onMouseLeave={() => { dragRef.current = null }}
          />
        </div>

        <p className="px-4 py-1.5 text-[10px] text-slate-500 bg-slate-800/40 shrink-0">
          Kolečko = zoom k kurzoru · tažení = posun · klik = bod · přesnost závisí na rozlišení snímku (viz mm/px u kalibrace)
        </p>
      </div>
    </div>
  )
}
