import { useState } from 'react'
import * as THREE from 'three'
import { Html } from '@react-three/drei'
import type { Annotation } from '../../types'
import { SEVERITY_COLORS, DEFECT_LABELS, SEVERITY_LABELS } from '../../types'
import { useViewerStore } from '../../store/viewerStore'
import { isPolygon } from './AnnotationPolygons'
import ScreenScaled from './ScreenScaled'

interface Props {
  annotations: Annotation[]
}

export default function AnnotationMarkers({ annotations }: Props) {
  return (
    <>
      {annotations
        .filter(a =>
          a.position_x != null &&
          a.position_y != null &&
          a.position_z != null &&
          !isPolygon(a.geometry_json),
        )
        .map(a => (
          <AnnotationMarker key={a.id} annotation={a} />
        ))}
    </>
  )
}

function AnnotationMarker({ annotation: a }: { annotation: Annotation }) {
  const [hovered, setHovered] = useState(false)
  const [showTooltip, setShowTooltip] = useState(false)
  const { setSelectedAnnotation, selectedAnnotation, focusOn } = useViewerStore()
  const hoveredFromList = useViewerStore(s => s.hoveredAnnotationId === a.id)
  const tool = useViewerStore(s => s.tool)
  const isSelected = selectedAnnotation?.id === a.id
  const active = hovered || isSelected || hoveredFromList
  // Only capture clicks in view mode — while drawing/measuring the click must
  // land on the model to place a point, not select a nearby defect.
  const interactive = tool === 'view'

  const color = SEVERITY_COLORS[a.severity]
  const pos: [number, number, number] = [a.position_x!, a.position_y!, a.position_z!]

  return (
    <ScreenScaled position={pos}>
      {/* Visible marker (purely cosmetic — clicks are handled by the larger
          invisible hit sphere below) */}
      <mesh>
        <sphereGeometry args={[active ? 0.06 : 0.05, 16, 16]} />
        <meshStandardMaterial
          color={color}
          emissive={color}
          emissiveIntensity={active ? 0.9 : 0.3}
          roughness={0.3}
        />
      </mesh>

      {/* Enlarged invisible hit target. Its front hemisphere sits well in front
          of the surface the marker rests on, so the raycaster reaches it BEFORE
          the model behind — otherwise the model (being nearest) grabs the click
          and stopPropagation swallows it, and the defect "clicks through". */}
      {interactive && (
        <mesh
          onPointerEnter={() => { setHovered(true); setShowTooltip(true) }}
          onPointerLeave={() => { setHovered(false); setShowTooltip(false) }}
          onClick={(e) => {
            e.stopPropagation()
            setSelectedAnnotation(a)
            focusOn({ x: a.position_x!, y: a.position_y!, z: a.position_z! }, 0.22, true)
          }}
        >
          <sphereGeometry args={[0.14, 12, 12]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
      )}

      {/* Halo. When hovering a row in the list it shows THROUGH the model so a
          defect on the far side is locatable; otherwise it's depth-tested like
          the marker, so a selected defect under the roof doesn't bleed through. */}
      {active && (
        <mesh>
          <sphereGeometry args={[hoveredFromList && !isSelected ? 0.11 : 0.09, 16, 16]} />
          <meshBasicMaterial
            color={color}
            transparent
            opacity={0.28}
            depthTest={!(hoveredFromList && !isSelected) ? true : false}
          />
        </mesh>
      )}

      {/* Pulse ring */}
      <mesh rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[0.065, 0.085, 32]} />
        <meshBasicMaterial color={color} opacity={0.5} transparent side={THREE.DoubleSide} />
      </mesh>

      {/* Tooltip — constant small pixel size, crisp */}
      {(showTooltip || hoveredFromList) && (
        <Html style={{ pointerEvents: 'none' }} zIndexRange={[10, 0]}>
          <div className="bg-slate-900 text-white text-[11px] rounded-lg px-2 py-1 shadow-xl whitespace-nowrap -translate-x-1/2 -translate-y-full -mt-2">
            <p className="font-semibold">{DEFECT_LABELS[a.defect_type] ?? a.defect_type}</p>
            <p className="text-slate-300">{SEVERITY_LABELS[a.severity]}</p>
          </div>
        </Html>
      )}
    </ScreenScaled>
  )
}
