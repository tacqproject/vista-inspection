import * as THREE from 'three'

/**
 * Outward surface normal (world) at a world point, via the model's BVH closest
 * face — view-independent, so a defect is framed from the side it's actually on
 * (unlike an "outward from centre" guess, wrong for e.g. a column).
 */
export function surfaceNormalAt(scene: THREE.Object3D, pWorld: THREE.Vector3): THREE.Vector3 | null {
  let out: THREE.Vector3 | null = null
  let best = Infinity
  scene.traverse((o: any) => {
    const bt = o?.geometry?.boundsTree
    if (!o.isMesh || !bt) return
    const pLocal = o.worldToLocal(pWorld.clone())
    const hit = bt.closestPointToPoint(pLocal)
    if (!hit || hit.distance >= best) return
    best = hit.distance
    const g = o.geometry
    const fi = hit.faceIndex
    const idx = g.index
    const ia = idx ? idx.getX(fi * 3) : fi * 3
    const ib = idx ? idx.getX(fi * 3 + 1) : fi * 3 + 1
    const ic = idx ? idx.getX(fi * 3 + 2) : fi * 3 + 2
    const pos = g.attributes.position
    const va = new THREE.Vector3().fromBufferAttribute(pos, ia)
    const vb = new THREE.Vector3().fromBufferAttribute(pos, ib)
    const vc = new THREE.Vector3().fromBufferAttribute(pos, ic)
    const nLocal = new THREE.Vector3()
    new THREE.Triangle(va, vb, vc).getNormal(nLocal)   // outward for standard winding
    const nm = new THREE.Matrix3().getNormalMatrix(o.matrixWorld)
    out = nLocal.applyMatrix3(nm).normalize()
  })
  return out
}
