import * as THREE from 'three'
import { MeshBVH } from 'three-mesh-bvh'

/**
 * Raycast BVHs for streamed tiles, built in web workers. The geometry stays
 * renderable meanwhile (copies are sent); the BVH's re-ordered index replaces
 * the tile's index when it arrives. Until then a click falls back to the plain
 * (slower) raycast — still correct.
 */
const N_WORKERS = Math.max(1, Math.min(2, (navigator.hardwareConcurrency || 2) - 1))
let workers: Worker[] | null = null
let idle: Worker[] = []
const queue: { id: number; geo: THREE.BufferGeometry }[] = []
const pending = new Map<number, THREE.BufferGeometry>()
const dropped = new WeakSet<THREE.BufferGeometry>()
let nextId = 1

function init() {
  if (workers) return
  try {
    workers = Array.from({ length: N_WORKERS }, () =>
      new Worker(new URL('./bvh.worker.ts', import.meta.url), { type: 'module' }))
  } catch {
    workers = []
  }
  for (const w of workers) {
    w.onmessage = (e: MessageEvent) => {
      const { id, serialized } = e.data
      const geo = pending.get(id)
      pending.delete(id)
      if (geo && serialized && !dropped.has(geo) && !(geo as any).boundsTree) {
        ;(geo as any).boundsTree = MeshBVH.deserialize(serialized, geo, { setIndex: true })
      }
      idle.push(w)
      pump()
    }
    idle.push(w)
  }
}

function pump() {
  while (idle.length && queue.length) {
    const job = queue.shift()!
    if (dropped.has(job.geo) || (job.geo as any).boundsTree) continue
    const w = idle.pop()!
    const pos = job.geo.getAttribute('position').array as Float32Array
    const idx = job.geo.index!.array as Uint32Array | Uint16Array
    const position = pos.slice(); const index = idx.slice()
    pending.set(job.id, job.geo)
    w.postMessage({ id: job.id, position, index }, [position.buffer, index.buffer])
  }
}

/** Queue a BVH build; falls back to building on the main thread without workers. */
export function requestBVH(geo: THREE.BufferGeometry) {
  if ((geo as any).boundsTree || !geo.index || !geo.getAttribute('position')) return
  init()
  if (!workers || workers.length === 0) { (geo as any).computeBoundsTree?.(); return }
  queue.push({ id: nextId++, geo })
  pump()
}

/** The tile was unloaded: skip / discard its pending build. */
export function dropBVH(geo: THREE.BufferGeometry) {
  dropped.add(geo)
}
