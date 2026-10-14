import { useEffect, useMemo, useRef } from 'react'
import { useLoader, useThree } from '@react-three/fiber'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'

export interface OverlayCfg {
  modelPath: string      // model of the OTHER inspection (its own mesh frame)
  refModelPath: string   // the reference (A) model — to derive A's normalization
  matrix: number[]       // row-major 4×4, mesh-space: X_ref_mesh = M · X_this_mesh
  opacity: number
  color?: string
  // B is already baked into the canonical (A) frame → no alignment to apply,
  // render it straight (identity normalization), it sits on A as-is.
  canonical?: boolean
  // manual fine-tune of B on top of the alignment (A's scene frame): scale +
  // rotation about B's centre (radians), then a translation (scene units).
  adjust?: { tx: number; ty: number; tz: number; rx: number; ry: number; rz: number; sc: number }
}

/** A's display normalization (mesh → 5-unit scene box), matching Scene's. */
function normalizationOf(gltf: { scene: THREE.Object3D }) {
  const box = new THREE.Box3().setFromObject(gltf.scene)
  const center = box.getCenter(new THREE.Vector3())
  const size = box.getSize(new THREE.Vector3())
  const maxDim = Math.max(size.x, size.y, size.z)
  const scale = maxDim > 0 ? 5 / maxDim : 1
  return { scale, offset: center.multiplyScalar(-scale) }
}

/**
 * Renders another inspection's model (B) registered into the reference model's
 * (A) frame: raw B mesh → alignment matrix → A's display normalization. Used to
 * see a partial re-scan sitting in place on the full reference model.
 *
 * A's normalization is derived HERE from A's own mesh (loaded from the loader
 * cache the pane already populated) rather than a shared store value: that store
 * value is a single global clobbered by other viewer panes, which left the
 * overlay with no transform and nothing rendered.
 */
export default function OverlayModel({ cfg }: { cfg: OverlayCfg }) {
  const invalidate = useThree(s => s.invalidate)
  const camera = useThree(s => s.camera)
  const controls = useThree(s => s.controls)

  // lod1 is plenty for the overlay; A's lod0 matches Scene's normalization basis
  const bPath = `/uploads/${cfg.modelPath.replace(/lod\d/, 'lod1')}`
  const aPath = `/uploads/${cfg.refModelPath.replace(/lod\d/, 'lod0')}`
  const [bGltf, aGltf] = useLoader(GLTFLoader, [bPath, aPath])

  const scene = useMemo(() => bGltf.scene.clone(true), [bGltf])
  // Baked (canonical) B is already in A's scene frame → identity everything.
  const ref = useMemo(
    () => cfg.canonical
      ? { scale: 1, offset: new THREE.Vector3(0, 0, 0) }
      : normalizationOf(aGltf),
    [aGltf, cfg.canonical],
  )
  const m = useMemo(
    () => cfg.canonical
      ? new THREE.Matrix4()                                      // identity
      : new THREE.Matrix4().fromArray(cfg.matrix).transpose(),   // row→col major
    [cfg.matrix, cfg.canonical],
  )

  // B's centre in A's scene frame — the pivot for the manual fine-tune rotation.
  const worldCenter = useMemo(() => {
    const box = new THREE.Box3().setFromObject(bGltf.scene)
    const c = box.getCenter(new THREE.Vector3())
    return c.applyMatrix4(m).multiplyScalar(ref.scale)
      .add(new THREE.Vector3(ref.offset.x, ref.offset.y, ref.offset.z))
  }, [bGltf, m, ref])

  useEffect(() => {
    scene.traverse((child: any) => {
      if (!(child instanceof THREE.Mesh)) return
      child.raycast = () => null          // overlay must not steal clicks
      // The overlay carries a manual matrix (matrixAutoUpdate off), so three's
      // frustum test can evaluate the bounding sphere at the wrong (pre-matrix)
      // place and cull the whole mesh — it then renders NOWHERE. Disable culling.
      child.frustumCulled = false
      // A partial re-scan sits coincident with — or recessed INTO — the big
      // reference model, so normal depth testing hides it behind the reference's
      // shell. Draw the overlay on top (x-ray) so it's visible from any angle.
      child.renderOrder = 999
      const mats = Array.isArray(child.material) ? child.material : [child.material]
      child.material = mats.map((mat: THREE.Material) => {
        const c = (mat as any).clone() as THREE.MeshStandardMaterial
        c.transparent = true
        c.opacity = cfg.opacity
        c.depthTest = false               // always render over the reference
        c.depthWrite = false
        if (cfg.color) {
          c.color = new THREE.Color(cfg.color)
          c.map = null                    // flat tint reads better for a diff view
          c.emissive = new THREE.Color(cfg.color)
          c.emissiveIntensity = 0.25
        }
        return c
      })
      if (child.material.length === 1) child.material = child.material[0]
    })
    invalidate()
  }, [scene, cfg.opacity, cfg.color, invalidate])

  // Fly the camera to frame B once — a door-sized re-scan on a whole building is
  // otherwise an unfindable speck, which reads as "B isn't there".
  const framedRef = useRef(false)
  useEffect(() => {
    if (framedRef.current) return
    const box = new THREE.Box3().setFromObject(bGltf.scene)   // B in its mesh frame
    if (box.isEmpty()) return
    const center = box.getCenter(new THREE.Vector3())
    const size = box.getSize(new THREE.Vector3())
    // world (scene) placement of B's centre: scale·(M·v) + offset
    const worldCenter = center.clone().applyMatrix4(m).multiplyScalar(ref.scale)
      .add(new THREE.Vector3(ref.offset.x, ref.offset.y, ref.offset.z))
    const worldRadius =
      (0.5 * Math.max(size.x, size.y, size.z) * m.getMaxScaleOnAxis() * ref.scale) || 1
    const cam = camera as THREE.PerspectiveCamera
    const dist = (worldRadius / Math.tan((cam.fov * Math.PI / 180) / 2)) * 2.4
    const dir = new THREE.Vector3(1, 0.5, 1).normalize()
    cam.position.copy(worldCenter.clone().add(dir.multiplyScalar(dist)))
    cam.near = Math.max(0.001, worldRadius * 0.02)
    cam.updateProjectionMatrix()
    const ctl = controls as any
    if (ctl?.target) { ctl.target.copy(worldCenter); ctl.update() }
    framedRef.current = true
    invalidate()
  }, [bGltf, m, ref, camera, controls, invalidate])

  const adj = cfg.adjust
  const wc = worldCenter
  // manual fine-tune (A's scene frame): translate ∘ (scale+rotate about B-centre)
  return (
    <group position={adj ? [adj.tx, adj.ty, adj.tz] : [0, 0, 0]}>
      <group position={[wc.x, wc.y, wc.z]} rotation={adj ? [adj.rx, adj.ry, adj.rz] : [0, 0, 0]} scale={adj ? adj.sc : 1}>
        <group position={[-wc.x, -wc.y, -wc.z]}>
          <group scale={ref.scale} position={[ref.offset.x, ref.offset.y, ref.offset.z]}>
            <primitive object={scene} matrixAutoUpdate={false} matrix={m} />
          </group>
        </group>
      </group>
    </group>
  )
}
