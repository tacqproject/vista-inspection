import { useRef } from 'react'
import type { ReactNode } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'

/**
 * Nudges its children a hair toward the camera every frame. Overlays that hug
 * the mesh surface (measurement lines/dots, polygon outlines) would z-fight the
 * surface they sit on if depth-tested straight — so they used depthTest=false
 * and bled through the model when viewed from the far side (a defect under the
 * roof showing on top of the roof).
 *
 * With this tiny offset the overlay sits just in front of its own surface (no
 * z-fight, still readable) while geometry clearly in front still occludes it —
 * so children can keep depthTest ON and no longer show through the model.
 *
 * `anchor` is a representative world point of the overlay (a line's midpoint, a
 * polygon's centroid); the offset is a small fraction of the viewing distance,
 * so it stays visually negligible at every zoom.
 */
export default function CameraOffsetGroup({ anchor, factor = 0.006, children }: {
  anchor: [number, number, number]
  factor?: number
  children: ReactNode
}) {
  const ref = useRef<THREE.Group>(null)
  const v = useRef(new THREE.Vector3())

  useFrame(({ camera }) => {
    if (!ref.current) return
    v.current
      .set(camera.position.x - anchor[0], camera.position.y - anchor[1], camera.position.z - anchor[2])
      .multiplyScalar(factor)
    ref.current.position.copy(v.current)
  })

  return <group ref={ref}>{children}</group>
}
