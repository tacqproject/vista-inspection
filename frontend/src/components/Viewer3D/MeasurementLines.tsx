import { useMemo } from 'react'
import { Line, Html } from '@react-three/drei'
import type { Measurement } from '../../types'
import { useViewerStore } from '../../store/viewerStore'
import ScreenScaled from './ScreenScaled'
import CameraOffsetGroup from './CameraOffsetGroup'

interface Props {
  measurements: Measurement[]
  scaleFactor?: number
}

const AXIS_COLORS = { x: '#ef4444', y: '#22c55e', z: '#3b82f6' }   // CAD convention: X red, Y green, Z blue

export default function MeasurementLines({ measurements, scaleFactor = 1 }: Props) {
  const { measurePoints } = useViewerStore()

  return (
    <>
      {measurements
        .filter(m => m.points_json && Array.isArray(m.points_json) && m.points_json.length >= 2)
        .map(m => (
          <SavedMeasurementLine key={m.id} measurement={m} scaleFactor={scaleFactor} />
        ))}

      {measurePoints.length >= 1 && (
        <ActiveMeasurement points={measurePoints} scaleFactor={scaleFactor} />
      )}
    </>
  )
}

type P3 = [number, number, number]

/** Axis-aligned breakdown "staircase": p1 →X→ →Y→ →Z→ p2, colored per axis. */
function AxisBreakdown({ p1, p2, occlude = false }: { p1: P3; p2: P3; occlude?: boolean }) {
  const cornerA: P3 = [p2[0], p1[1], p1[2]]   // after X leg
  const cornerB: P3 = [p2[0], p2[1], p1[2]]   // after Y leg
  const seg = (a: P3, b: P3) => Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]) > 1e-6
  const dt = !occlude
  return (
    <>
      {seg(p1, cornerA) && (
        <Line points={[p1, cornerA]} color={AXIS_COLORS.x} lineWidth={1.5} dashed dashSize={0.05} gapSize={0.03} depthTest={dt} renderOrder={9} transparent opacity={0.8} />
      )}
      {seg(cornerA, cornerB) && (
        <Line points={[cornerA, cornerB]} color={AXIS_COLORS.y} lineWidth={1.5} dashed dashSize={0.05} gapSize={0.03} depthTest={dt} renderOrder={9} transparent opacity={0.8} />
      )}
      {seg(cornerB, p2) && (
        <Line points={[cornerB, p2]} color={AXIS_COLORS.z} lineWidth={1.5} dashed dashSize={0.05} gapSize={0.03} depthTest={dt} renderOrder={9} transparent opacity={0.8} />
      )}
    </>
  )
}

/** total + per-axis label */
function MeasureLabel({ mid, name, p1, p2, scaleFactor, color }: {
  mid: P3
  name?: string | null
  p1: P3
  p2: P3
  scaleFactor: number
  color: 'sky' | 'amber'
}) {
  const total = Math.hypot(p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]) * scaleFactor
  const dx = Math.abs(p2[0] - p1[0]) * scaleFactor
  const dy = Math.abs(p2[1] - p1[1]) * scaleFactor
  const dz = Math.abs(p2[2] - p1[2]) * scaleFactor
  const bg = color === 'sky' ? 'bg-sky-900/90 text-sky-100' : 'bg-amber-900/90 text-amber-100'

  return (
    <Html position={mid} style={{ pointerEvents: 'none' }} zIndexRange={[10, 0]}>
      <div className={`${bg} text-[11px] rounded px-1.5 py-0.5 whitespace-nowrap select-none -translate-x-1/2`}>
        {name ? <span className="font-semibold">{name}: </span> : null}
        <span className="font-semibold">{total.toFixed(3)} m</span>
        <span className="block text-[9px] opacity-85">
          <span style={{ color: '#fca5a5' }}>X {dx.toFixed(2)}</span>{' · '}
          <span style={{ color: '#86efac' }}>Y {dy.toFixed(2)}</span>{' · '}
          <span style={{ color: '#93c5fd' }}>Z {dz.toFixed(2)}</span>
        </span>
      </div>
    </Html>
  )
}

function EndPoint({ p, color, big = false, occlude = false }: { p: P3; color: string; big?: boolean; occlude?: boolean }) {
  return (
    <ScreenScaled position={p}>
      <mesh renderOrder={10}>
        <sphereGeometry args={[big ? 0.04 : 0.028, 8, 8]} />
        <meshBasicMaterial color={color} depthTest={!occlude} transparent />
      </mesh>
    </ScreenScaled>
  )
}

function SavedMeasurementLine({ measurement: m, scaleFactor }: {
  measurement: Measurement
  scaleFactor: number
}) {
  const active = useViewerStore(s => s.hoveredMeasurementId === m.id)
  const pts = m.points_json! as any[]
  const p1 = [pts[0].x, pts[0].y, pts[0].z] as P3
  const p2 = [pts[1].x, pts[1].y, pts[1].z] as P3
  const mid: P3 = [(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, (p1[2] + p2[2]) / 2]
  const color = active ? '#0ea5e9' : '#38bdf8'

  return (
    // occluded by the model (with a hair of camera-ward offset so it doesn't
    // z-fight the surface) — a measurement on the far side no longer shows
    // through
    <CameraOffsetGroup anchor={mid}>
      <Line points={[p1, p2]} color={color} lineWidth={active ? 4.5 : 2.5} renderOrder={9} />
      <AxisBreakdown p1={p1} p2={p2} occlude />
      <EndPoint p={p1} color={color} big={active} occlude />
      <EndPoint p={p2} color={color} big={active} occlude />
      <MeasureLabel mid={mid} name={m.name} p1={p1} p2={p2} scaleFactor={scaleFactor} color="sky" />
    </CameraOffsetGroup>
  )
}

function ActiveMeasurement({ points, scaleFactor }: {
  points: Array<{ position: { x: number; y: number; z: number }; index: number }>
  scaleFactor: number
}) {
  const p0 = points[0].position
  const p1 = points[1]?.position

  const pt0: P3 = [p0.x, p0.y, p0.z]
  const pt1: P3 = p1 ? [p1.x, p1.y, p1.z] : pt0
  const mid: P3 = [(pt0[0] + pt1[0]) / 2, (pt0[1] + pt1[1]) / 2, (pt0[2] + pt1[2]) / 2]

  return (
    <group>
      <EndPoint p={pt0} color="#fbbf24" />
      {p1 && (
        <>
          <Line points={[pt0, pt1]} color="#fbbf24" lineWidth={2.5} dashed dashSize={0.1} gapSize={0.05} depthTest={false} renderOrder={9} />
          <AxisBreakdown p1={pt0} p2={pt1} />
          <EndPoint p={pt1} color="#fbbf24" />
          <MeasureLabel mid={mid} p1={pt0} p2={pt1} scaleFactor={scaleFactor} color="amber" />
        </>
      )}
    </group>
  )
}
