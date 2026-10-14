import { create } from 'zustand'
import type { Annotation, Vec3 } from '../types'

export type ViewerTool = 'view' | 'annotate' | 'measure' | 'polygon'

export interface ClickedFaceInfo {
  u: number           // 0-1 in image space
  v: number           // 0-1 in image space (Y already flipped from UV space)
  textureUrl: string | null
  // which inspection's model this click belongs to — so the eye marker only
  // shows in that viewer (the store is shared across the compare panes)
  inspectionId?: string
  modelPoint?: Vec3 | null   // clicked point in original model coordinates (for photo projection)
  modelNormal?: Vec3 | null  // surface normal at the click (mesh frame)
  // polygon defect boundary in MODEL coords + its colour, so photos can outline
  // the defect exactly like the 3D view
  outline?: Vec3[] | null
  outlineColor?: string | null
  // set when the lookup came from selecting a defect (not a raw model click) —
  // the UI then keeps the current tab instead of jumping to Foto
  fromAnnotation?: boolean
}

interface MeasurePoint {
  position: Vec3
  index: number
}

interface ViewerState {
  tool: ViewerTool
  setTool: (t: ViewerTool) => void

  selectedAnnotation: Annotation | null
  setSelectedAnnotation: (a: Annotation | null) => void

  // hovering a row in the defect/measurement list highlights it in 3D
  hoveredAnnotationId: string | null
  setHoveredAnnotationId: (id: string | null) => void
  hoveredMeasurementId: string | null
  setHoveredMeasurementId: (id: string | null) => void

  // request the camera to fly to a point (scene coords). `token` bumps on every
  // call so clicking the same item again re-triggers the flight. `orient` re-aims
  // the camera to look at the feature face-on (along `normal`, else outward from
  // the model centre) instead of keeping the current direction.
  focusRequest: {
    x: number; y: number; z: number; radius: number; token: number
    orient?: boolean; nx?: number; ny?: number; nz?: number
  } | null
  focusOn: (p: Vec3, radius?: number, orient?: boolean, normal?: Vec3 | null) => void

  pendingPosition: Vec3 | null
  setPendingPosition: (p: Vec3 | null) => void

  measurePoints: MeasurePoint[]
  addMeasurePoint: (p: Vec3) => void
  clearMeasurePoints: () => void

  // polygon drawing
  polygonPoints: Vec3[]
  addPolygonPoint: (p: Vec3) => void
  clearPolygon: () => void

  pendingPolygon: Vec3[] | null
  setPendingPolygon: (pts: Vec3[] | null) => void

  hoverPoint3D: Vec3 | null
  setHoverPoint3D: (p: Vec3 | null) => void

  showAnnotations: boolean
  toggleAnnotations: () => void

  showMeasurements: boolean
  toggleMeasurements: () => void

  annotationFormOpen: boolean
  setAnnotationFormOpen: (v: boolean) => void

  clickedFaceInfo: ClickedFaceInfo | null
  setClickedFaceInfo: (info: ClickedFaceInfo | null) => void

  // display normalization of the loaded model: scene = model * scale + offset
  modelTransform: { scale: number; offset: Vec3; groundY: number } | null
  setModelTransform: (t: { scale: number; offset: Vec3; groundY: number } | null) => void

  // Set when a model fails to load, so the viewer shows an error instead of an
  // endless loading spinner (the overlay only clears on modelTransform).
  modelError: string | null
  setModelError: (e: string | null) => void

  // whole-model display re-orientation quaternion [x,y,z,w] (e.g. Z-up scan →
  // upright). Applied to a group wrapping all world content; clicks are mapped
  // back through its inverse so stored coordinates stay in the un-rotated frame.
  modelQuat: [number, number, number, number]
  setModelQuat: (q: [number, number, number, number]) => void

