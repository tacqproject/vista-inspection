import { useEffect } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { useViewerStore } from '../../store/viewerStore'
import type { Annotation, Vec3 } from '../../types'
import { SEVERITY_COLORS } from '../../types'
import { isPolygon } from './AnnotationPolygons'
import { surfaceNormalAt } from './meshNormal'

/**
 * Lives inside the R3F Canvas and registers two capture functions in the store,
 * called by the report dialog (requires `preserveDrawingBuffer`):
 *   • snapshotFn        — one overview shot with numbered defect markers
 *   • defectSnapshotsFn — a face-on close-up per defect (so ALL defects show in
 *                         the PDF, not only those visible from the overview)
 */
export default function SnapshotCapturer() {
  const { gl, scene, camera, controls } = useThree()
  const setSnapshotFn = useViewerStore(s => s.setSnapshotFn)
  const setDefectSnapshotsFn = useViewerStore(s => s.setDefectSnapshotsFn)

  useEffect(() => {
    const modelQuat = () => {
      const q = useViewerStore.getState().modelQuat
      return new THREE.Quaternion(q[0], q[1], q[2], q[3])
    }

    // ── overview: current view + numbered markers ───────────────────────────
    // a streamed (tiled) model refines after the camera moves — wait for detail
    const settle = async () => {
      const fn = useViewerStore.getState().modelSettleFn
      if (fn) await fn()
    }

    const capture = async (annotations: Annotation[]): Promise<string | null> => {
      try {
        await settle()
        gl.render(scene, camera)
        const out = drawMarkers(gl.domElement, annotations, camera, modelQuat())
        return out.toDataURL('image/png')
      } catch {
        return null
      }
    }

    // ── per defect: fly face-on, render, capture, restore ───────────────────
    const captureEach = async (annotations: Annotation[]): Promise<(string | null)[]> => {
      const cam = camera as THREE.PerspectiveCamera
      const savePos = cam.position.clone()
      const saveQuat = cam.quaternion.clone()
      const saveUp = cam.up.clone()
      // Frames rendered while waiting for tiles run OrbitControls.update(), which
      // re-aims the camera at the orbit target — point that target at the defect
      // for the duration so the view (and tile refinement) stays on it.
      const ctl = controls as any
      const saveTarget = ctl?.target?.clone?.()
      const Q = modelQuat()
      const results: (string | null)[] = []
      try {
        for (const a of annotations) {
          const mp = markerPoint(a)
          if (!mp) { results.push(null); continue }
          const target = new THREE.Vector3(mp.x, mp.y, mp.z).applyQuaternion(Q)

          let dir = surfaceNormalAt(scene, target)
          if (!dir || dir.lengthSq() < 1e-6) {
            dir = target.clone().normalize()                 // fallback: outward
            if (dir.lengthSq() < 1e-6) dir.set(0.4, 0.3, 1)
          }
          const radius = featureRadius(a)
          const fov = (cam.fov * Math.PI) / 180
          const dist = Math.min(Math.max((radius / Math.tan(fov / 2)) * 1.9, 0.4), 6)

          cam.up.set(Math.abs(dir.y) > 0.95 ? 1 : 0, Math.abs(dir.y) > 0.95 ? 0 : 1, 0)
          cam.position.copy(target).addScaledVector(dir, dist)
          cam.lookAt(target)
          cam.updateMatrixWorld()
          if (ctl?.target) ctl.target.copy(target)
          await settle()                 // stream in the close-up detail first
          cam.position.copy(target).addScaledVector(dir, dist)   // re-assert the exact
          cam.lookAt(target)                                      // pose before capture
          cam.updateMatrixWorld()

          gl.render(scene, camera)
          const out = drawMarkers(gl.domElement, [a], camera, Q)
          results.push(out.toDataURL('image/jpeg', 0.85))
        }
      } catch {
        // fall through to restore
      }
      // restore the interactive view
      if (saveTarget && ctl?.target) ctl.target.copy(saveTarget)
      cam.position.copy(savePos)
      cam.quaternion.copy(saveQuat)
      cam.up.copy(saveUp)
      cam.updateMatrixWorld()
      ;(controls as any)?.update?.()
      gl.render(scene, camera)
      return results
    }

    setSnapshotFn(capture)
    setDefectSnapshotsFn(captureEach)
    return () => { setSnapshotFn(null); setDefectSnapshotsFn(null) }
  }, [gl, scene, camera, controls, setSnapshotFn, setDefectSnapshotsFn])

  return null
}

/** Copy the WebGL canvas to a 2D canvas and draw a numbered marker per annotation. */
function drawMarkers(
  src: HTMLCanvasElement, annotations: Annotation[],
  camera: THREE.Camera, Q: THREE.Quaternion,
): HTMLCanvasElement {
  const W = src.width, H = src.height
  const out = document.createElement('canvas')
  out.width = W
  out.height = H
  const ctx = out.getContext('2d')!
  ctx.drawImage(src, 0, 0)

  const v = new THREE.Vector3()
  const r = Math.max(11, Math.round(W / 64))
  ctx.font = `bold ${Math.round(r * 1.1)}px sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.lineWidth = Math.max(2, r / 6)

  annotations.forEach((a, i) => {
    const p = markerPoint(a)
    if (!p) return
    v.set(p.x, p.y, p.z).applyQuaternion(Q).project(camera)   // model → world → NDC
    if (v.z > 1) return
    const x = (v.x * 0.5 + 0.5) * W
    const y = (1 - (v.y * 0.5 + 0.5)) * H
    if (x < -r || x > W + r || y < -r || y > H + r) return
    ctx.beginPath()
    ctx.arc(x, y, r, 0, Math.PI * 2)
    ctx.fillStyle = SEVERITY_COLORS[a.severity] ?? '#dc2626'
    ctx.fill()
    ctx.strokeStyle = '#ffffff'
    ctx.stroke()
    ctx.fillStyle = '#ffffff'
    ctx.fillText(String(i + 1), x, y + 1)
  })
  return out
}

/** Marker anchor: a point defect uses its position, a polygon its centroid. */
function markerPoint(a: Annotation): Vec3 | null {
  if (isPolygon(a.geometry_json)) {
    const pts = a.geometry_json as Vec3[]
    const n = pts.length
    if (!n) return null
    return {
      x: pts.reduce((s, p) => s + p.x, 0) / n,
      y: pts.reduce((s, p) => s + p.y, 0) / n,
      z: pts.reduce((s, p) => s + p.z, 0) / n,
    }
  }
  if (a.position_x != null && a.position_y != null && a.position_z != null) {
    return { x: a.position_x, y: a.position_y, z: a.position_z }
  }
  return null
}

/** Framing radius (scene units): a polygon's extent, or a fixed close-up for a point. */
function featureRadius(a: Annotation): number {
  if (isPolygon(a.geometry_json)) {
    const pts = a.geometry_json as Vec3[]
    const c = markerPoint(a)!
    let r = 0
    for (const p of pts) r = Math.max(r, Math.hypot(p.x - c.x, p.y - c.y, p.z - c.z))
    return r * 1.3 + 0.12
  }
  return 0.32
}
