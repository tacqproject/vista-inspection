import { useMemo, useState } from 'react'
import * as THREE from 'three'
import { Line } from '@react-three/drei'
import { useViewerStore } from '../../store/viewerStore'
import type { Vec3 } from '../../types'
import ScreenScaled from './ScreenScaled'

export default function PolygonDrawing() {
  const { polygonPoints, hoverPoint3D, tool, requestPolygonClose } = useViewerStore()
  const [hoverFirst, setHoverFirst] = useState(false)

  // useMemo must be called unconditionally — before any early return
  const fillGeo = useMemo(
    () => polygonPoints.length >= 3 ? fanTriangulate(polygonPoints) : null,
    [polygonPoints],
  )

  if (tool !== 'polygon' || polygonPoints.length === 0) return null

  const linePoints = polygonPoints.map(p => [p.x, p.y, p.z] as [number, number, number])
  const last = linePoints[linePoints.length - 1]

  return (
    <group>
      {/* Semi-transparent fill preview */}
      {fillGeo && (
        <mesh geometry={fillGeo} renderOrder={1}>
          <meshBasicMaterial color="#f59e0b" opacity={0.22} transparent side={THREE.DoubleSide} depthWrite={false} />
        </mesh>
      )}

      {/* Edges drawn so far — depthTest off so they stay visible through the
          model while drawing, just like a saved polygon's outline */}
      {linePoints.length >= 2 && (
        <Line points={linePoints} color="#f59e0b" lineWidth={3} depthTest={false} renderOrder={6} transparent />
      )}

      {/* Closing edge preview (last → first), shown when ≥ 3 pts */}
      {linePoints.length >= 3 && (
        <Line
          points={[last, linePoints[0]]}
          color="#f59e0b"
          lineWidth={2}
          dashed
          dashSize={0.06}
          gapSize={0.04}
          depthTest={false}
          renderOrder={6}
          transparent
        />
      )}

      {/* Preview edge following mouse */}
      {hoverPoint3D && (
        <Line
          points={[last, [hoverPoint3D.x, hoverPoint3D.y, hoverPoint3D.z]]}
          color="#fbbf24"
          lineWidth={2}
          dashed
          dashSize={0.06}
          gapSize={0.04}
          depthTest={false}
          renderOrder={6}
          transparent
        />
      )}

      {/* Vertex spheres — first vertex is the clickable "close polygon" handle.
          All drawn depth-test-free so no vertex disappears behind the surface */}
      {polygonPoints.map((p, i) => {
        const isFirst = i === 0
        const canClose = isFirst && polygonPoints.length >= 3
        return (
          <ScreenScaled key={i} position={[p.x, p.y, p.z]}>
            <mesh
              renderOrder={7}
              onClick={canClose ? (e: any) => { e.stopPropagation(); requestPolygonClose() } : undefined}
              onPointerEnter={canClose ? () => setHoverFirst(true) : undefined}
              onPointerLeave={canClose ? () => setHoverFirst(false) : undefined}
            >
              {/* enlarged, glowing when it can close — signals the click target */}
              <sphereGeometry args={[canClose ? (hoverFirst ? 0.075 : 0.055) : 0.038, 12, 12]} />
              <meshBasicMaterial
                color={isFirst ? '#10b981' : '#f59e0b'}
                depthTest={false}
                transparent
              />
            </mesh>
            {canClose && (
              <mesh renderOrder={7}>
                <ringGeometry args={[0.085, 0.105, 24]} />
                <meshBasicMaterial color="#10b981" transparent opacity={hoverFirst ? 0.9 : 0.45} depthTest={false} side={THREE.DoubleSide} />
              </mesh>
            )}
          </ScreenScaled>
        )
      })}
    </group>
  )
}

function fanTriangulate(pts: Vec3[]): THREE.BufferGeometry {
  const n = pts.length
  let cx = 0, cy = 0, cz = 0
  for (const p of pts) { cx += p.x; cy += p.y; cz += p.z }
  cx /= n; cy /= n; cz /= n

  const verts: number[] = []
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    verts.push(cx, cy, cz, pts[i].x, pts[i].y, pts[i].z, pts[j].x, pts[j].y, pts[j].z)
  }

  const geo = new THREE.BufferGeometry()
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(verts), 3))
  geo.computeVertexNormals()
  return geo
}
