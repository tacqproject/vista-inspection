import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useThree } from '@react-three/fiber'
import { Line, Html } from '@react-three/drei'
import * as THREE from 'three'
import { photosApi } from '../../api/inspections'
import { useViewerStore } from '../../store/viewerStore'

/**
 * Meshroom-style camera frustums: one wire pyramid per reconstructed photo.
 * Click opens the photo. Positions come from the backend already converted
 * to the mesh frame; here we only apply the display normalization.
 */
export default function CameraFrustums({ inspectionId }: { inspectionId: string }) {
  const { modelTransform, setCameraPhoto, cameraSize, setActiveCameraView, activeCameraView } = useViewerStore()

  const { data } = useQuery({
    queryKey: ['cameras', inspectionId],
    queryFn: () => photosApi.cameras(inspectionId),
    staleTime: Infinity,
    retry: false,
  })

  if (!data || !modelTransform) return null
  const { scale, offset } = modelTransform

  return (
    <>
      {/* While looking THROUGH a camera, hide all camera bodies: the active one
          would be rendered from the inside, and its neighbours stand centimetres
          away, filling the frame. Close the view (×) to get them back. */}
      {(activeCameraView ? [] : data.cameras)
        .map(cam => (
        <Frustum
          key={cam.filename}
          cam={cam}
          scale={scale}
          offset={offset}
          size={cameraSize}
          onOpen={() => setCameraPhoto(cam.filename)}
          onFly={() => setActiveCameraView(cam.filename, cam.pp, cam.fov_deg)}
        />
      ))}
    </>
  )
}

// frustum size comes from the store (user-adjustable slider)

