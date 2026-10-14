import { useEffect, useMemo, useRef } from 'react'
import { useThree, useFrame } from '@react-three/fiber'
import * as THREE from 'three'
import { useCameraSyncStore } from '../../store/cameraSyncStore'

/**
 * Locks the two side-by-side compare viewers onto the SAME physical spot. The
 * driver publishes its full pose (position, target, up) in A's scene frame; the
 * follower maps it into its own frame through the alignment matrix. Zoom onto a
 * place in A and B shows that same place. Both panes open framed on the region
 * B covers (via the alignment), so the shared area is on screen from the start.
 */
export default function CameraSync({ side }: { side: 'a' | 'b' }) {
  const camera = useThree(s => s.camera) as THREE.PerspectiveCamera
  const controls = useThree(s => s.controls) as any
  const invalidate = useThree(s => s.invalidate)

  const matrixBA = useCameraSyncStore(s => s.matrixBA)
  const driver = useCameraSyncStore(s => s.driver)
  const version = useCameraSyncStore(s => s.version)
  const publish = useCameraSyncStore(s => s.publish)

  // Point transforms own-frame ↔ A's frame, rotation quaternions for up, and a
  // shared initial pose (in A's frame) that frames the region B covers.
  const cfg = useMemo(() => {
    const I = new THREE.Matrix4(); const qi = new THREE.Quaternion()
    if (!matrixBA) return null
    const M = new THREE.Matrix4().fromArray(matrixBA).transpose()   // B_scene → A_scene
    const t = new THREE.Vector3(); const q = new THREE.Quaternion(); const sc = new THREE.Vector3()
    M.decompose(t, q, sc)
    const s = sc.x || 1
    const fov = (camera.fov * Math.PI) / 180
    const radiusA = 2.5 * s                          // B is a 5-unit box in its own frame
    const dist = (radiusA / Math.sin(fov / 2)) * 1.3
    const dir = new THREE.Vector3(1, 0.55, 1).normalize()
    const startTargetA = t.clone()
    const startPosA = t.clone().addScaledVector(dir, dist)
    if (side === 'a') return { toA: I, fromA: I.clone(), qToA: qi, qFromA: qi.clone(), startPosA, startTargetA }
    return { toA: M, fromA: M.clone().invert(), qToA: q, qFromA: q.clone().invert(), startPosA, startTargetA }
  }, [matrixBA, side, camera])

  // Apply a pose given in A's frame, mapped into this pane's frame.
  const applyA = (posA: THREE.Vector3, targetA: THREE.Vector3, upA: THREE.Vector3) => {
    if (!cfg || !controls?.target) return
    camera.position.copy(posA).applyMatrix4(cfg.fromA)
    controls.target.copy(targetA).applyMatrix4(cfg.fromA)
    camera.up.copy(upA).applyQuaternion(cfg.qFromA).normalize()
    const r = camera.position.distanceTo(controls.target)
    camera.near = Math.max(0.0005, r * 0.005)
    camera.updateProjectionMatrix()
    camera.lookAt(controls.target)
    controls.update()
    invalidate()
  }

  // Open framed on the shared region (same pose in A's frame for both panes).
  const framed = useRef(false)
  useEffect(() => {
    if (!cfg || framed.current || !controls?.target) return
    framed.current = true
    applyA(cfg.startPosA, cfg.startTargetA, new THREE.Vector3(0, 1, 0))
  }, [cfg, controls])              // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { framed.current = false }, [matrixBA])

  // world-up when this pane takes over, so it orbits naturally
  useEffect(() => { if (driver === side) camera.up.set(0, 1, 0) }, [driver, side, camera])

  const lastKey = useRef('')

  // DRIVER — publish full pose (position, target, up) in A's frame.
  useFrame(() => {
    if (!cfg || driver !== side || !controls?.target) return
    const pos = camera.position.clone().applyMatrix4(cfg.toA)
    const tgt = controls.target.clone().applyMatrix4(cfg.toA)
    const up = camera.up.clone().applyQuaternion(cfg.qToA).normalize()
    const key = [pos.x, pos.y, pos.z, tgt.x, tgt.y, tgt.z, up.x, up.y, up.z].map(v => v.toFixed(3)).join(',')
    if (key === lastKey.current) return
    lastKey.current = key
    publish({ x: pos.x, y: pos.y, z: pos.z }, { x: tgt.x, y: tgt.y, z: tgt.z }, { x: up.x, y: up.y, z: up.z })
  })

  // FOLLOWER — map the driver's pose into this frame and apply it.
  useEffect(() => {
    if (!cfg || driver === side || driver === null || !controls?.target) return
    const st = useCameraSyncStore.getState()
    if (!st.pos || !st.target || !st.up) return
    applyA(new THREE.Vector3(st.pos.x, st.pos.y, st.pos.z),
      new THREE.Vector3(st.target.x, st.target.y, st.target.z),
      new THREE.Vector3(st.up.x, st.up.y, st.up.z))
    lastKey.current = ''
  }, [version, driver, side, cfg, camera, controls, invalidate])   // eslint-disable-line react-hooks/exhaustive-deps

  return null
}
