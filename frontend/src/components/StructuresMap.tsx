import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { MapPin } from 'lucide-react'
import type { Structure } from '../types'

const TILE = 256
const TILE_URL = (z: number, x: number, y: number) =>
  `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`

// Web Mercator: geographic → fractional tile coordinates at zoom z.
const lon2tx = (lon: number, z: number) => ((lon + 180) / 360) * 2 ** z
const lat2ty = (lat: number, z: number) => {
  const r = (lat * Math.PI) / 180
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z
}

/**
 * Dashboard map of all georeferenced structures, drawn from ArcGIS World
 * Imagery tiles (same source the 3D satellite ground uses) with clickable
 * markers — no Leaflet dependency, plain Web-Mercator tile math.
 */
export default function StructuresMap({ structures }: { structures: Structure[] }) {
  const navigate = useNavigate()
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(800)
  const H = 340

  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(320, e.contentRect.width)))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])

  const pts = useMemo(
    () => structures.filter(s => s.latitude != null && s.longitude != null)
      .map(s => ({ s, lat: s.latitude as number, lon: s.longitude as number })),
    [structures],
  )

  const view = useMemo(() => {
    if (!pts.length) return null
    const lats = pts.map(p => p.lat), lons = pts.map(p => p.lon)
    const minLat = Math.min(...lats), maxLat = Math.max(...lats)
    const minLon = Math.min(...lons), maxLon = Math.max(...lons)
    const cLat = (minLat + maxLat) / 2, cLon = (minLon + maxLon) / 2

    // Largest zoom at which the bounding box (plus padding) still fits the canvas.
    let zoom = 16
    for (let z = 16; z >= 2; z--) {
      const spanX = Math.abs(lon2tx(maxLon, z) - lon2tx(minLon, z)) * TILE
      const spanY = Math.abs(lat2ty(maxLat, z) - lat2ty(minLat, z)) * TILE
      if (spanX <= w - 80 && spanY <= H - 80) { zoom = z; break }
      zoom = z
    }
    if (pts.length === 1) zoom = 15

    const cx = lon2tx(cLon, zoom) * TILE          // center in world pixels
    const cy = lat2ty(cLat, zoom) * TILE
    const originX = cx - w / 2                     // top-left of canvas in world px
    const originY = cy - H / 2
    return { zoom, originX, originY }
  }, [pts, w])

  if (!pts.length) {
    return (
      <div className="card p-6 text-center text-sm text-gray-400 flex items-center justify-center gap-2">
        <MapPin size={15} /> Žádná konstrukce nemá zadané GPS souřadnice.
      </div>
    )
  }

  const { zoom, originX, originY } = view!
  const n = 2 ** zoom
  const x0 = Math.floor(originX / TILE), x1 = Math.floor((originX + w) / TILE)
  const y0 = Math.floor(originY / TILE), y1 = Math.floor((originY + H) / TILE)
  const tiles: Array<{ x: number; y: number; left: number; top: number }> = []
  for (let x = x0; x <= x1; x++)
    for (let y = y0; y <= y1; y++) {
      const wx = ((x % n) + n) % n
      tiles.push({ x: wx, y, left: x * TILE - originX, top: y * TILE - originY })
    }

  return (
    <div className="card p-0 overflow-hidden">
      <div ref={ref} className="relative bg-slate-800" style={{ height: H }}>
        {tiles.map((t, i) => (
          t.y >= 0 && t.y < n ? (
            <img key={i} src={TILE_URL(zoom, t.x, t.y)} alt="" draggable={false}
              className="absolute select-none" width={TILE} height={TILE}
              style={{ left: t.left, top: t.top }} />
          ) : null
        ))}

        {pts.map(({ s, lat, lon }) => {
          const left = lon2tx(lon, zoom) * TILE - originX
          const top = lat2ty(lat, zoom) * TILE - originY
          if (left < 0 || left > w || top < 0 || top > H) return null
          return (
            <button key={s.id} onClick={() => navigate(`/structures/${s.id}`)}
              title={s.name}
              className="absolute -translate-x-1/2 -translate-y-full group"
              style={{ left, top }}>
              <MapPin size={26} className="text-red-500 drop-shadow-[0_1px_2px_rgba(0,0,0,0.8)] fill-red-500/30" />
              <span className="absolute left-1/2 -translate-x-1/2 top-full whitespace-nowrap text-[10px] font-medium
                bg-black/70 text-white px-1.5 py-0.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                {s.name}
              </span>
            </button>
          )
        })}

        <div className="absolute bottom-1 right-1.5 text-[9px] text-white/70 bg-black/40 px-1 rounded">
          © Esri World Imagery
        </div>
      </div>
    </div>
  )
}
