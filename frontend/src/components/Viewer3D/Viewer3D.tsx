import { Component, Suspense, useEffect, useRef, useMemo, type ReactNode } from 'react'
import { Canvas, useThree, useFrame } from '@react-three/fiber'
import { OrbitControls, GizmoHelper, GizmoViewcube, Environment, Html, useProgress } from '@react-three/drei'
import { useQuery } from '@tanstack/react-query'
import * as THREE from 'three'
import { annotationsApi, measurementsApi } from '../../api/inspections'
import { useViewerStore } from '../../store/viewerStore'
import Scene from './Scene'
import CameraFrustums from './CameraFrustums'
import PointCloud from './PointCloud'
import SatelliteGround from './SatelliteGround'
import OverlayModel, { type OverlayCfg } from './OverlayModel'
import CameraSync from './CameraSync'
import AnnotationMarkers from './AnnotationMarkers'
import AnnotationPolygons from './AnnotationPolygons'
import MeasurementLines from './MeasurementLines'
import PolygonDrawing from './PolygonDrawing'
import SnapshotCapturer from './SnapshotCapturer'
import { surfaceNormalAt } from './meshNormal'

interface Props {
  inspectionId: string
  modelPath?: string
  scaleFactor?: number
  map?: {
    lat: number
    lon: number
    rotationDeg: number
    sizeMeters: number
    offsetX?: number
    offsetY?: number
    offsetZ?: number
    scalePct?: number
  } | null
  /** another inspection's model registered into this one's frame */
  overlay?: OverlayCfg | null
  /** manual-align pick mode: report clicked scene points; show numbered markers */
  onPick?: (p: { x: number; y: number; z: number }) => void
  pickMarkers?: Array<{ x: number; y: number; z: number }>
  pickColor?: string
  /** <1 fades this model so an overlaid re-scan stays visible */
  baseOpacity?: number
  /** side-by-side compare: sync this pane's camera with the other via alignment */
  cameraSync?: 'a' | 'b'
  /** model is baked into the structure's canonical frame → skip normalization */
  canonical?: boolean
}

/** Bridges the store's model transform into SatelliteGround config. */
function MapGround({ map, scaleFactor }: {
  map: NonNullable<Props['map']>
  scaleFactor: number
}) {
  const { modelTransform } = useViewerStore()
  if (!modelTransform) return null
  // The calibration factor maps SCENE units → meters (measurements are made
  // by clicking in the viewer, i.e. in scene space), so 1 m = 1/scaleFactor
  // scene units. Do NOT mix in the display normalization here — that's
  // already baked into the scene coordinates the calibration was made in.
  return (
    <SatelliteGround cfg={{
      ...map,
      metersToScene: 1 / (scaleFactor || 1),
      groundY: modelTransform.groundY,
    }} />
  )
}

/**
 * Predictable wheel zoom replacing OrbitControls' multiplicative dolly.
 * Step is proportional to the distance of the surface under the cursor but
 * with a floor, so it never crawls to a halt near the model ("Zeno zoom")
 * and never rockets when far away. Camera and orbit target move together
 * (fly-forward), keeping the motion linear in feel.
 */
