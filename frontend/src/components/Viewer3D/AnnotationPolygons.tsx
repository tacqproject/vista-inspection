import { useMemo, useState } from 'react'
import * as THREE from 'three'
import { Line, Html } from '@react-three/drei'
import type { Annotation, Vec3 } from '../../types'
import { DEFECT_LABELS, SEVERITY_COLORS, SEVERITY_LABELS } from '../../types'
import { useViewerStore } from '../../store/viewerStore'
import ScreenScaled from './ScreenScaled'
import CameraOffsetGroup from './CameraOffsetGroup'

interface Props {
  annotations: Annotation[]
}

export default function AnnotationPolygons({ annotations }: Props) {
  return (
    <>
      {annotations
        .filter(a => isPolygon(a.geometry_json))
        .map(a => (
          <AnnotationPolygon key={a.id} annotation={a} />
        ))}
    </>
  )
}

export function isPolygon(g: unknown): g is Vec3[] {
  return Array.isArray(g) && g.length >= 3
}

/** Plane normal of a (possibly non-planar) polygon via Newell's method. */
function newellNormal(pts: Vec3[]): Vec3 {
  let nx = 0, ny = 0, nz = 0
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i], q = pts[(i + 1) % pts.length]
    nx += (p.y - q.y) * (p.z + q.z)
    ny += (p.z - q.z) * (p.x + q.x)
    nz += (p.x - q.x) * (p.y + q.y)
  }
  const len = Math.hypot(nx, ny, nz) || 1
  return { x: nx / len, y: ny / len, z: nz / len }
}

function AnnotationPolygon({ annotation: a }: { annotation: Annotation }) {
  const [hovered, setHovered] = useState(false)
  const { setSelectedAnnotation, selectedAnnotation, focusOn } = useViewerStore()
  const hoveredFromList = useViewerStore(s => s.hoveredAnnotationId === a.id)
  const tool = useViewerStore(s => s.tool)
  // only capture clicks in view mode — while drawing/measuring the click must
  // reach the model to place a point
  const interactive = tool === 'view'

  const pts = a.geometry_json as Vec3[]
  const color = SEVERITY_COLORS[a.severity]
  const isSelected = selectedAnnotation?.id === a.id
  const active = hovered || isSelected || hoveredFromList

  const { fillGeo, labelAnchor, outline, centroid, radius } = useMemo(() => {
    const n = pts.length
    let cx = 0, cy = 0, cz = 0
    let top = pts[0]
    for (const p of pts) {
      cx += p.x; cy += p.y; cz += p.z
      if (p.y > top.y) top = p
    }
    cx /= n; cy /= n; cz /= n

    const verts: number[] = []
    let rad = 0
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n
      verts.push(cx, cy, cz, pts[i].x, pts[i].y, pts[i].z, pts[j].x, pts[j].y, pts[j].z)
      rad = Math.max(rad, Math.hypot(pts[i].x - cx, pts[i].y - cy, pts[i].z - cz))
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(verts), 3))
    geo.computeVertexNormals()

    const outline = [...pts, pts[0]].map(p => [p.x, p.y, p.z] as [number, number, number])
    // label above the polygon's highest vertex — never over the studied area
    const labelAnchor = [cx, top.y, cz] as [number, number, number]
    return { fillGeo: geo, labelAnchor, outline, centroid: { x: cx, y: cy, z: cz }, radius: rad }
  }, [pts])

  return (
    <group
      onPointerEnter={interactive ? () => setHovered(true) : undefined}
      onPointerLeave={interactive ? () => setHovered(false) : undefined}
      onClick={interactive ? e => {
        e.stopPropagation()
        setSelectedAnnotation(isSelected ? null : a)
        focusOn(centroid, radius * 1.3, true, newellNormal(pts))
      } : undefined}
    >
      {/* Everything that hugs the surface is nudged a hair toward the camera and
          depth-tested: it sits just in front of its own surface (so the outline
          isn't lost behind the wall it's on) yet is still hidden by geometry in
          front — a polygon on the far side no longer bleeds through the model.
          The tiny offset also lets the invisible fill win clicks over the mesh. */}
      <CameraOffsetGroup anchor={[centroid.x, centroid.y, centroid.z]}>
        {/* Invisible fill — the click/hover target; no visible fill so the
            studied surface stays fully readable */}
        <mesh geometry={fillGeo} renderOrder={5}>
          <meshBasicMaterial
            color={color}
            opacity={0}
            transparent
            side={THREE.DoubleSide}
            depthWrite={false}
            depthTest={false}
          />
        </mesh>

        {/* Outline — the only visible part; highlighted on hover/selection */}
        <Line
          points={outline}
          color={color}
          lineWidth={active ? 4.5 : 3}
          renderOrder={6}
          transparent
          opacity={active ? 1 : 0.85}
        />

        {/* Vertex dots — distance-scaled like all other markers */}
        {pts.map((p, i) => (
          <ScreenScaled key={i} position={[p.x, p.y, p.z]}>
            <mesh>
              <sphereGeometry args={[0.022, 8, 8]} />
              <meshBasicMaterial color={color} />
            </mesh>
          </ScreenScaled>
        ))}
      </CameraOffsetGroup>

      {/* Tooltip — anchored above the polygon's top edge, outside the area */}
      {active && (
        <Html position={labelAnchor} style={{ pointerEvents: 'none' }} zIndexRange={[10, 0]}>
          <div className="bg-slate-900 text-white text-[11px] rounded-lg px-2 py-1 shadow-xl whitespace-nowrap -translate-x-1/2 -translate-y-full -mt-3">
            <p className="font-semibold">{DEFECT_LABELS[a.defect_type] ?? a.defect_type}</p>
            <p className="text-slate-300">{SEVERITY_LABELS[a.severity]}</p>
            <p className="text-slate-400 text-[10px]">Polygon · {pts.length} bodů</p>
          </div>
        </Html>
      )}
    </group>
  )
}
