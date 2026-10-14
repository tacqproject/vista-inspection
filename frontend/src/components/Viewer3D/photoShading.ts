import * as THREE from 'three'

/**
 * Photo shading: a photogrammetric texture already contains the real light,
 * so textured meshes are drawn unlit (exact photo colours, no tone mapping).
 * The lit (PBR) material is kept on userData and swapped back for relief
 * shading. Untextured meshes always stay lit.
 */
export function applyPhotoShading(root: THREE.Object3D | null | undefined, on: boolean) {
  root?.traverse((o: any) => {
    if (!o.isMesh || Array.isArray(o.material)) return
    if (!o.userData.photoMat) {
      const m = o.material
      if (!m?.map) return
      o.userData.litMat = m
      o.userData.photoMat = new THREE.MeshBasicMaterial({
        name: m.name, map: m.map, side: m.side, toneMapped: false,
        transparent: m.transparent, opacity: m.opacity, depthWrite: m.depthWrite,
      })
    }
    const target = on ? o.userData.photoMat : o.userData.litMat
    if (o.material !== target) {
      // carry the current fade state over
      target.transparent = o.material.transparent
      target.opacity = o.material.opacity
      target.depthWrite = o.material.depthWrite
      o.material = target
    }
  })
}

/** Dispose the variant not currently assigned (the other is disposed with the object). */
export function disposePhotoShading(o: any) {
  const extra = o.material === o.userData.photoMat ? o.userData.litMat : o.userData.photoMat
  extra?.dispose?.()
}
