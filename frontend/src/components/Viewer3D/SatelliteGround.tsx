import { useEffect, useState } from 'react'
import * as THREE from 'three'
import { useThree } from '@react-three/fiber'

export interface MapConfig {
  lat: number
  lon: number
  rotationDeg: number     // model isn't georeferenced → user aligns north manually
  sizeMeters: number      // ground plane edge length
  offsetX?: number        // manual position compensation, meters
  offsetY?: number
  offsetZ?: number
  scalePct?: number       // manual scale compensation (100 = calibrated size)
  metersToScene: number   // 1 m in scene units (display scale / inspection scale factor)
  groundY: number         // scene-space height of the model's bottom
}

const TILE_URL = (z: number, y: number, x: number) =>
  `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`
const GRID = 4          // 4×4 tiles stitched into one texture
const TILE_PX = 256

/**
 * Satellite imagery ground plane under the model (Esri World Imagery).
 * Tiles around lat/lon are stitched into a single canvas texture; the zoom
 * level is chosen so the requested ground size spans the tile grid.
 */
export default function SatelliteGround({ cfg }: { cfg: MapConfig }) {
  const [texture, setTexture] = useState<THREE.CanvasTexture | null>(null)
  const [coverage, setCoverage] = useState(1)   // actual meters covered by the texture
  const invalidate = useThree(s => s.invalidate)

  useEffect(() => {
    let cancelled = false

    const build = async () => {
      // zoom so that GRID tiles ≈ sizeMeters
      const latR = (cfg.lat * Math.PI) / 180
      const metersPerTileAt = (z: number) => (156543.03392 * Math.cos(latR) / 2 ** z) * TILE_PX
      let z = 19
      while (z > 3 && metersPerTileAt(z) * GRID < cfg.sizeMeters) z--
      const metersPerTile = metersPerTileAt(z)

      // center tile (fractional) — offset the grid so lat/lon is exactly centered
      const n = 2 ** z
      const xf = ((cfg.lon + 180) / 360) * n
      const yf = ((1 - Math.log(Math.tan(latR) + 1 / Math.cos(latR)) / Math.PI) / 2) * n

      const x0 = Math.floor(xf - GRID / 2)
      const y0 = Math.floor(yf - GRID / 2)
      const fracX = xf - GRID / 2 - x0     // 0..1 sub-tile shift
      const fracY = yf - GRID / 2 - y0

      const canvas = document.createElement('canvas')
      canvas.width = canvas.height = GRID * TILE_PX
      const ctx = canvas.getContext('2d')!
      ctx.fillStyle = '#20303f'
      ctx.fillRect(0, 0, canvas.width, canvas.height)

      await Promise.all(
        Array.from({ length: GRID + 1 }, (_, j) =>
          Array.from({ length: GRID + 1 }, (_, i) =>
            new Promise<void>(resolve => {
              const img = new Image()
              img.crossOrigin = 'anonymous'
              img.onload = () => {
                ctx.drawImage(img, (i - fracX) * TILE_PX, (j - fracY) * TILE_PX)
                resolve()
              }
              img.onerror = () => resolve()
              img.src = TILE_URL(z, y0 + j, x0 + i)
            }),
          ),
        ).flat(),
      )

      if (cancelled) return
      const tex = new THREE.CanvasTexture(canvas)
      tex.colorSpace = THREE.SRGBColorSpace
      tex.anisotropy = 4
      setTexture(tex)
      setCoverage(metersPerTile * GRID)
      invalidate()
    }

    build()
    return () => { cancelled = true }
  }, [cfg.lat, cfg.lon, cfg.sizeMeters, invalidate])

  if (!texture) return null

  // scale compensation applies to the whole meters→scene mapping so the
  // plane size and the XYZ offsets stay mutually consistent
  const m2s = cfg.metersToScene * ((cfg.scalePct ?? 100) / 100)
  const sceneSize = coverage * m2s

  return (
    <mesh
      rotation={[-Math.PI / 2, 0, (cfg.rotationDeg * Math.PI) / 180]}
      position={[
        (cfg.offsetX ?? 0) * m2s,
        cfg.groundY - 0.005 + (cfg.offsetY ?? 0) * m2s,
        (cfg.offsetZ ?? 0) * m2s,
      ]}
      raycast={() => null}
    >
      <planeGeometry args={[sceneSize, sceneSize]} />
      <meshBasicMaterial map={texture} toneMapped={false} />
    </mesh>
  )
}
