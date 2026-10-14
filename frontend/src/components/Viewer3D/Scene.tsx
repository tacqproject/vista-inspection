import { useEffect, useMemo, useRef, useState, Suspense } from 'react'
import { useLoader, useThree } from '@react-three/fiber'
import { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader.js'
import { MTLLoader } from 'three/examples/jsm/loaders/MTLLoader.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { FBXLoader } from 'three/examples/jsm/loaders/FBXLoader.js'
import { PLYLoader } from 'three/examples/jsm/loaders/PLYLoader.js'
import { Detailed, Html } from '@react-three/drei'
import { unrotate, useRetargetOnDoubleClick } from './interaction'
import TilesModel, { tilesetUrlFor, useTileset } from './TilesModel'
import { Eye } from 'lucide-react'
import * as THREE from 'three'
import { computeBoundsTree, disposeBoundsTree, acceleratedRaycast } from 'three-mesh-bvh'

// BVH-accelerated raycasting: wheel zoom and clicks raycast the 600k-triangle
// mesh on every event — without a bounds tree that stutters badly.
;(THREE.BufferGeometry.prototype as any).computeBoundsTree = computeBoundsTree
;(THREE.BufferGeometry.prototype as any).disposeBoundsTree = disposeBoundsTree
THREE.Mesh.prototype.raycast = acceleratedRaycast
import { useViewerStore } from '../../store/viewerStore'
import { applyPhotoShading, disposePhotoShading } from './photoShading'
import type { ViewerTool } from '../../store/viewerStore'
import ScreenScaled from './ScreenScaled'

interface Props {
  modelPath?: string
  tool: ViewerTool
  inspectionId: string
  /** manual-align pick mode: model clicks report the scene-space point here */
  onPick?: (p: { x: number; y: number; z: number }) => void
  /** <1 fades this (reference) model so an overlaid re-scan stays visible */
  baseOpacity?: number
  /** model vertices are already baked into the structure's canonical scene frame
   *  → skip per-model normalization so all inspections share one frame */
  canonical?: boolean
}

export default function Scene({ modelPath, tool, inspectionId, onPick, baseOpacity = 1, canonical = false }: Props) {
  // Reconstructed models ship LOD siblings (model_lod0/1/2.gltf); imported
  // reality-scan meshes are a single file (glb/gltf/fbx/ply) → SingleMeshModel.
  const isReconstructedLod = !!modelPath && /lod\d\.gltf$/i.test(modelPath)
  // Prefer the streamed tileset when the backend has built one (big models stay
  // light when zoomed out, full detail streams in up close); otherwise the
  // whole-file glTF LODs.
  const tilesUrl = isReconstructedLod ? tilesetUrlFor(modelPath) : null
  const { data: tileset, isLoading: tilesetLoading } = useTileset(tilesUrl)

  if (!modelPath) {
    return <DemoBox tool={tool} />
  }
  if (isReconstructedLod && tilesetLoading) return null      // brief existence check
  if (tilesUrl && tileset) {
    return (
      <TilesModel url={tilesUrl} tileset={tileset} tool={tool} onPick={onPick}
        baseOpacity={baseOpacity} inspectionId={inspectionId} canonical={canonical} />
    )
  }

  const isSingleMesh = /\.(glb|gltf|fbx|ply)$/i.test(modelPath)
  return (
    <Suspense fallback={null}>
      {isReconstructedLod
        ? <GLTFLodModel modelPath={modelPath} tool={tool} onPick={onPick} baseOpacity={baseOpacity} inspectionId={inspectionId} canonical={canonical} />
        : isSingleMesh
        ? <SingleMeshModel modelPath={modelPath} tool={tool} onPick={onPick} baseOpacity={baseOpacity} inspectionId={inspectionId} />
        : <OBJModel modelPath={modelPath} tool={tool} onPick={onPick} baseOpacity={baseOpacity} inspectionId={inspectionId} />}
    </Suspense>
  )
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getTextureUrl(mesh: THREE.Mesh, faceMatIdx: number): string | null {
  const mat = Array.isArray(mesh.material) ? mesh.material[faceMatIdx] : mesh.material
  const map = (mat as any)?.map
  if (map?.userData?.uri) return map.userData.uri as string
  const img = map?.image as HTMLImageElement | null
  return img?.src ?? img?.currentSrc ?? null
}

/** Convert any loaded material to MeshStandardMaterial while preserving texture maps. */
function toStandard(mat: THREE.Material): THREE.MeshStandardMaterial {
  if (mat instanceof THREE.MeshStandardMaterial) return mat
  const phong = mat as THREE.MeshPhongMaterial
  // a photo texture is the true colour — AliceVision's MTL has Kd 0.6, which
  // would multiply (darken) it
  const map = (phong as any).map ?? null
  return new THREE.MeshStandardMaterial({
    name: mat.name,
    color: map ? new THREE.Color(0xffffff) : (phong as any).color ?? new THREE.Color(0xcbd5e1),
    map,
    normalMap: (phong as any).normalMap ?? null,
    roughness: 0.75,
    metalness: 0.05,
  })
}

function upgradeObj(obj: THREE.Group): { scale: number; offset: THREE.Vector3 } {
  const box = new THREE.Box3().setFromObject(obj)
  const center = box.getCenter(new THREE.Vector3())
  const size = box.getSize(new THREE.Vector3())
  const maxDim = Math.max(size.x, size.y, size.z)
  if (maxDim === 0) return { scale: 1, offset: new THREE.Vector3() }
  const scale = 5 / maxDim
  obj.scale.setScalar(scale)
  obj.position.sub(center.multiplyScalar(scale))

  obj.traverse(child => {
    if (!(child instanceof THREE.Mesh)) return

    if (Array.isArray(child.material)) {
      child.material = child.material.map(m => toStandard(m))
    } else if (child.material) {
      child.material = toStandard(child.material)
    } else {
      child.material = new THREE.MeshStandardMaterial({ color: '#cbd5e1', roughness: 0.75 })
    }
  })

  return { scale, offset: obj.position.clone() }
}

// ─── glTF LOD Model ──────────────────────────────────────────────────────────

/** Tag each material's texture with its source URI so clicks can resolve the photo. */
function tagTextureUris(gltf: any, baseUrl: string) {
  const json = gltf.parser?.json
  if (!json?.materials) return
  const uriByMatName = new Map<string, string>()
  for (const m of json.materials) {
    const texIdx = m.pbrMetallicRoughness?.baseColorTexture?.index
    if (texIdx == null) continue
    const srcIdx = json.textures?.[texIdx]?.source
    const uri = json.images?.[srcIdx]?.uri
    if (m.name && uri) uriByMatName.set(m.name, `${baseUrl}/${uri}`)
  }
  gltf.scene.traverse((child: any) => {
    if (!(child instanceof THREE.Mesh)) return
    const mats = Array.isArray(child.material) ? child.material : [child.material]
    for (const mat of mats) {
      const uri = uriByMatName.get(mat?.name)
      if (uri && mat.map) mat.map.userData.uri = uri
    }
  })
}

/** iOS/iPadOS detection — Safari on iPad reports itself as macOS. */
const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.userAgent.includes('Mac') && navigator.maxTouchPoints > 1)

/**
 * Free GPU resources of a loaded object. R3F does NOT dispose `<primitive>`
 * objects (they're externally owned), so switching models or unmounting would
 * otherwise leak geometry + (large-scan) textures on the GPU until the context
 * is lost. Used by the OBJ / reality-scan loaders, which own their objects.
 */
const _TEX_KEYS = ['map', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'emissiveMap', 'bumpMap', 'alphaMap']
function disposeObject3D(root: THREE.Object3D | null) {
  if (!root) return
  root.traverse((o: any) => {
    const geo = o.geometry
    if (geo) { geo.disposeBoundsTree?.(); geo.dispose?.() }
    disposePhotoShading(o)
    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : [])
    for (const m of mats) {
      for (const k of _TEX_KEYS) m[k]?.dispose?.()
      m.dispose?.()
    }
  })
}

