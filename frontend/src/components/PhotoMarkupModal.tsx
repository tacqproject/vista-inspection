import { useEffect, useRef, useState } from 'react'
import { X, MoveUpRight, Circle, Pencil, Undo2, Save } from 'lucide-react'
import type { DetailPhoto } from '../api/inspections'

type Tool = 'arrow' | 'ellipse' | 'free'
type Pt = { x: number; y: number }              // natural image pixels
type Shape =
  | { t: 'arrow'; a: Pt; b: Pt }
  | { t: 'ellipse'; a: Pt; b: Pt }
  | { t: 'free'; pts: Pt[] }

const STROKE = '#ef4444'

/**
 * Draw an arrow / circle / freehand mark on a defect photo to highlight the
 * damage, then save a flattened copy that becomes the defect's representative
 * photo in the PDF protocol.
 */
export default function PhotoMarkupModal({ photo, onSave, onClose }: {
  photo: DetailPhoto
  onSave: (blob: Blob) => Promise<void>
  onClose: () => void
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const [ready, setReady] = useState(false)
  const [tool, setTool] = useState<Tool>('arrow')
  const [shapes, setShapes] = useState<Shape[]>([])
  const [saving, setSaving] = useState(false)
  const draftRef = useRef<Shape | null>(null)
  const drawingRef = useRef(false)

  // Load the image once.
  useEffect(() => {
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => { imgRef.current = img; setReady(true) }
    img.src = photo.url
  }, [photo.url])

  // Line width scales with image size so marks read at any resolution.
  const lw = () => Math.max(3, Math.round((imgRef.current?.naturalWidth ?? 1000) / 250))

  const render = () => {
    const canvas = canvasRef.current, img = imgRef.current
    if (!canvas || !img) return
    const ctx = canvas.getContext('2d')!
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
    const all = draftRef.current ? [...shapes, draftRef.current] : shapes
    ctx.strokeStyle = STROKE
    ctx.fillStyle = STROKE
    ctx.lineWidth = lw()
    ctx.lineJoin = 'round'
    ctx.lineCap = 'round'
    for (const s of all) drawShape(ctx, s, lw())
  }

  useEffect(render)

  // Map a pointer event to natural-image pixel coordinates.
  const toImg = (e: React.PointerEvent): Pt => {
    const canvas = canvasRef.current!
    const rect = canvas.getBoundingClientRect()
    return {
      x: ((e.clientX - rect.left) / rect.width) * canvas.width,
      y: ((e.clientY - rect.top) / rect.height) * canvas.height,
    }
  }

  const onDown = (e: React.PointerEvent) => {
    if (!ready) return
    drawingRef.current = true
    const p = toImg(e)
    draftRef.current = tool === 'free' ? { t: 'free', pts: [p] } : { t: tool, a: p, b: p }
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
  }
  const onMove = (e: React.PointerEvent) => {
    if (!drawingRef.current || !draftRef.current) return
    const p = toImg(e)
    const d = draftRef.current
    if (d.t === 'free') d.pts.push(p)
    else d.b = p
    render()
  }
  const onUp = () => {
    if (draftRef.current) setShapes(s => [...s, draftRef.current!])
    draftRef.current = null
    drawingRef.current = false
  }

  const save = async () => {
    const canvas = canvasRef.current
    if (!canvas) return
    setSaving(true)
    try {
      const blob: Blob = await new Promise((res, rej) =>
        canvas.toBlob(b => (b ? res(b) : rej(new Error('toBlob failed'))), 'image/jpeg', 0.92))
      await onSave(blob)
      onClose()
    } finally {
      setSaving(false)
    }
  }

  const nw = imgRef.current?.naturalWidth ?? 1000
  const nh = imgRef.current?.naturalHeight ?? 750

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="card w-full max-w-3xl mx-4 p-4 max-h-[95vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-gray-900 flex items-center gap-2">
            <Pencil size={16} /> Označit poruchu ve fotce
          </h3>
          <button onClick={onClose} className="btn-ghost p-1"><X size={16} /></button>
        </div>

        <div className="flex items-center gap-2 mb-3">
          {([['arrow', 'Šipka', MoveUpRight], ['ellipse', 'Kroužek', Circle], ['free', 'Volně', Pencil]] as const)
            .map(([t, lbl, Icon]) => (
              <button key={t} onClick={() => setTool(t)}
                className={`text-xs px-2.5 py-1.5 rounded-lg border-2 font-medium flex items-center gap-1 transition-all
                  ${tool === t ? 'border-red-400 bg-red-50 text-red-700' : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'}`}>
                <Icon size={13} /> {lbl}
              </button>
            ))}
          <button onClick={() => setShapes(s => s.slice(0, -1))} disabled={!shapes.length}
            className="btn-ghost text-xs ml-auto disabled:opacity-40">
            <Undo2 size={13} /> Zpět
          </button>
        </div>

        <div className="bg-slate-100 rounded-lg overflow-hidden flex items-center justify-center" style={{ maxHeight: '65vh' }}>
          <canvas
            ref={canvasRef}
            width={nw}
            height={nh}
            onPointerDown={onDown}
            onPointerMove={onMove}
            onPointerUp={onUp}
            className="max-w-full touch-none"
            style={{ maxHeight: '65vh', cursor: 'crosshair' }}
          />
        </div>

        <div className="flex items-center justify-between mt-3">
          <p className="text-[11px] text-gray-400">Uložením se vytvoří označená kopie a nastaví jako fotka do protokolu.</p>
          <div className="flex gap-2">
            <button onClick={onClose} className="btn-secondary text-sm">Zrušit</button>
            <button onClick={save} disabled={saving || !shapes.length} className="btn-primary text-sm disabled:opacity-50">
              <Save size={14} /> {saving ? 'Ukládám…' : 'Uložit označení'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function drawShape(ctx: CanvasRenderingContext2D, s: Shape, lw: number) {
  if (s.t === 'free') {
    ctx.beginPath()
    s.pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)))
    ctx.stroke()
  } else if (s.t === 'ellipse') {
    const cx = (s.a.x + s.b.x) / 2, cy = (s.a.y + s.b.y) / 2
    const rx = Math.abs(s.b.x - s.a.x) / 2, ry = Math.abs(s.b.y - s.a.y) / 2
    ctx.beginPath()
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2)
    ctx.stroke()
  } else {
    // arrow shaft + head
    ctx.beginPath()
    ctx.moveTo(s.a.x, s.a.y)
    ctx.lineTo(s.b.x, s.b.y)
    ctx.stroke()
    const ang = Math.atan2(s.b.y - s.a.y, s.b.x - s.a.x)
    const head = lw * 4
    ctx.beginPath()
    ctx.moveTo(s.b.x, s.b.y)
    ctx.lineTo(s.b.x - head * Math.cos(ang - Math.PI / 6), s.b.y - head * Math.sin(ang - Math.PI / 6))
    ctx.lineTo(s.b.x - head * Math.cos(ang + Math.PI / 6), s.b.y - head * Math.sin(ang + Math.PI / 6))
    ctx.closePath()
    ctx.fill()
  }
}