function Frustum({ cam, scale, offset, size, onOpen, onFly }: {
  cam: { filename: string; position: [number, number, number]; forward: [number, number, number]; up: [number, number, number]; aspect: number; fov_deg: number }
  scale: number
  offset: { x: number; y: number; z: number }
  size: number
  onOpen: () => void
  onFly: () => void
}) {
  const [hovered, setHovered] = useState(false)
  const { camera, controls, invalidate } = useThree()

  /** Click: jump the viewer into this photo's exact viewpoint (pose + lens). */
  const flyTo = (e: any) => {
    e.stopPropagation()
    const ctl = controls as any
    if (!ctl) return
    // Snap EXACTLY onto the pose: with damping on, controls.update() would only
    // move part-way and the rest settles over the next frames — a little jerk
    // that breaks the photo alignment. Disable it just for the snap, then
    // restore (CameraViewAutoExit re-manages it while the view is locked).
    const wasDamping = ctl.enableDamping
    ctl.enableDamping = false
    // Frustums live inside the model re-orientation group, so their world pose
    // is rotated by modelQuat — apply the same rotation to the camera pose.
    const mq = useViewerStore.getState().modelQuat
    const Q = new THREE.Quaternion(mq[0], mq[1], mq[2], mq[3])
    camera.position.copy(apex.clone().applyQuaternion(Q))
    const fwd = new THREE.Vector3(...cam.forward).normalize().applyQuaternion(Q)
    // Honor the camera ROLL: OrbitControls re-aims with camera.up as the up
    // axis, so leaving it at world-Y would level the view — photos are shot
    // with a few degrees of roll (median 3.5°, up to 27° here), which alone
    // misaligns a photo overlay by ~a metre at the frame edges.
    camera.up.copy(new THREE.Vector3(...cam.up).normalize().applyQuaternion(Q))
    ctl.target.copy(camera.position.clone().addScaledVector(fwd, 1))
    if ((camera as THREE.PerspectiveCamera).isPerspectiveCamera) {
      (camera as THREE.PerspectiveCamera).fov = cam.fov_deg
      ;(camera as THREE.PerspectiveCamera).updateProjectionMatrix()
    }
    ctl.update()
    ctl.enableDamping = wasDamping
    invalidate()
    onFly()
  }

  const { apex, corners, quat } = useMemo(() => {
    const apex = new THREE.Vector3(
      cam.position[0] * scale + offset.x,
      cam.position[1] * scale + offset.y,
      cam.position[2] * scale + offset.z,
    )
    const fwd = new THREE.Vector3(...cam.forward).normalize()
    const up = new THREE.Vector3(...cam.up).normalize()
    const right = new THREE.Vector3().crossVectors(fwd, up).normalize()

    const h = Math.tan((cam.fov_deg * Math.PI / 180) / 2) * size
    const w = h * cam.aspect
    const center = apex.clone().addScaledVector(fwd, size)
    const corners = [
      center.clone().addScaledVector(right, -w).addScaledVector(up, h),
      center.clone().addScaledVector(right, w).addScaledVector(up, h),
      center.clone().addScaledVector(right, w).addScaledVector(up, -h),
      center.clone().addScaledVector(right, -w).addScaledVector(up, -h),
    ]
    // orientation of the camera body cube (aligned with the lens axes)
    const m = new THREE.Matrix4().makeBasis(right, up, fwd.clone().negate())
    const quat = new THREE.Quaternion().setFromRotationMatrix(m)

    return { apex, corners, quat }
  }, [cam, scale, offset, size])

  const color = hovered ? '#38bdf8' : '#64748b'
  const pts: [number, number, number][] = []
  const P = (v: THREE.Vector3): [number, number, number] => [v.x, v.y, v.z]
  // pyramid edges
  for (const c of corners) { pts.push(P(apex), P(c)) }
  // image-plane rectangle
  for (let i = 0; i < 4; i++) { pts.push(P(corners[i]), P(corners[(i + 1) % 4])) }
  // "up" tick on the top edge
  const topMid = corners[0].clone().add(corners[1]).multiplyScalar(0.5)
  const upTick = topMid.clone().add(
    topMid.clone().sub(apex).normalize().multiplyScalar(0.03),
  )
  pts.push(P(topMid), P(upTick))

  // shared shell material + body size (cameras stay small; the slider scales them)
  const body = Math.max(size * 0.09, 0.008)
  const shell = {
    color: hovered ? '#38bdf8' : '#cbd5e1',
    emissive: hovered ? '#0ea5e9' : '#0f172a',
    emissiveIntensity: hovered ? 0.6 : 0.2,
    roughness: 0.5,
    metalness: 0.15,
  }

  return (
    <group>
      <Line points={pts} segments color={color} lineWidth={hovered ? 2 : 1} transparent opacity={hovered ? 1 : 0.55} />
      {/* little 3D camera — the visible click target
          (local axes from `quat`: +X right, +Y up, −Z = viewing direction) */}
      <group
        position={apex}
        quaternion={quat}
        // onClick={e => { e.stopPropagation(); onOpen() }}
        // onClick={flyTo}
        onClick={(e) => {
        e.stopPropagation(); // Keep this if needed
        const nativeEvent = e.nativeEvent as MouseEvent;

        if (nativeEvent.ctrlKey || nativeEvent.metaKey) { // metaKey for Command on Mac
          // do something else (e.g., customAction)
          onOpen();
        } else {
          flyTo(e);
        }
      }}
        onDoubleClick={flyTo}
        onPointerEnter={() => setHovered(true)}
        onPointerLeave={() => setHovered(false)}
      >
        {/* body */}
        <mesh>
          <boxGeometry args={[body * 1.6, body, body * 0.85]} />
          <meshStandardMaterial {...shell} />
        </mesh>
        {/* lens barrel — points along the viewing direction */}
        <mesh position={[0, 0, -body * 0.75]} rotation={[Math.PI / 2, 0, 0]}>
          <cylinderGeometry args={[body * 0.36, body * 0.44, body * 0.7, 14]} />
          <meshStandardMaterial {...shell} />
        </mesh>
        {/* front glass */}
        <mesh position={[0, 0, -body * 1.11]} rotation={[Math.PI / 2, 0, 0]}>
          <cylinderGeometry args={[body * 0.3, body * 0.3, body * 0.04, 14]} />
          <meshStandardMaterial
            color={hovered ? '#e0f2fe' : '#334155'}
            emissive={hovered ? '#7dd3fc' : '#0f172a'}
            emissiveIntensity={hovered ? 0.9 : 0.4}
            roughness={0.15}
            metalness={0.6}
          />
        </mesh>
        {/* viewfinder bump on top */}
        <mesh position={[0, body * 0.62, body * 0.05]}>
          <boxGeometry args={[body * 0.55, body * 0.25, body * 0.5]} />
          <meshStandardMaterial {...shell} />
        </mesh>
      </group>
      {hovered && (
        <Html position={[apex.x, apex.y, apex.z]} style={{ pointerEvents: 'none' }} zIndexRange={[10, 0]}>
          <div className="bg-slate-900 text-white text-[10px] rounded px-1.5 py-0.5 whitespace-nowrap -translate-x-1/2 -translate-y-full -mt-1.5">
            {cam.filename}
          </div>
        </Html>
      )}
    </group>
  )
}
