import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { useViewerStore } from '../../store/viewerStore'

/** Map a clicked world point back through the model re-orientation, so stored
 *  coords/annotations stay in the un-rotated frame the model group rotates. */
export function unrotate(p: { x: number; y: number; z: number }): { x: number; y: number; z: number } {
  const q = useViewerStore.getState().modelQuat
  if (q[0] === 0 && q[1] === 0 && q[2] === 0 && q[3] === 1) return { x: p.x, y: p.y, z: p.z }
  const v = new THREE.Vector3(p.x, p.y, p.z).applyQuaternion(
    new THREE.Quaternion(q[0], q[1], q[2], q[3]).invert(),
  )
  return { x: v.x, y: v.y, z: v.z }
}

/**
 * Double-click re-anchors the orbit/zoom center onto the clicked surface
 * point. OrbitControls' zoom step is proportional to the distance to its
 * target — with the target far behind the surface, close-up zooming crawls.
 */
export function useRetargetOnDoubleClick() {
  const controls = useThree(s => s.controls) as any
  return (e: any) => {
    if (!e.point || !controls) return
    e.stopPropagation()
    controls.target.copy(e.point)
    controls.update()
  }
}