function GLTFLodModel({ modelPath, tool, onPick, baseOpacity = 1, inspectionId, canonical = false }: {
  modelPath: string; tool: ViewerTool
  onPick?: (p: { x: number; y: number; z: number }) => void
  baseOpacity?: number
  inspectionId: string
  canonical?: boolean
}) {
  const {
    setPendingPosition, setAnnotationFormOpen,
    addMeasurePoint, addPolygonPoint, setHoverPoint3D,
    setClickedFaceInfo, clickedFaceInfo, setModelTransform,
  } = useViewerStore()

  // Constrained GPUs (iPads/phones, small maxTextureSize) get the 2K-texture
  // variant — decoding 8K tiles exceeds iOS Safari memory limits and kills
  // the tab. The *_mobile.gltf files share geometry with the full variant.
  const maxTex = useThree(s => s.gl.capabilities.maxTextureSize)
  const useMobile = IS_IOS || maxTex < 8192

  const baseUrl = `/uploads/${modelPath.substring(0, modelPath.lastIndexOf('/'))}`
  const lodPaths = [0, 1, 2].map(i => {
    let p = modelPath.replace(/lod\d/, `lod${i}`)
    if (useMobile) p = p.replace(/\.gltf$/i, '_mobile.gltf')
    return `/uploads/${p}`
  })
  const [lod0, lod1, lod2] = useLoader(GLTFLoader, lodPaths)
  const photoShading = useViewerStore(s => s.photoShading)
  const invalidatePS = useThree(s => s.invalidate)
  useEffect(() => {
    for (const g of [lod0, lod1, lod2]) applyPhotoShading(g.scene, photoShading)
    invalidatePS()
  }, [lod0, lod1, lod2, photoShading, invalidatePS])

  // Canonical (baked) models are already in the structure's scene frame — and
  // their annotations were migrated into that same frame — so they must render
  // at identity (no re-normalization), or model and annotations drift apart.
  // Non-canonical models get the usual centred 5-unit-box normalization.
  const { scale, offset, groundY } = useMemo(() => {
    const box = new THREE.Box3().setFromObject(lod0.scene)
    if (canonical) {
      return { scale: 1, offset: new THREE.Vector3(0, 0, 0), groundY: box.min.y }
    }
    const center = box.getCenter(new THREE.Vector3())
    const size = box.getSize(new THREE.Vector3())
    const maxDim = Math.max(size.x, size.y, size.z)
    const s = maxDim > 0 ? 5 / maxDim : 1
    const off = center.multiplyScalar(-s)
    return { scale: s, offset: off, groundY: box.min.y * s + off.y }
  }, [lod0, canonical])

  useEffect(() => {
    for (const gltf of [lod0, lod1, lod2]) {
      tagTextureUris(gltf, baseUrl)
    }
    // Raycasting must only hit the full-detail mesh: the raycaster ignores
    // LOD visibility, and simplified levels deviate from the true surface —
    // clicks would land off-surface and photo projection would be wrong.
    for (const gltf of [lod1, lod2]) {
      gltf.scene.traverse((child: any) => {
        if (child instanceof THREE.Mesh) child.raycast = () => null
      })
    }
    // BVH for the raycastable level (one-time, ~1 s for 600k triangles)
    lod0.scene.traverse((child: any) => {
      if (child instanceof THREE.Mesh && !(child.geometry as any).boundsTree) {
        (child.geometry as any).computeBoundsTree?.()
      }
    })
  }, [lod0, lod1, lod2])

  // Publish the normalization so panels can convert scene ↔ model coordinates
  useEffect(() => {
    setModelTransform({ scale, offset: { x: offset.x, y: offset.y, z: offset.z }, groundY })
    return () => setModelTransform(null)
  }, [scale, offset, groundY])

  // Fade the reference model in overlay mode so a re-scan sitting inside it
  // stays visible. Toggle per-material transparency; restore when fully opaque.
  const invalidate = useThree(s => s.invalidate)
  useEffect(() => {
    const faded = baseOpacity < 0.999
    for (const gltf of [lod0, lod1, lod2]) {
      gltf.scene.traverse((child: any) => {
        if (!(child instanceof THREE.Mesh)) return
        const mats = Array.isArray(child.material) ? child.material : [child.material]
        mats.forEach((m: any) => {
          if (m.userData.baseOpacity === undefined) m.userData.baseOpacity = m.opacity ?? 1
          m.transparent = faded
          m.opacity = faded ? baseOpacity : m.userData.baseOpacity
          m.depthWrite = !faded
          m.needsUpdate = true
        })
      })
    }
    invalidate()
  }, [lod0, lod1, lod2, baseOpacity, invalidate])

  const handleClick = (e: any) => {
    if (!e.point) return
    // R3F fires click after a drag too — ignore it, otherwise orbiting the
    // model would drop stray polygon/measure points
    if (e.delta > 4) return
    e.stopPropagation()
    const pt = unrotate(e.point)

    // manual-align pick mode takes over all clicks
    if (onPick) { onPick(pt); return }

    if (tool === 'view') {
      if (e.uv) {
        const textureUrl = getTextureUrl(e.object as THREE.Mesh, e.face?.materialIndex ?? 0)
        // Invert the display normalization → original model coordinates
        const modelPoint = {
          x: (pt.x - offset.x) / scale,
          y: (pt.y - offset.y) / scale,
          z: (pt.z - offset.z) / scale,
        }
        // uniform scale + translation preserve directions → face normal is
        // valid in the model frame as-is
        const n = e.face?.normal
        const modelNormal = n ? { x: n.x, y: n.y, z: n.z } : null
        setClickedFaceInfo({ u: e.uv.x, v: 1 - e.uv.y, textureUrl, modelPoint, modelNormal, inspectionId })
      }
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
    if (tool === 'polygon' && e.point) {
      setHoverPoint3D(unrotate(e.point))
    }
  }

  const handlePointerLeave = () => {
    if (tool === 'polygon') setHoverPoint3D(null)
  }

  // only mark the spot in the viewer the click belongs to — the store is shared
  // across compare panes, so an unscoped marker would float in the other model
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
        <Detailed distances={[0, 12, 25]}>
          <primitive object={lod0.scene} />
          <primitive object={lod1.scene} />
          <primitive object={lod2.scene} />
        </Detailed>
      </group>

      {/* Clicked-point marker: an eye badge marking the spot being looked up
          on the photos (constant screen size, always faces the camera) */}
      {tool === 'view' && mp && (
        <Html
          position={[
            mp.x * scale + offset.x,
            mp.y * scale + offset.y,
            mp.z * scale + offset.z,
          ]}
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

// ─── OBJ Model ───────────────────────────────────────────────────────────────

function OBJModel({ modelPath, tool, onPick, baseOpacity = 1, inspectionId }: {
  modelPath: string; tool: ViewerTool
  onPick?: (p: { x: number; y: number; z: number }) => void
  baseOpacity?: number
  inspectionId: string
}) {
  const {
    setPendingPosition, setAnnotationFormOpen,
    addMeasurePoint, addPolygonPoint, setHoverPoint3D,
    setClickedFaceInfo, setModelTransform, setModelError,
  } = useViewerStore()

  // OBJLoader does NOT load the MTL by itself — load it explicitly first.
  // The MTL is optional (imported models often lack it): a missing/broken
  // MTL must not crash the viewer, so load manually instead of useLoader.
  const [obj, setObj] = useState<THREE.Group | null>(null)
  const transform = useRef({ scale: 1, offset: new THREE.Vector3() })
  const photoShading = useViewerStore(s => s.photoShading)
  const invalidatePS = useThree(s => s.invalidate)
  useEffect(() => { applyPhotoShading(obj, photoShading); invalidatePS() }, [obj, photoShading, invalidatePS])

  // Publish the normalization once loaded → hides the loading overlay.
  useEffect(() => {
    if (!obj) return
    const { scale, offset } = transform.current
    const groundY = new THREE.Box3().setFromObject(obj).min.y
    setModelTransform({ scale, offset: { x: offset.x, y: offset.y, z: offset.z }, groundY })
    return () => setModelTransform(null)
  }, [obj, setModelTransform])

  // Release the previous object's GPU resources when it is replaced / unmounted.
  useEffect(() => () => disposeObject3D(obj), [obj])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const objLoader = new OBJLoader()
      try {
        const mtl = await new MTLLoader().loadAsync(
          `/uploads/${modelPath.replace(/\.obj$/i, '.mtl')}`,
        )
        mtl.preload()
        objLoader.setMaterials(mtl)
      } catch {
        // no MTL — model renders with default material
      }
      try {
        const loaded = await objLoader.loadAsync(`/uploads/${modelPath}`)
        if (cancelled) return
        transform.current = upgradeObj(loaded)
        setObj(loaded)
      } catch (err) {
        if (cancelled) return
        console.error('Nepodařilo se načíst OBJ model:', err)
        setModelError('Nepodařilo se načíst 3D model (OBJ).')
      }
    }
    setObj(null)
    setModelError(null)
    load()
    return () => { cancelled = true }
  }, [modelPath])

  const handleClick = (e: any) => {
    if (!e.point) return
    // R3F fires click after a drag too — ignore it, otherwise orbiting the
    // model would drop stray polygon/measure points
    if (e.delta > 4) return
    e.stopPropagation()
    const pt = unrotate(e.point)

    // manual-align pick mode takes over all clicks
    if (onPick) { onPick(pt); return }

    if (tool === 'view') {
      if (e.uv) {
        const textureUrl = getTextureUrl(e.object as THREE.Mesh, e.face?.materialIndex ?? 0)
        const { scale, offset } = transform.current
        const modelPoint = {
          x: (pt.x - offset.x) / scale,
          y: (pt.y - offset.y) / scale,
          z: (pt.z - offset.z) / scale,
        }
        // uniform scale + translation preserve directions → face normal is
        // valid in the model frame as-is
        const n = e.face?.normal
        const modelNormal = n ? { x: n.x, y: n.y, z: n.z } : null
        setClickedFaceInfo({ u: e.uv.x, v: 1 - e.uv.y, textureUrl, modelPoint, modelNormal, inspectionId })
      }
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
    if (tool === 'polygon' && e.point) {
      setHoverPoint3D(unrotate(e.point))
    }
  }

  const handlePointerLeave = () => {
    if (tool === 'polygon') setHoverPoint3D(null)
  }

  const retarget = useRetargetOnDoubleClick()

  const invalidate = useThree(s => s.invalidate)
  useEffect(() => {
    if (!obj) return
    const faded = baseOpacity < 0.999
    obj.traverse((child: any) => {
      if (!(child instanceof THREE.Mesh)) return
      const mats = Array.isArray(child.material) ? child.material : [child.material]
      mats.forEach((m: any) => {
        if (m.userData.baseOpacity === undefined) m.userData.baseOpacity = m.opacity ?? 1
        m.transparent = faded
        m.opacity = faded ? baseOpacity : m.userData.baseOpacity
        m.depthWrite = !faded
        m.needsUpdate = true
      })
    })
    invalidate()
  }, [obj, baseOpacity, invalidate])

  if (!obj) return null

  return (
    <primitive
      object={obj}
      onClick={handleClick}
      onDoubleClick={retarget}
      onPointerMove={tool === 'polygon' ? handlePointerMove : undefined}
      onPointerLeave={tool === 'polygon' ? handlePointerLeave : undefined}
    />
  )
}

/** Center + scale to the 5-unit box without touching materials (unlike upgradeObj). */
function normalizeInPlace(obj: THREE.Object3D): { scale: number; offset: THREE.Vector3 } {
  const box = new THREE.Box3().setFromObject(obj)
  const center = box.getCenter(new THREE.Vector3())
  const size = box.getSize(new THREE.Vector3())
  const maxDim = Math.max(size.x, size.y, size.z)
  if (maxDim === 0) return { scale: 1, offset: new THREE.Vector3() }
  const scale = 5 / maxDim
  obj.scale.setScalar(scale)
  obj.position.sub(center.multiplyScalar(scale))
  return { scale, offset: obj.position.clone() }
}

/**
 * Single-file imported model from a reality scan (RealityScan / Polycam /
 * Scaniverse …): GLB/glTF (self-contained), FBX, or PLY (mesh or point cloud).
 * Unlike GLTFLodModel there are no LOD siblings — one file, rendered directly.
 */
function SingleMeshModel({ modelPath, tool, onPick, baseOpacity = 1, inspectionId }: {
  modelPath: string; tool: ViewerTool
  onPick?: (p: { x: number; y: number; z: number }) => void
  baseOpacity?: number
  inspectionId: string
}) {
  const {
    setPendingPosition, setAnnotationFormOpen,
    addMeasurePoint, addPolygonPoint, setHoverPoint3D, setClickedFaceInfo,
    setModelTransform, setModelError,
  } = useViewerStore()
  const [obj, setObj] = useState<THREE.Object3D | null>(null)
  const transform = useRef({ scale: 1, offset: new THREE.Vector3() })
  const photoShading = useViewerStore(s => s.photoShading)
  const invalidatePS = useThree(s => s.invalidate)
  useEffect(() => { applyPhotoShading(obj, photoShading); invalidatePS() }, [obj, photoShading, invalidatePS])

  // Publish the normalization once loaded so the viewer knows the model is
  // actually on screen (hides the loading overlay) and panels can map coords.
  useEffect(() => {
    if (!obj) return
    const { scale, offset } = transform.current
    const groundY = new THREE.Box3().setFromObject(obj).min.y
    setModelTransform({ scale, offset: { x: offset.x, y: offset.y, z: offset.z }, groundY })
    return () => setModelTransform(null)
  }, [obj, setModelTransform])

  // Release the previous object's GPU resources when it is replaced / unmounted.
  useEffect(() => () => disposeObject3D(obj), [obj])

  useEffect(() => {
    let cancelled = false
    const url = `/uploads/${modelPath}`
    const ext = modelPath.split('.').pop()!.toLowerCase()

    const load = async () => {
      try {
        let object: THREE.Object3D
        if (ext === 'glb' || ext === 'gltf') {
          object = (await new GLTFLoader().loadAsync(url)).scene   // PBR materials kept
        } else if (ext === 'fbx') {
          object = await new FBXLoader().loadAsync(url)
          object.traverse((c: any) => {
            if (c.isMesh && c.material) {
              c.material = Array.isArray(c.material) ? c.material.map(toStandard) : toStandard(c.material)
            }
          })
        } else {   // ply — mesh (with vertex colours) or a raw point cloud
          const geo = await new PLYLoader().loadAsync(url)
          const hasColor = !!geo.getAttribute('color')
          if (geo.index || geo.getAttribute('normal')) {
            if (!geo.getAttribute('normal')) geo.computeVertexNormals()
            object = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
              vertexColors: hasColor, color: hasColor ? 0xffffff : 0xcbd5e1,
              roughness: 0.85, metalness: 0.02,
            }))
          } else {
            object = new THREE.Points(geo, new THREE.PointsMaterial({
              size: 0.02, sizeAttenuation: true,
              vertexColors: hasColor, color: hasColor ? 0xffffff : 0x94a3b8,
            }))
          }
        }
        if (cancelled) return
        transform.current = normalizeInPlace(object)
        // BVH so clicks/zoom stay responsive on heavy scan meshes
        object.traverse((c: any) => {
          if (c instanceof THREE.Mesh && !(c.geometry as any).boundsTree) {
            (c.geometry as any).computeBoundsTree?.()
          }
        })
        setObj(object)
      } catch (err) {
        if (cancelled) return
        console.error('Nepodařilo se načíst model reality scanu:', err)
        setModelError('Nepodařilo se načíst 3D model (reality scan).')
      }
    }
    setObj(null)
    setModelError(null)
    load()
    return () => { cancelled = true }
  }, [modelPath])

  const handleClick = (e: any) => {
    if (!e.point || e.delta > 4) return
    e.stopPropagation()
    const pt = unrotate(e.point)
    if (onPick) { onPick(pt); return }
    if (tool === 'view') {
      const textureUrl = getTextureUrl(e.object as THREE.Mesh, e.face?.materialIndex ?? 0)
      const { scale, offset } = transform.current
      const modelPoint = {
        x: (pt.x - offset.x) / scale,
        y: (pt.y - offset.y) / scale,
        z: (pt.z - offset.z) / scale,
      }
      const n = e.face?.normal
      setClickedFaceInfo({
        u: e.uv?.x ?? 0, v: e.uv ? 1 - e.uv.y : 0, textureUrl, modelPoint,
        modelNormal: n ? { x: n.x, y: n.y, z: n.z } : null, inspectionId,
      })
      return
    }
    if (tool === 'annotate') { setPendingPosition(pt); setAnnotationFormOpen(true) }
    else if (tool === 'measure') addMeasurePoint(pt)
    else if (tool === 'polygon') addPolygonPoint(pt)
  }

  const handlePointerMove = (e: any) => {
    if (tool === 'polygon' && e.point) setHoverPoint3D(unrotate(e.point))
  }
  const handlePointerLeave = () => { if (tool === 'polygon') setHoverPoint3D(null) }
  const retarget = useRetargetOnDoubleClick()

  const invalidate = useThree(s => s.invalidate)
  useEffect(() => {
    if (!obj) return
    const faded = baseOpacity < 0.999
    obj.traverse((child: any) => {
      if (!(child instanceof THREE.Mesh)) return
      const mats = Array.isArray(child.material) ? child.material : [child.material]
      mats.forEach((m: any) => {
        if (m.userData.baseOpacity === undefined) m.userData.baseOpacity = m.opacity ?? 1
        m.transparent = faded
        m.opacity = faded ? baseOpacity : m.userData.baseOpacity
        m.depthWrite = !faded
        m.needsUpdate = true
      })
    })
    invalidate()
  }, [obj, baseOpacity, invalidate])

  if (!obj) return null
  return (
    <primitive
      object={obj}
      onClick={handleClick}
      onDoubleClick={retarget}
      onPointerMove={tool === 'polygon' ? handlePointerMove : undefined}
      onPointerLeave={tool === 'polygon' ? handlePointerLeave : undefined}
    />
  )
}

