import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as THREE from 'three'
import { useViewerStore } from '../../store/viewerStore'

/**
 * Sparse SfM landmark cloud (Meshroom-style). Loads the compact binary from
 * the backend: [uint32 count][float32 xyz × count][uint8 rgb × count],
 * already in the mesh frame — only the display normalization applies here.
 */
export default function PointCloud({ inspectionId }: { inspectionId: string }) {
  const { modelTransform } = useViewerStore()

  const { data } = useQuery({
    queryKey: ['pointcloud', inspectionId],
    queryFn: async () => {
      const res = await fetch(`/api/inspections/${inspectionId}/pointcloud`)
      if (!res.ok) throw new Error('pointcloud unavailable')
      return res.arrayBuffer()
    },
    staleTime: Infinity,
    retry: false,
  })

  const geometry = useMemo(() => {
    if (!data) return null
    const count = new DataView(data).getUint32(0, true)
    const pos = new Float32Array(data, 4, count * 3)
    const col = new Uint8Array(data, 4 + count * 12, count * 3)
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3))
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3, true))
    return geo
  }, [data])

  if (!geometry || !modelTransform) return null
  const { scale, offset } = modelTransform

  return (
    <points
      geometry={geometry}
      scale={scale}
      position={[offset.x, offset.y, offset.z]}
      raycast={() => null}
    >
      <pointsMaterial size={2.5} sizeAttenuation={false} vertexColors />
    </points>
  )
}