function LinearZoom() {
  const { camera, gl, scene, controls, invalidate } = useThree()

  useEffect(() => {
    // touch devices zoom by pinch via OrbitControls — wheel handler is
    // desktop-only (attaching both would double-zoom on hybrid laptops)
    if (navigator.maxTouchPoints > 1) return
    const el = gl.domElement
    const raycaster = new THREE.Raycaster()
    const ndc = new THREE.Vector2()

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const ctl = controls as any
      if (!ctl) return

      // Looking through a camera: zoom the LENS (narrow the FOV) instead of
      // dollying. A pure FOV change scales the image about the optical axis,
      // so the photo overlay stays perfectly aligned at every depth — moving
      // the camera would introduce parallax and break the match.
      const vs = useViewerStore.getState()
      if (vs.activeCameraView && vs.activeCameraFov) {
        const step = e.deltaY < 0 ? 1.15 : 1 / 1.15
        const z = Math.min(Math.max(vs.cameraViewZoom * step, 1), 25)
        const baseHalf = Math.tan((vs.activeCameraFov * Math.PI / 180) / 2)
        const cam = camera as THREE.PerspectiveCamera
        cam.fov = (2 * Math.atan(baseHalf / z) * 180) / Math.PI
        cam.updateProjectionMatrix()
        vs.setCameraViewZoom(z)
        invalidate()
        return
      }

      const rect = el.getBoundingClientRect()
      ndc.set(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1,
      )
      raycaster.setFromCamera(ndc, camera)
      const hit = raycaster.intersectObjects(scene.children, true)[0]

      const dist = hit
        ? hit.distance
        : camera.position.distanceTo(ctl.target)
      const zoomIn = e.deltaY < 0

      // proportional step with a floor — linear feel near the surface
      let step = Math.max(dist * 0.22, 0.06)
      if (zoomIn && hit) step = Math.min(step, Math.max(dist - 0.04, 0))
      if (!zoomIn) step = Math.min(step, 8)

      const dir = raycaster.ray.direction.clone().multiplyScalar(zoomIn ? step : -step)
      camera.position.add(dir)
      if (hit) {
        // Pivot at the surface DEPTH but on the view axis: update() re-aims
        // the camera at the target, so an off-axis target (the cursor hit
        // itself) would rotate the view on every tick and the image would
        // jump. On-axis target keeps orientation — no jumping — while
        // rotation stays anchored at model depth.
        const fwd = camera.getWorldDirection(new THREE.Vector3())
        const depth = Math.max(hit.point.clone().sub(camera.position).dot(fwd), 0.05)
        ctl.target.copy(camera.position).addScaledVector(fwd, depth)
      } else {
        ctl.target.add(dir)
      }
      ctl.update()
      invalidate()
    }

    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [camera, gl, scene, controls, invalidate])

  return null
}

/**
 * WebGL context can be killed by the browser under GPU memory pressure
 * (big textures + full-res photo preloads). preventDefault on 'lost' allows
 * the browser to restore it; on 'restored' we force a re-render.
 */
function ContextLossRecovery() {
  const { gl, invalidate } = useThree()
  useEffect(() => {
    const el = gl.domElement
    const onLost = (e: Event) => { e.preventDefault() }
    const onRestored = () => { invalidate() }
    el.addEventListener('webglcontextlost', onLost)
    el.addEventListener('webglcontextrestored', onRestored)
    return () => {
      el.removeEventListener('webglcontextlost', onLost)
      el.removeEventListener('webglcontextrestored', onRestored)
    }
  }, [gl, invalidate])
  return null
}

/**
 * Flying into a camera sets camera.up to that lens' up vector (to honor roll).
 * When the camera view is closed, restore the world-up axis so normal orbiting
 * behaves as usual.
 */
function CameraUpReset() {
  const { camera, controls, invalidate } = useThree()
  const activeCameraView = useViewerStore(s => s.activeCameraView)
  useEffect(() => {
    if (activeCameraView) return
    camera.up.set(0, 1, 0)
    const cam = camera as THREE.PerspectiveCamera
    if (cam.isPerspectiveCamera) {
      cam.fov = 45          // viewer default (lens FOV/zoom was applied while inside)
      cam.updateProjectionMatrix()
    }
    ;(controls as any)?.update?.()
    invalidate()
  }, [activeCameraView, camera, controls, invalidate])
  return null
}

/**
 * Leaving the camera viewpoint: as soon as the user orbits or pans, the render
 * no longer matches the photo, so the camera view (and its overlay) is closed.
 * Wheel zoom is left alone — it keeps the optical axis and stays aligned.
 */