  // capture a PNG data-URL of the 3D canvas with numbered defect markers drawn
  // on top; registered from inside the R3F Canvas, called by the report dialog.
  snapshotFn: ((annotations: Annotation[]) => Promise<string | null>) | null
  setSnapshotFn: (fn: ((annotations: Annotation[]) => Promise<string | null>) | null) => void

  // capture ONE face-on 3D close-up per defect (index-aligned) so every defect
  // is visible in the PDF, not just those in a single overview shot.
  defectSnapshotsFn: ((annotations: Annotation[]) => Promise<(string | null)[]>) | null
  setDefectSnapshotsFn: (fn: ((annotations: Annotation[]) => Promise<(string | null)[]>) | null) => void

  // Resolves once the model shows full detail for the CURRENT camera. A streamed
  // (tiled) model refines asynchronously after a camera move, so snapshots must
  // wait for it; single-file models resolve immediately (null = nothing to wait).
  modelSettleFn: (() => Promise<void>) | null
  setModelSettleFn: (fn: (() => Promise<void>) | null) => void

  showCameras: boolean
  toggleCameras: () => void
  cameraPhoto: string | null              // filename of the photo opened by clicking a frustum
  setCameraPhoto: (f: string | null) => void

  // viewer flown into a camera's viewpoint → its photo can be overlaid
  activeCameraView: string | null
  activeCameraPP: [number, number] | null   // principal point offset (fraction of image size)
  activeCameraFov: number | null            // the lens' vertical FOV (deg) = zoom 1×
  cameraViewZoom: number                    // optical zoom while looking through the camera
  setActiveCameraView: (f: string | null, pp?: [number, number] | null, fov?: number | null) => void
  setCameraViewZoom: (z: number) => void
  photoOverlay: boolean
  setPhotoOverlay: (v: boolean) => void
  photoOverlayOpacity: number
  setPhotoOverlayOpacity: (v: number) => void

  showPointCloud: boolean
  togglePointCloud: () => void

  // photo shading: textured models drawn unlit — the photo texture already
  // holds the real light; scene lights on top double the shading, wash out
  // sunlit faces and turn reconstruction noise into visible bumps. Off = lit
  // relief (useful to judge the geometry itself).
  photoShading: boolean
  togglePhotoShading: () => void

  // streamed model detail per inspection (compare panes share the store):
  // 'loading' = better tiles still on the way; 'view' = best the current zoom
  // needs (zooming in loads more); 'max' = only finest tiles on screen
  // texel = size of one texture pixel on the surface at the view centre (scene units)
  detailStatus: Record<string, { state: 'loading' | 'view' | 'max'; texel: number | null }>
  setDetailStatus: (inspectionId: string, s: { state: 'loading' | 'view' | 'max'; texel: number | null } | null) => void

  cameraSize: number                       // frustum size in scene units
  setCameraSize: (v: number) => void

  // set when the user clicks the first vertex to close the polygon;
  // InspectionDetail watches this and runs its finishPolygon()
  polygonCloseRequested: boolean
  requestPolygonClose: () => void
  clearPolygonCloseRequest: () => void
}

