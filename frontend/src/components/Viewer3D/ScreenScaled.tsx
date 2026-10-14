import { useRef, ReactNode } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'

const tmp = new THREE.Vector3()

/**
 * Distance-aware sizing for markers/labels. Not fully screen-constant:
 * exponent 1.3 makes apparent size shrink as the camera gets close
 * (∝ d^0.3), so zoomed-in detail work isn't covered by huge markers,
 * while zoomed-out views still show them clearly.
 */
export default function ScreenScaled({ position, children }: {
  position: [number, number, number]
  children: ReactNode
}) {
  const ref = useRef<THREE.Group>(null)

  useFrame(({ camera }) => {
    if (!ref.current) return
    const d = camera.position.distanceTo(ref.current.getWorldPosition(tmp))
    const s = Math.pow(d / 8, 1.5)
    ref.current.scale.setScalar(THREE.MathUtils.clamp(s, 0.0005, 4))
  })

  return <group ref={ref} position={position}>{children}</group>
}