function CameraViewAutoExit() {
  const gl = useThree(s => s.gl)
  const controls = useThree(s => s.controls) as any
  const activeCameraView = useViewerStore(s => s.activeCameraView)
  const setActiveCameraView = useViewerStore(s => s.setActiveCameraView)

  useEffect(() => {
    if (!activeCameraView) return

    // Lock the view rock-solid onto the photo: a click has sub-pixel pointer
    // movement that OrbitControls would still apply as a tiny rotation (the 4px
    // auto-exit threshold below doesn't catch it), drifting the model under the
    // overlay a little with every click/interaction. Disable orbit/pan/damping
    // while locked; wheel zoom (LinearZoom → FOV) stays and keeps alignment.
    const prev = controls && {
      rotate: controls.enableRotate, pan: controls.enablePan, damp: controls.enableDamping,
    }
    if (controls) {
      controls.enableRotate = false
      controls.enablePan = false
      controls.enableDamping = false
    }

    const el = gl.domElement
    let down: { x: number; y: number } | null = null

    const onDown = (e: PointerEvent) => { down = { x: e.clientX, y: e.clientY } }
    const onMove = (e: PointerEvent) => {
      if (!down || e.buttons === 0) return
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) {
        down = null
        setActiveCameraView(null)          // a real drag means "leave this view"
      }
    }
    const onUp = () => { down = null }

    el.addEventListener('pointerdown', onDown)
    el.addEventListener('pointermove', onMove)
    el.addEventListener('pointerup', onUp)
    el.addEventListener('pointerleave', onUp)
    return () => {
      el.removeEventListener('pointerdown', onDown)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerup', onUp)
      el.removeEventListener('pointerleave', onUp)
      if (controls && prev) {
        controls.enableRotate = prev.rotate
        controls.enablePan = prev.pan
        controls.enableDamping = prev.damp
      }
    }
  }, [activeCameraView, gl, controls, setActiveCameraView])

  return null
}

/**
 * Flies the camera to a point requested from the defect/measurement list
 * (store.focusRequest). Keeps the current viewing direction — only repositions
 * and re-frames — so the move feels like a smooth push-in, not a disorienting
 * jump. `radius` sets how tightly the feature is framed.
 */
/**
 * Pick a view direction from which the feature is actually visible: the
 * surface normal first, then directions tilted up to 70° around it, scored by
 * how directly they face the surface. Each is tested with a ray from the
 * feature towards the camera position against the model meshes (BVH): a
 * direction is "clear" when nothing (another pillar, the deck) lies between.
 * If none is clear, the least-blocked one is used and the camera stops in
 * front of the obstacle. → [direction, usable distance]
 */
function clearViewpoint(scene: THREE.Object3D, target: THREE.Vector3, normal: THREE.Vector3,
                        dist: number, current: THREE.Vector3 | null): [THREE.Vector3, number] {
  const meshes: THREE.Object3D[] = []
  scene.traverse((o: any) => { if (o.isMesh && o.visible && o.geometry?.boundsTree) meshes.push(o) })
  if (!meshes.length) return [normal, dist]
  const n = normal.clone().normalize()
  // orthonormal basis around the normal
  const a = Math.abs(n.y) < 0.9 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(1, 0, 0)
  const u = new THREE.Vector3().crossVectors(n, a).normalize()
  const v = new THREE.Vector3().crossVectors(n, u).normalize()
  const cands: THREE.Vector3[] = [n.clone()]
  for (const tilt of [20, 40, 60, 70]) {
    const t = tilt * Math.PI / 180
    for (let k = 0; k < 12; k++) {
      const ph = k * Math.PI / 6
      cands.push(n.clone().multiplyScalar(Math.cos(t))
        .addScaledVector(u, Math.sin(t) * Math.cos(ph)).addScaledVector(v, Math.sin(t) * Math.sin(ph)).normalize())
    }
  }
  if (current && current.dot(n) > 0.2) cands.push(current.clone().normalize())
  const rc = new THREE.Raycaster() as any
  rc.firstHitOnly = true
  rc.far = dist
  const eps = Math.max(dist * 0.03, 1e-3)          // leave the defect's own surface
  let best: THREE.Vector3 | null = null, bestScore = -Infinity
  let fallback = n, fallbackFree = 0
  for (const d of cands) {
    rc.set(target.clone().addScaledVector(d, eps), d)
    const hit = rc.intersectObjects(meshes, false)[0]
    const free = hit ? hit.distance + eps : Infinity
    if (free >= dist) {
      // head-on preferred; a small bonus for staying near the current view
      const score = d.dot(n) + (current ? 0.15 * d.dot(current) : 0)
      if (score > bestScore) { bestScore = score; best = d }
    } else if (free > fallbackFree) {
      fallbackFree = free; fallback = d
    }
  }
  if (best) return [best, dist]
  return [fallback, Math.max(fallbackFree * 0.8, eps * 2)]
}