export const useViewerStore = create<ViewerState>((set, get) => ({
  tool: 'view',
  setTool: t => set({
    tool: t,
    pendingPosition: null,
    measurePoints: [],
    polygonPoints: [],
    hoverPoint3D: null,
  }),

  selectedAnnotation: null,
  setSelectedAnnotation: a => set({ selectedAnnotation: a }),

  hoveredAnnotationId: null,
  setHoveredAnnotationId: id => set({ hoveredAnnotationId: id }),
  hoveredMeasurementId: null,
  setHoveredMeasurementId: id => set({ hoveredMeasurementId: id }),

  focusRequest: null,
  focusOn: (p, radius = 0.22, orient = false, normal = null) =>
    set(s => ({
      focusRequest: {
        x: p.x, y: p.y, z: p.z,
        radius,
        token: (s.focusRequest?.token ?? 0) + 1,
        orient,
        nx: normal?.x, ny: normal?.y, nz: normal?.z,
      },
    })),

  pendingPosition: null,
  setPendingPosition: p => set({ pendingPosition: p }),

  measurePoints: [],
  addMeasurePoint: p => {
    const pts = get().measurePoints
    if (pts.length >= 2) {
      set({ measurePoints: [{ position: p, index: 0 }] })
    } else {
      set({ measurePoints: [...pts, { position: p, index: pts.length }] })
    }
  },
  clearMeasurePoints: () => set({ measurePoints: [] }),

  polygonPoints: [],
  addPolygonPoint: p => set(s => ({ polygonPoints: [...s.polygonPoints, p] })),
  clearPolygon: () => set({ polygonPoints: [], hoverPoint3D: null }),

  pendingPolygon: null,
  setPendingPolygon: pts => set({ pendingPolygon: pts }),

  hoverPoint3D: null,
  setHoverPoint3D: p => set({ hoverPoint3D: p }),

  showAnnotations: true,
  toggleAnnotations: () => set(s => ({ showAnnotations: !s.showAnnotations })),

  showMeasurements: true,
  toggleMeasurements: () => set(s => ({ showMeasurements: !s.showMeasurements })),

  annotationFormOpen: false,
  setAnnotationFormOpen: v => set({ annotationFormOpen: v }),

  clickedFaceInfo: null,
  setClickedFaceInfo: info => set({ clickedFaceInfo: info }),

  modelTransform: null,
  setModelTransform: t => set({ modelTransform: t }),

  modelError: null,
  setModelError: e => set({ modelError: e }),

  modelQuat: [0, 0, 0, 1],
  setModelQuat: q => set({ modelQuat: q }),

  snapshotFn: null,
  setSnapshotFn: fn => set({ snapshotFn: fn }),

  defectSnapshotsFn: null,
  setDefectSnapshotsFn: fn => set({ defectSnapshotsFn: fn }),

  modelSettleFn: null,
  setModelSettleFn: fn => set({ modelSettleFn: fn }),

  showCameras: false,
  toggleCameras: () => set(s => ({ showCameras: !s.showCameras })),
  cameraPhoto: null,
  setCameraPhoto: f => set({ cameraPhoto: f }),

  activeCameraView: null,
  activeCameraPP: null,
  activeCameraFov: null,
  cameraViewZoom: 1,
  setActiveCameraView: (f, pp = null, fov = null) =>
    set({ activeCameraView: f, activeCameraPP: pp, activeCameraFov: fov, cameraViewZoom: 1, photoOverlay: false }),
  setCameraViewZoom: z => set({ cameraViewZoom: z }),
  photoOverlay: false,
  setPhotoOverlay: v => set({ photoOverlay: v }),
  photoOverlayOpacity: 0.5,
  setPhotoOverlayOpacity: v => set({ photoOverlayOpacity: v }),

  showPointCloud: false,
  togglePointCloud: () => set(s => ({ showPointCloud: !s.showPointCloud })),

  detailStatus: {},
  setDetailStatus: (id, st) => set(s => {
    const cur = s.detailStatus[id]
    if ((!cur && !st) || (cur && st && cur.state === st.state && cur.texel === st.texel)) return s
    const next = { ...s.detailStatus }
    if (st) next[id] = st; else delete next[id]
    return { detailStatus: next }
  }),

  photoShading: localStorage.getItem('vista-photo-shading') !== '0',
  togglePhotoShading: () => set(s => {
    try { localStorage.setItem('vista-photo-shading', s.photoShading ? '0' : '1') } catch { /* private mode */ }
    return { photoShading: !s.photoShading }
  }),

  cameraSize: parseFloat(localStorage.getItem('vista-camera-size') ?? '0.2'),
  setCameraSize: v => {
    localStorage.setItem('vista-camera-size', String(v))
    set({ cameraSize: v })
  },

  polygonCloseRequested: false,
  requestPolygonClose: () => set({ polygonCloseRequested: true }),
  clearPolygonCloseRequest: () => set({ polygonCloseRequested: false }),
}))
