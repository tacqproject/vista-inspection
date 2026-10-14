import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useThree } from '@react-three/fiber'
import { Html } from '@react-three/drei'
import { useQuery } from '@tanstack/react-query'
import { TilesRenderer } from '3d-tiles-renderer/r3f'
import * as THREE from 'three'
import { applyPhotoShading, disposePhotoShading } from './photoShading'
import { requestBVH, dropBVH } from './bvhQueue'
import { Eye } from 'lucide-react'
import { useViewerStore, type ViewerTool } from '../../store/viewerStore'
import { unrotate, useRetargetOnDoubleClick } from './interaction'

/**
 * Streamed, hierarchical model (OGC 3D Tiles built by backend app/services/tiles.py).
 *
 * The whole model is a kd-tree of tiles, each with its own geometry AND its own
 * texture baked at a resolution matching its level. Zoomed out, only the coarse
 * root (a few MB) is loaded; zooming in streams full-resolution leaves just for
 * the part in view, and unloads what's no longer needed (LRU cache). The full
 * mesh stays on the server (photo projection, measurements, PDF).
 *
 * Tiles live in exactly the same frame as model_lod0.gltf (tileset asset.gltfUpAxis
 * = "Z" → no content rotation; a canonical bake is the root `transform`), so the
 * scene normalization, annotations, cameras and clicks work unchanged.
 */

const IS_TOUCH = /iPad|iPhone|iPod|Android/.test(navigator.userAgent)
  || (navigator.userAgent.includes('Mac') && navigator.maxTouchPoints > 1)

// Screen-space error target in px. Tile geometricError ≈ 4 × its world texel size
// (plus measured surface deviation), so 6 px ⇒ a coarser tile is replaced once
// one of its texels would span > ~1.5 screen pixels — sharp at every zoom level.
const ERROR_TARGET = IS_TOUCH ? 12 : 6
const GB = 1024 ** 3
const CACHE_MIN = IS_TOUCH ? 0.35 * GB : 1.0 * GB
const CACHE_MAX = IS_TOUCH ? 0.6 * GB : 1.6 * GB
// iOS Safari kills the tab on GPU-memory pressure. Tilesets built since 10/2026
// have ≤ 2048² textures (nothing to do); older ones have up to 4096² (64 MB
// decoded each) — on touch devices those are downsized to 2048² on load.
const MOBILE_TEX = 2048

function downsizeTexture(tex: THREE.Texture | null | undefined) {
  const img: any = tex?.image
  if (!tex || !img || !img.width || Math.max(img.width, img.height) <= MOBILE_TEX) return
  const k = MOBILE_TEX / Math.max(img.width, img.height)
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(img.width * k))
  canvas.height = Math.max(1, Math.round(img.height * k))
  canvas.getContext('2d')?.drawImage(img, 0, 0, canvas.width, canvas.height)
  img.close?.()                         // free the full-size ImageBitmap
  tex.image = canvas
  tex.needsUpdate = true
}

/** Surface size of one texture pixel at a raycast hit (world = scene units). */
function texelAt(hit: THREE.Intersection): number | null {
  const mesh = hit.object as THREE.Mesh
  const face = hit.face
  const geo = mesh.geometry as THREE.BufferGeometry
  const uv = geo?.getAttribute('uv')
  const img: any = (mesh.material as any)?.map?.image
  if (!face || !uv || !img?.width) return null
  const pos = geo.getAttribute('position')
  const [a, b, c] = [face.a, face.b, face.c].map(i =>
    new THREE.Vector3().fromBufferAttribute(pos, i).applyMatrix4(mesh.matrixWorld))
  const area = b.clone().sub(a).cross(c.clone().sub(a)).length() / 2
  const ua = uv.getX(face.a), va = uv.getY(face.a)
  const uvArea = Math.abs((uv.getX(face.b) - ua) * (uv.getY(face.c) - va) - (uv.getX(face.c) - ua) * (uv.getY(face.b) - va)) / 2
  const texels = uvArea * img.width * img.height
  return texels > 0 && area > 0 ? Math.sqrt(area / texels) : null
}

type Tileset = {
  root: unknown
  extras?: { bounds?: { min: number[]; max: number[] }; built?: string }
}