function CameraFocus({ scaleFactor = 1 }: { scaleFactor?: number }) {
  const { camera, controls, invalidate, scene } = useThree()
  const focusRequest = useViewerStore(s => s.focusRequest)
  const anim = useRef<{
    fromPos: THREE.Vector3; toPos: THREE.Vector3
    fromTarget: THREE.Vector3; toTarget: THREE.Vector3
    start: number; dur: number
  } | null>(null)

  useEffect(() => {
    const ctl = controls as any
    if (!focusRequest || !ctl) return

    // leaving a locked camera viewpoint — a flight would fight its constraints
    useViewerStore.getState().setActiveCameraView(null)

    // Focus points come in the un-rotated model frame — apply the display
    // re-orientation so we fly to where the feature actually renders.
    const q = useViewerStore.getState().modelQuat
    const Q = new THREE.Quaternion(q[0], q[1], q[2], q[3])
    const target = new THREE.Vector3(focusRequest.x, focusRequest.y, focusRequest.z).applyQuaternion(Q)

    let dir: THREE.Vector3 | null = null
    if (focusRequest.orient) {
      // Re-aim to look at the feature face-on. Prefer the ACTUAL surface normal
      // at the point (BVH closest face) — correct for a column etc.; fall back to
      // a supplied polygon plane normal, else the current direction.
      // face normals of photogrammetry meshes point OUTWARD, so this frames the
      // defect from the correct side regardless of the model centre (fixes the
      // "shows the far side of a column" case). No camera-relative sign flip —
      // that would send you to whichever side you happened to be viewing from.
      dir = surfaceNormalAt(scene, target)
      if (!dir && focusRequest.nx != null) {
        dir = new THREE.Vector3(focusRequest.nx, focusRequest.ny!, focusRequest.nz!).applyQuaternion(Q)
      }
    }
    if (!dir || dir.lengthSq() < 1e-6) {
      // keep the current view direction (target → camera), just move along it
      dir = camera.position.clone().sub(ctl.target)
    }
    if (dir.lengthSq() < 1e-6) dir.set(0.5, 0.4, 1)   // final fallback
    dir.normalize()

    // Use the fixed viewer FOV, NOT camera.fov: right after leaving a camera
    // view the lens FOV may still be the narrowed value (reset by a separate
    // effect that runs later), which would place this one flight too far/near.
    const fov = 45 * Math.PI / 180
    // no closer than ~1,5 m in reality (metric models), else the old 0,5 units
    const minDist = Math.abs(scaleFactor - 1) > 1e-9 ? 1.5 / scaleFactor : 0.5
    let dist = Math.min(Math.max(focusRequest.radius / Math.tan(fov / 2) * 1.8, minDist), 8)
    if (focusRequest.orient) {
      // don't park the camera behind another pillar: pick an unobstructed side
      const cur = camera.position.clone().sub(target)
      const [d2, free] = clearViewpoint(scene, target, dir, dist, cur.lengthSq() > 1e-9 ? cur.normalize() : null)
      dir = d2; dist = Math.min(dist, free)
    }

    anim.current = {
      fromPos: camera.position.clone(),
      toPos: target.clone().addScaledVector(dir, dist),
      fromTarget: ctl.target.clone(),
      toTarget: target,
      start: performance.now(),
      dur: 550,
    }
    invalidate()
  }, [focusRequest?.token])   // eslint-disable-line react-hooks/exhaustive-deps

  useFrame(() => {
    const a = anim.current
    const ctl = controls as any
    if (!a || !ctl) return
    const t = Math.min((performance.now() - a.start) / a.dur, 1)
    const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2   // easeInOutCubic
    camera.position.lerpVectors(a.fromPos, a.toPos, e)
    ctl.target.lerpVectors(a.fromTarget, a.toTarget, e)
    ctl.update()
    if (t < 1) invalidate()
    else anim.current = null
  })

  return null
}