// ─── Demo Box ─────────────────────────────────────────────────────────────────

function DemoBox({ tool }: { tool: ViewerTool }) {
  const {
    setPendingPosition, setAnnotationFormOpen,
    addMeasurePoint, addPolygonPoint, setHoverPoint3D,
    setClickedFaceInfo,
  } = useViewerStore()

  const handleClick = (e: any) => {
    if (!e.point) return
    // R3F fires click after a drag too — ignore it, otherwise orbiting the
    // model would drop stray polygon/measure points
    if (e.delta > 4) return
    e.stopPropagation()
    const pt = unrotate(e.point)

    if (tool === 'view') {
      // Demo box has no real textures — just record the click for the photo panel
      setClickedFaceInfo({ u: e.uv?.x ?? 0.5, v: 1 - (e.uv?.y ?? 0.5), textureUrl: null })
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
    if (tool === 'polygon' && e.point) {
      setHoverPoint3D(unrotate(e.point))
    }
  }

  const handlePointerLeave = () => {
    if (tool === 'polygon') setHoverPoint3D(null)
  }

  const concrete = new THREE.MeshStandardMaterial({ color: '#b0b8c4', roughness: 0.85, metalness: 0.02 })
  const roof = new THREE.MeshStandardMaterial({ color: '#64748b', roughness: 0.7, metalness: 0.05 })
  const glass = new THREE.MeshStandardMaterial({ color: '#93c5fd', roughness: 0.05, metalness: 0.4 })

  return (
    <group onClick={handleClick} onPointerMove={tool === 'polygon' ? handlePointerMove : undefined} onPointerLeave={tool === 'polygon' ? handlePointerLeave : undefined}>
      <mesh position={[0, 1.5, 0]} castShadow receiveShadow material={concrete}>
        <boxGeometry args={[2, 3, 2]} />
      </mesh>
      <mesh position={[0, 3.5, 0]} castShadow material={roof}>
        <coneGeometry args={[1.6, 1, 4]} />
      </mesh>
      {[[-0.5, 2, 1.01], [0.5, 2, 1.01], [-0.5, 1, 1.01], [0.5, 1, 1.01]].map(([x, y, z], i) => (
        <mesh key={i} position={[x as number, y as number, z as number]} material={glass}>
          <planeGeometry args={[0.4, 0.5]} />
        </mesh>
      ))}
    </group>
  )
}