/** `…/model_lod0.gltf` → `/uploads/…/model_tiles/tileset.json` (null for non-LOD models). */
export function tilesetUrlFor(modelPath: string | null | undefined): string | null {
  if (!modelPath || !/_lod\d\.gltf$/i.test(modelPath)) return null
  return `/uploads/${modelPath.replace(/_lod\d\.gltf$/i, '_tiles/tileset.json')}`
}

/** Fetch the tileset if the backend has built one (null → fall back to glTF LODs). */
export function useTileset(url: string | null) {
  return useQuery({
    queryKey: ['tileset', url],
    enabled: !!url,
    retry: false,
    staleTime: 30_000,
    queryFn: async (): Promise<Tileset | null> => {
      const r = await fetch(url!, { cache: 'no-cache' })
      if (!r.ok) return null
      try {
        const j = await r.json()
        return j && j.root ? (j as Tileset) : null
      } catch {
        return null            // e.g. an HTML fallback page
      }
    },
  })
}

export default function TilesModel({ url, tileset, tool, onPick, baseOpacity = 1, inspectionId, canonical = false }: {
  url: string
  tileset: Tileset
  tool: ViewerTool
  onPick?: (p: { x: number; y: number; z: number }) => void
  baseOpacity?: number
  inspectionId: string
  canonical?: boolean
}) {
  const setPendingPosition = useViewerStore(s => s.setPendingPosition)
  const setAnnotationFormOpen = useViewerStore(s => s.setAnnotationFormOpen)
  const addMeasurePoint = useViewerStore(s => s.addMeasurePoint)
  const addPolygonPoint = useViewerStore(s => s.addPolygonPoint)
  const setHoverPoint3D = useViewerStore(s => s.setHoverPoint3D)
  const setClickedFaceInfo = useViewerStore(s => s.setClickedFaceInfo)
  const clickedFaceInfo = useViewerStore(s => s.clickedFaceInfo)
  const setModelTransform = useViewerStore(s => s.setModelTransform)
  const setModelError = useViewerStore(s => s.setModelError)
  const setModelSettleFn = useViewerStore(s => s.setModelSettleFn)
  const invalidate = useThree(s => s.invalidate)
  const gl = useThree(s => s.gl)

  const tilesRef = useRef<any>(null)
  const [rootLoaded, setRootLoaded] = useState(false)
  const opacityRef = useRef(baseOpacity)
  opacityRef.current = baseOpacity
  const photoShading = useViewerStore(s => s.photoShading)
  const photoShadingRef = useRef(photoShading)
  photoShadingRef.current = photoShading

  // Version the URL by build time: a rebuilt tileset gets a fresh renderer
  // instance instead of mixing old in-memory tiles with new ones.
  const versionedUrl = useMemo(
    () => (tileset.extras?.built ? `${url}?v=${encodeURIComponent(tileset.extras.built)}` : url),
    [url, tileset.extras?.built],
  )

  // Same normalization as the glTF LOD path (centred 5-unit box; canonical =
  // identity), computed from the CURRENT model bounds the backend put in the
  // tileset — so annotations / cameras / measurements line up identically.
  const { scale, offset, groundY } = useMemo(() => {
    const b = tileset.extras?.bounds
    const lo = new THREE.Vector3(...((b?.min ?? [0, 0, 0]) as [number, number, number]))
    const hi = new THREE.Vector3(...((b?.max ?? [1, 1, 1]) as [number, number, number]))
    if (canonical) return { scale: 1, offset: new THREE.Vector3(), groundY: lo.y }
    const center = lo.clone().add(hi).multiplyScalar(0.5)
    const size = hi.clone().sub(lo)
    const maxDim = Math.max(size.x, size.y, size.z)
    const s = maxDim > 0 ? 5 / maxDim : 1
    const off = center.multiplyScalar(-s)
    return { scale: s, offset: off, groundY: lo.y * s + off.y }
  }, [tileset, canonical])

  // publish once the root tile is actually on screen (hides the loading overlay)
  useEffect(() => {
    if (!rootLoaded) return
    setModelTransform({ scale, offset: { x: offset.x, y: offset.y, z: offset.z }, groundY })
    return () => setModelTransform(null)
  }, [rootLoaded, scale, offset, groundY, setModelTransform])

  useEffect(() => { setRootLoaded(false); setModelError(null) }, [versionedUrl, setModelError])

  const applyOpacity = useCallback((root: THREE.Object3D, opacity: number) => {
    const faded = opacity < 0.999
    root.traverse((o: any) => {
      if (!o.isMesh) return
      const mats = Array.isArray(o.material) ? o.material : [o.material]
      for (const m of mats) {
        if (!m) continue
        m.transparent = faded
        m.opacity = faded ? opacity : 1
        m.depthWrite = !faded
        m.needsUpdate = true
      }
    })
  }, [])

  const maxAniso = useMemo(() => Math.min(8, gl.capabilities.getMaxAnisotropy()), [gl])

  const onLoadModel = useCallback(({ scene }: { scene: THREE.Object3D }) => {
    scene.traverse((o: any) => {
      if (!o.isMesh) return
      // BVH (fast clicks / surface normals on dense leaf tiles), built in a
      // worker — on the main thread it stuttered every streamed tile on iPad
      requestBVH(o.geometry)
      const mats = Array.isArray(o.material) ? o.material : [o.material]
      for (const m of mats) {
        if (!m?.map) continue
        if (IS_TOUCH) downsizeTexture(m.map)
        m.map.anisotropy = maxAniso      // crisp at grazing angles
      }
    })
    applyPhotoShading(scene, photoShadingRef.current)
    applyOpacity(scene, opacityRef.current)
    setRootLoaded(true)
    invalidate()
  }, [applyOpacity, invalidate, maxAniso])

  const onDisposeModel = useCallback(({ scene }: { scene: THREE.Object3D }) => {
    scene.traverse((o: any) => {
      if (!o.isMesh) return
      dropBVH(o.geometry)
      o.geometry.disposeBoundsTree?.()
      disposePhotoShading(o)     // the renderer disposes only the assigned material
    })
  }, [])

  // switch every loaded tile between photo (unlit) and lit relief shading
  useEffect(() => {
    const g = tilesRef.current?.group
    if (!g) return
    applyPhotoShading(g, photoShading)
    applyOpacity(g, opacityRef.current)
    invalidate()
  }, [photoShading, applyOpacity, invalidate])

  const onLoadError = useCallback((e: any) => {
    console.error('Načtení dlaždice selhalo:', e?.error ?? e)
    // only fatal if nothing is on screen yet (a missing leaf just stays coarse)
    if (!tilesRef.current?.root || !rootLoaded) setModelError('Nepodařilo se načíst 3D model (dlaždice).')
  }, [rootLoaded, setModelError])

  // Tablets: parse fewer tiles at once (each parse + GPU upload runs on the main
  // thread; five at a time froze touch input while zooming) and keep fewer
  // downloads in flight so the tiles in view arrive first.
  useEffect(() => {
    const t = tilesRef.current
    if (!t || !IS_TOUCH) return
    if (t.parseQueue) t.parseQueue.maxJobs = 2
    if (t.downloadQueue && 'maxJobsPerOrigin' in t.downloadQueue) t.downloadQueue.maxJobsPerOrigin = 8
  }, [versionedUrl])

  // Detail indicator for what the inspector is looking at — the surface at the
  // centre of the view (the far background legitimately stays coarse):
  // 'max' when that tile is a finest (photo-resolution) tile, 'loading' while
  // better tiles are still on the way, else 'view' (refined as far as this
  // zoom needs; zooming in loads more). Polled — no "settled" event exists.
  const setDetailStatus = useViewerStore(s => s.setDetailStatus)
  const camera = useThree(s => s.camera)
  useEffect(() => {
    const ray = new THREE.Raycaster()
    const centre = new THREE.Vector2(0, 0)
    const tick = () => {
      const t = tilesRef.current
      if (!t?.root || !rootLoaded) return
      const s = t.stats ?? {}
      const pending = (s.queued || 0) + (s.downloading || 0) + (s.parsing || 0)
      const byScene = new Map<THREE.Object3D, any>()
      for (const tile of t.visibleTiles ?? []) {
        const sc = tile.engineData?.scene ?? tile.cached?.scene
        if (sc) byScene.set(sc, tile)
      }
      let centreTile: any = null
      let texel: number | null = null
      if (byScene.size) {
        ray.setFromCamera(centre, camera)
        const hit = ray.intersectObjects([...byScene.keys()], true)[0]
        for (let o: THREE.Object3D | null = hit?.object ?? null; o; o = o.parent) {
          if (byScene.has(o)) { centreTile = byScene.get(o); break }
        }
        if (hit) texel = texelAt(hit)
      }
      let state: 'loading' | 'view' | 'max'
      if (centreTile && !(centreTile.children?.length)) state = 'max'
      else state = pending > 0 ? 'loading' : 'view'
      // 2 significant digits: no store churn from sub-percent changes
      setDetailStatus(inspectionId, { state, texel: texel ? Number(texel.toPrecision(2)) : null })
    }
    const h = window.setInterval(tick, 300)
    return () => { window.clearInterval(h); setDetailStatus(inspectionId, null) }
  }, [inspectionId, rootLoaded, setDetailStatus, camera])

  // overlay-mode fade applies to everything already loaded
  useEffect(() => {
    if (tilesRef.current) applyOpacity(tilesRef.current.group, baseOpacity)
    invalidate()
  }, [baseOpacity, applyOpacity, invalidate])

  // Snapshots (PDF close-ups) move the camera programmatically; tiles refine a
  // few frames later. Let callers wait until nothing is queued/downloading.
  useEffect(() => {
    const settle = async () => {
      const t0 = performance.now()
      let quiet = 0
      while (performance.now() - t0 < 15000) {
        invalidate()          // → a frame → tiles.update() for the current camera
        await new Promise(r => requestAnimationFrame(() => r(null)))
        await new Promise(r => setTimeout(r, 60))
        const s = tilesRef.current?.stats
        if (!s) return
        const busy = (s.queued || 0) + (s.downloading || 0) + (s.parsing || 0) > 0
        quiet = busy ? 0 : quiet + 1
        if (quiet >= 3) return
      }
    }
    setModelSettleFn(settle)
    return () => setModelSettleFn(null)
  }, [invalidate, setModelSettleFn])

  const handleClick = (e: any) => {
    if (!e.point) return
    if (e.delta > 4) return          // a drag, not a click
    e.stopPropagation()
    const pt = unrotate(e.point)
    if (onPick) { onPick(pt); return }

    if (tool === 'view') {
      const modelPoint = {
        x: (pt.x - offset.x) / scale,
        y: (pt.y - offset.y) / scale,
        z: (pt.z - offset.z) / scale,
      }
      const n = e.face?.normal
      const modelNormal = n ? { x: n.x, y: n.y, z: n.z } : null
      setClickedFaceInfo({
        u: e.uv?.x ?? 0.5, v: e.uv ? 1 - e.uv.y : 0.5, textureUrl: null,
        modelPoint, modelNormal, inspectionId,
      })
      return
    }
    if (tool === 'annotate') {
      setPendingPosition(pt)
      setAnnotationFormOpen(true)
    } else if (tool === 'measure') {
      addMeasurePoint(pt)
    } else if (tool === 'polygon') {
      addPolygonPoint(pt)
    }
  }

  const handlePointerMove = (e: any) => {
    if (tool === 'polygon' && e.point) setHoverPoint3D(unrotate(e.point))
  }
  const handlePointerLeave = () => {
    if (tool === 'polygon') setHoverPoint3D(null)
  }

  const mp = clickedFaceInfo?.inspectionId === inspectionId ? clickedFaceInfo?.modelPoint : null
  const retarget = useRetargetOnDoubleClick()

  return (
    <>
      <group
        scale={scale}
        position={offset}
        onClick={handleClick}
        onDoubleClick={retarget}
        onPointerMove={tool === 'polygon' ? handlePointerMove : undefined}
        onPointerLeave={tool === 'polygon' ? handlePointerLeave : undefined}
      >
        <TilesRenderer
          ref={tilesRef}
          url={versionedUrl}
          errorTarget={ERROR_TARGET}
          loadSiblings={!IS_TOUCH}
          lruCache-minBytesSize={CACHE_MIN}
          lruCache-maxBytesSize={CACHE_MAX}
          onLoadModel={onLoadModel}
          onDisposeModel={onDisposeModel}
          onLoadError={onLoadError}
        />
      </group>

      {tool === 'view' && mp && (
        <Html
          position={[mp.x * scale + offset.x, mp.y * scale + offset.y, mp.z * scale + offset.z]}
          center
          zIndexRange={[20, 0]}
          style={{ pointerEvents: 'none' }}
        >
          <div className="flex items-center justify-center w-6 h-6 rounded-full bg-brand-600 text-white shadow-lg ring-2 ring-white/80">
            <Eye size={14} strokeWidth={2.5} />
          </div>
        </Html>
      )}
    </>
  )
}