/** Spinner + progress shown while the model (and textures) stream in. */

/** Numbered marker for a manual-align pick (constant screen size). */
function PickMarker({ position, n, color }: { position: [number, number, number]; n: number; color: string }) {
  return (
    <Html position={position} center zIndexRange={[30, 0]} style={{ pointerEvents: 'none' }}>
      <div
        className="flex items-center justify-center w-5 h-5 rounded-full text-white text-[11px] font-bold shadow ring-2 ring-white/80"
        style={{ background: color }}
      >
        {n}
      </div>
    </Html>
  )
}

/**
 * Catches a load error thrown from the model subtree (GLTFLoader via useLoader
 * throws to the nearest boundary). Without this the subtree suspends forever and
 * the loading overlay never clears. Keyed by modelPath so a new model remounts a
 * fresh boundary. OBJ/reality-scan loaders catch their own async errors and set
 * modelError directly — this covers the useLoader/Suspense path.
 */
class ModelErrorBoundary extends Component<{ onError: () => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(err: unknown) { console.error('Načtení 3D modelu selhalo:', err); this.props.onError() }
  render() { return this.state.failed ? null : this.props.children }
}

/**
 * Streamed models: tells the inspector whether the close-up is final — a
 * spinner while better tiles stream in, "best for this view" once refined to
 * the current zoom, and "full resolution" when only the finest tiles (the
 * photos' own resolution) are on screen, i.e. zooming won't sharpen it further.
 */
function DetailBadge({ inspectionId, scaleFactor }: { inspectionId: string; scaleFactor: number }) {
  const d = useViewerStore(s => s.detailStatus[inspectionId])
  if (!d) return null
  const st = d.state
  // texel in mm only for metrically scaled models (scaleFactor = m per scene unit)
  const mm = d.texel && Math.abs(scaleFactor - 1) > 1e-9 ? d.texel * scaleFactor * 1000 : null
  const res = mm ? ` · ${mm < 10 ? mm.toFixed(1).replace('.', ',') : Math.round(mm)} mm/px` : ''
  const cfg = {
    loading: { text: 'Načítám detail…', title: 'Stahuje se přesnější část modelu — obraz se ještě zostří.', cls: 'text-sky-200' },
    view: { text: 'Detail pro tento pohled', title: 'Načteno vše, co je pro tuto vzdálenost potřeba. Přiblížením se načte jemnější detail.', cls: 'text-slate-200' },
    max: { text: 'Plné rozlišení', title: 'Uprostřed pohledu je nejjemnější úroveň modelu (rozlišení fotek). Dalším přiblížením se už nezlepší. Číslo = velikost jednoho pixelu textury na povrchu.', cls: 'text-emerald-300' },
  }[st]
  return (
    <div
      className={`absolute left-3 bottom-3 z-10 flex items-center gap-2 rounded-full bg-slate-900/75 px-3 py-1.5 text-xs font-medium shadow ring-1 ring-white/10 select-none pointer-events-auto ${cfg.cls}`}
      title={cfg.title}
    >
      {st === 'loading'
        ? <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-500 border-t-sky-300" />
        : <span className={`inline-block h-2.5 w-2.5 rounded-full ${st === 'max' ? 'bg-emerald-400' : 'bg-slate-400'}`} />}
      {cfg.text}{st === 'max' ? res : ''}
    </div>
  )
}

