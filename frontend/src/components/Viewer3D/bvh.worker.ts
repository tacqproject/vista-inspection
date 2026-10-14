// Builds the raycast BVH of a tile off the main thread (a 60–150k-triangle
// tile takes 50–200 ms on a tablet — a visible stutter per streamed tile).
import { BufferAttribute, BufferGeometry } from 'three'
import { MeshBVH } from 'three-mesh-bvh'

self.onmessage = (e: MessageEvent) => {
  const { id, position, index } = e.data as { id: number; position: Float32Array; index: Uint32Array | Uint16Array }
  try {
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(position, 3))
    g.setIndex(new BufferAttribute(index, 1))
    const bvh = new MeshBVH(g)
    const s = MeshBVH.serialize(bvh, { cloneBuffers: false })
    const transfer = [...s.roots, s.index.buffer] as ArrayBuffer[]
    ;(self as any).postMessage({ id, serialized: s }, transfer)
  } catch (err) {
    ;(self as any).postMessage({ id, error: String(err) })
  }
}