export default function Viewer3D({ inspectionId, modelPath, scaleFactor = 1, map = null, overlay = null, onPick, pickMarkers, pickColor = '#22d3ee', baseOpacity = 1, cameraSync, canonical = false }: Props) {
  const { tool, showAnnotations, showMeasurements, showCameras, showPointCloud, focusOn, modelTransform, modelQuat, modelError, setModelError } = useViewerStore()
  // model isn't "displayed" until its component has loaded and published its
  // transform — covers the gap after download (texture decode / BVH) where the
  // spinner used to vanish but nothing was on screen yet.
  const { progress } = useProgress()
  const modelLoading = !!modelPath && !modelTransform && !modelError

  // A fresh model path is a fresh attempt — clear any prior load error so the
  // spinner (not the error box) shows while it loads.
  useEffect(() => { setModelError(null) }, [modelPath, setModelError])

  // A canonical (baked) model sits at its true, small, off-centre place inside
  // the reference frame — the default camera would leave it a distant speck. Once
  // it has mounted (modelTransform published), frame the whole canonical box via
  // the app's focus mechanism so the detail is on screen and in context.
  const focusedOnce = useRef(false)
  useEffect(() => {
    if (!canonical || !modelTransform || focusedOnce.current) return
    focusedOnce.current = true
    focusOn({ x: 0, y: 0, z: 0 }, 2.6)
  }, [canonical, modelTransform, focusOn])

  const { data: annotations = [] } = useQuery({
    queryKey: ['annotations', inspectionId],
    queryFn: () => annotationsApi.list(inspectionId),
    refetchInterval: 5000,
  })

  const { data: measurements = [] } = useQuery({
    queryKey: ['measurements', inspectionId],
    queryFn: () => measurementsApi.list(inspectionId),
  })

  // Stable identity: an inline object would be a NEW camera prop on every
  // re-render (annotations refetch every 5 s), and R3F re-applies it — snapping
  // the camera back to [5,5,5] and undoing any programmatic framing (e.g. the
  // canonical-model fit).
  const cameraCfg = useMemo(() => ({ position: [5, 5, 5] as [number, number, number], fov: 45, near: 0.01, far: 5000 }), [])

  return (
    <div className="viewer-canvas">
      <Canvas
        camera={cameraCfg}
        frameloop="demand"
        dpr={navigator.maxTouchPoints > 1 ? 1 : [1, 1.5]}
        gl={{
          antialias: navigator.maxTouchPoints <= 1,
          toneMapping: THREE.ACESFilmicToneMapping,
          outputColorSpace: THREE.SRGBColorSpace,
          preserveDrawingBuffer: true,   // needed to snapshot the canvas for PDF reports
        }}
        onCreated={({ gl }) => { gl.toneMappingExposure = 1.1 }}
        style={{ background: '#1e293b' }}
      >
        <Suspense fallback={null}>
          {/* Lighting */}
          <ambientLight intensity={0.35} />
          <directionalLight position={[10, 20, 10]} intensity={2.0} />
          <directionalLight position={[-8, 8, -8]} intensity={0.4} />
          <hemisphereLight args={['#c8d8f0', '#334155', 0.5]} />

          {/* Image-based lighting (IBL) — major quality boost for PBR materials */}
          <Environment preset="warehouse" background={false} />

          {/* Everything that lives in the model's frame rotates together via the
              re-orientation group (e.g. flipping a Z-up reality scan upright).
              The map (north-aligned) and camera-sync stay outside. */}
          <group quaternion={modelQuat}>
            {/* Model + interaction */}
            <ModelErrorBoundary key={modelPath} onError={() => setModelError('Nepodařilo se načíst 3D model.')}>
              <Scene
                modelPath={modelPath}
                tool={tool}
                inspectionId={inspectionId}
                onPick={onPick}
                baseOpacity={baseOpacity}
                canonical={canonical}
              />
            </ModelErrorBoundary>

            {/* Manual-align pick markers (numbered) */}
            {pickMarkers?.map((p, i) => (
              <PickMarker key={i} position={[p.x, p.y, p.z]} n={i + 1} color={pickColor} />
            ))}

            {/* Camera frustums (Meshroom-style) */}
            {showCameras && <CameraFrustums inspectionId={inspectionId} />}

            {/* Sparse SfM point cloud */}
            {showPointCloud && <PointCloud inspectionId={inspectionId} />}

            {/* Registered model of another inspection */}
            {overlay && <OverlayModel cfg={overlay} />}

            {/* In-progress polygon overlay */}
            <PolygonDrawing />

            {/* Saved annotations */}
            {showAnnotations && (
              <>
                <AnnotationMarkers annotations={annotations} />
                <AnnotationPolygons annotations={annotations} />
              </>
            )}

            {/* Measurement lines */}
            {showMeasurements && (
              <MeasurementLines measurements={measurements} scaleFactor={scaleFactor} />
            )}
          </group>

          {/* Satellite imagery under the model (stays north-aligned) */}
          {map && <MapGround map={map} scaleFactor={scaleFactor} />}

          {/* Side-by-side camera sync through the alignment */}
          {cameraSync && <CameraSync side={cameraSync} />}

          <OrbitControls
            makeDefault
            // No damping: with frameloop="demand" a decaying damping residual
            // gets nudged on every unrelated invalidate (hovering a defect
            // outline, switching tabs, a query refetch) and drifts the view.
            // A hard stop is also better for lining up an exact inspection angle.
            enableDamping={false}
            enableZoom={navigator.maxTouchPoints > 1}   // pinch on touch; wheel via LinearZoom
            zoomToCursor
            minDistance={0.02}
            maxDistance={60}
          />
          <SnapshotCapturer />
          <LinearZoom />
          <CameraFocus scaleFactor={scaleFactor} />
          <ContextLossRecovery />
          <CameraUpReset />
          <CameraViewAutoExit />

          <GizmoHelper alignment="bottom-right" margin={[80, 80]}>
            <GizmoViewcube />
          </GizmoHelper>
        </Suspense>
      </Canvas>

      <DetailBadge inspectionId={inspectionId} scaleFactor={scaleFactor} />

      {/* No model placeholder */}
      {!modelPath && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <div className="text-center text-slate-400">
            <p className="text-sm">Žádný 3D model</p>
            <p className="text-xs mt-1 text-slate-500">Nahrajte snímky pro rekonstrukci</p>
          </div>
        </div>
      )}

      {/* Loading overlay — stays up through download AND the post-download
          processing (texture decode / BVH) until the model is actually on screen */}
      {modelLoading && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none bg-slate-900/45">
          <div className="flex flex-col items-center gap-3 select-none">
            <div className="animate-spin rounded-full h-10 w-10 border-[3px] border-slate-500 border-t-sky-400" />
            <p className="text-slate-300 text-xs whitespace-nowrap">
              {progress < 100 ? `Načítám model… ${progress.toFixed(0)} %` : 'Zpracovávám model…'}
            </p>
          </div>
        </div>
      )}

      {/* Load failed — show a message instead of spinning forever */}
      {!!modelPath && modelError && (
        <div className="absolute inset-0 flex items-center justify-center p-4 bg-slate-900/55">
          <div className="max-w-xs text-center text-slate-200 bg-slate-800/90 rounded-lg px-4 py-3 shadow-lg ring-1 ring-white/10">
            <p className="text-sm font-medium">{modelError}</p>
            <p className="text-xs mt-1 text-slate-400">
              Zkuste stránku načíst znovu, nebo model znovu nahrát / rekonstruovat.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
