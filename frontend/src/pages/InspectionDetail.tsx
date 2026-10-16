import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Eye, MapPin, Ruler, FileText, Upload,
  Layers, Lasso, Image as ImageIcon, AlertTriangle, RefreshCw,
  Wrench, Scissors, Undo2, Pencil, Video, X, Sparkles, Map, ChevronUp, ChevronDown, Crosshair,
  GitCompareArrows, ArrowRight, RotateCcw, RotateCw, Move3d,
  Gauge,
  ScanSearch,
  Sun,
} from 'lucide-react'
import { inspectionsApi, uploadsApi, configApi } from '../api/inspections'
import { structuresApi } from '../api/structures'
import { METHOD_LABELS, STATUS_LABELS, SEVERITY_COLORS } from '../types'
import Viewer3D from '../components/Viewer3D/Viewer3D'
import AnnotationPanel from '../components/AnnotationPanel'
import MeasurementPanel from '../components/MeasurementPanel'
import PhotoUpload from '../components/Upload/PhotoUpload'
import PhotoSourcePanel from '../components/PhotoSourcePanel'
import { useViewerStore } from '../store/viewerStore'
import type { ViewerTool } from '../store/viewerStore'
import { useAuthStore } from '../store/authStore'
import ReportModal from '../components/ReportModal'
import BmsProtocolModal from '../components/BmsProtocolModal'
import QualityModal from '../components/QualityModal'
import AiDefectsModal from '../components/AiDefectsModal'
import ApprovalControls from '../components/ApprovalControls'
import AiSegmentModal from '../components/AiSegmentModal'
import { errorMessage } from '../api/client'

type PanelTab = 'annotations' | 'measurements' | 'info' | 'photos' | 'inspections'

export default function InspectionDetail() {
  const { structureId, inspectionId } = useParams<{ structureId: string; inspectionId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const canEdit = useAuthStore(s => s.canEdit())
  const [activeTab, setActiveTab] = useState<PanelTab>('annotations')
  const [showUpload, setShowUpload] = useState(false)
  const [showReport, setShowReport] = useState(false)
  const [showBms, setShowBms] = useState(false)
  const [showQuality, setShowQuality] = useState(false)
  const [showAiDefects, setShowAiDefects] = useState(false)
  const [showAi, setShowAi] = useState(false)
  const [liveProgress, setLiveProgress] = useState<{ pct: number; message: string } | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [showReprocess, setShowReprocess] = useState(false)
  const [showEditInsp, setShowEditInsp] = useState(false)
  const [cropping, setCropping] = useState(false)

  // satellite map underlay (rotation/size persisted per structure)
  const [showMap, setShowMap] = useState(false)
  const [mapPanelOpen, setMapPanelOpen] = useState(false)
  const [mapCfg, setMapCfg] = useState(() => {
    try {
      const cfg = JSON.parse(localStorage.getItem(`vista-map-${structureId}`) || '')
      return { rotationDeg: 0, sizeMeters: 120, offsetX: 0, offsetY: 0, offsetZ: 0, scalePct: 100, ...cfg }
    }
    catch { return { rotationDeg: 0, sizeMeters: 120, offsetX: 0, offsetY: 0, offsetZ: 0, scalePct: 100 } }
  })
  const saveMapCfg = (cfg: { rotationDeg: number; sizeMeters: number; offsetX: number; offsetY: number; offsetZ: number; scalePct: number }) => {
    setMapCfg(cfg)
    localStorage.setItem(`vista-map-${structureId}`, JSON.stringify(cfg))
  }

  const {
    tool, setTool,
    showAnnotations, toggleAnnotations,
    showMeasurements, toggleMeasurements,
    showCameras, toggleCameras,
    showPointCloud, togglePointCloud,
    photoShading, togglePhotoShading,
    cameraSize, setCameraSize,
    cameraPhoto, setCameraPhoto,
    activeCameraView, activeCameraPP, cameraViewZoom, setActiveCameraView,
    photoOverlay, setPhotoOverlay,
    photoOverlayOpacity, setPhotoOverlayOpacity,
    polygonPoints, clearPolygon,
    polygonCloseRequested, clearPolygonCloseRequest,
    setPendingPolygon, setAnnotationFormOpen,
    clickedFaceInfo, setClickedFaceInfo, modelTransform,
    selectedAnnotation,
    modelQuat, setModelQuat,
  } = useViewerStore()

  // Auto-switch to Foto tab when the user clicks the MODEL in view mode.
  // Selecting a defect only prepares the photos (the Foto tab shows a • cue) —
  // it keeps the current tab so the defect card stays open for edit/detail.
  useEffect(() => {
    if (clickedFaceInfo && !clickedFaceInfo.fromAnnotation && tool === 'view') {
      setActiveTab('photos')
    }
  }, [clickedFaceInfo])

  // Selecting a defect looks up the photos that see it: convert its position
  // (scene coords) back to model coords and feed it to the photo search. No
  // surface normal is sent on purpose — that keeps the search lenient so photos
  // where the defect is only partly / obliquely visible still show up.
  useEffect(() => {
    const a = selectedAnnotation
    if (!a || !modelTransform) return
    const { scale, offset } = modelTransform
    const toModel = (p: { x: number; y: number; z: number }) => ({
      x: (p.x - offset.x) / scale,
      y: (p.y - offset.y) / scale,
      z: (p.z - offset.z) / scale,
    })

    let sp: { x: number; y: number; z: number } | null = null
    let outline: Array<{ x: number; y: number; z: number }> | null = null
    const geo = a.geometry_json as Array<{ x: number; y: number; z: number }> | undefined
    if (Array.isArray(geo) && geo.length >= 3) {
      const n = geo.length
      sp = {
        x: geo.reduce((s, p) => s + p.x, 0) / n,
        y: geo.reduce((s, p) => s + p.y, 0) / n,
        z: geo.reduce((s, p) => s + p.z, 0) / n,
      }
      outline = geo.map(toModel)   // polygon boundary → model coords for the photos
    } else if (a.position_x != null && a.position_y != null && a.position_z != null) {
      sp = { x: a.position_x, y: a.position_y, z: a.position_z }
    }
    if (!sp) return
    setClickedFaceInfo({
      u: 0.5, v: 0.5, textureUrl: null,
      modelPoint: toModel(sp),
      modelNormal: null,
      outline,
      outlineColor: SEVERITY_COLORS[a.severity],
      fromAnnotation: true,
    })
  }, [selectedAnnotation?.id, modelTransform, setClickedFaceInfo])

  const { data: inspection } = useQuery({
    queryKey: ['inspection', structureId, inspectionId],
    queryFn: () => inspectionsApi.get(structureId!, inspectionId!),
    enabled: !!structureId && !!inspectionId,
    refetchInterval: (query) => {
      const ps = query.state.data?.photo_set
      return ps && ['processing', 'uploading'].includes(ps.status) ? 3000 : false
    },
  })

  const { data: structure } = useQuery({
    queryKey: ['structures', structureId],
    queryFn: () => structuresApi.get(structureId!),
    enabled: !!structureId,
  })
  const hasCoords = structure?.latitude != null && structure?.longitude != null

  const photoSet = inspection?.photo_set
  const modelReady = photoSet?.status === 'completed' && !!photoSet?.model_path

  // ─── Model re-orientation (e.g. flip a Z-up reality scan upright) ──────────
  // Load the saved orientation into the viewer; reset on unmount/switch.
  useEffect(() => {
    const o = inspection?.model_orient
    setModelQuat(o && o.length === 4 ? (o as [number, number, number, number]) : [0, 0, 0, 1])
  }, [inspection?.model_orient, inspectionId, setModelQuat])
  useEffect(() => () => setModelQuat([0, 0, 0, 1]), [setModelQuat])

  const rotateModel = (axis: 'x' | 'y' | 'z', deg: number) => {
    // quaternion for a `deg`-rotation about a world axis, pre-multiplied onto the
    // current orientation so buttons rotate around the fixed screen axes
    const r = (deg * Math.PI) / 360
    const s = Math.sin(r), c = Math.cos(r)
    const a: [number, number, number, number] =
      axis === 'x' ? [s, 0, 0, c] : axis === 'y' ? [0, s, 0, c] : [0, 0, s, c]
    const [ax, ay, az, aw] = a
    const [bx, by, bz, bw] = modelQuat
    const q: [number, number, number, number] = [
      aw * bx + ax * bw + ay * bz - az * by,
      aw * by - ax * bz + ay * bw + az * bx,
      aw * bz + ax * by - ay * bx + az * bw,
      aw * bw - ax * bx - ay * by - az * bz,
    ]
    setModelQuat(q)
    if (structureId && inspectionId) inspectionsApi.update(structureId, inspectionId, { model_orient: q })
  }
  const resetOrient = () => {
    setModelQuat([0, 0, 0, 1])
    if (structureId && inspectionId) inspectionsApi.update(structureId, inspectionId, { model_orient: [0, 0, 0, 1] })
  }

  // WebSocket for live progress
  useEffect(() => {
    if (!inspectionId || !photoSet || !['processing', 'uploading'].includes(photoSet.status)) return
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${wsProto}//${window.location.host}/ws/inspections/${inspectionId}`
    const ws = new WebSocket(wsUrl)
    ws.onmessage = (e) => {
      const data = JSON.parse(e.data)
      if (data.type === 'progress') {
        setLiveProgress({ pct: data.progress, message: data.message })
      }
      if (data.type === 'completed' || data.type === 'error') {
        setLiveProgress(null)
        qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
      }
    }
    return () => ws.close()
  }, [inspectionId, photoSet?.status])

  // Keyboard shortcut: Escape cancels polygon drawing / restarts measuring
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (tool === 'polygon') {
        clearPolygon()
        setTool('view')
      } else if (tool === 'measure') {
        // clear the points but stay in measure mode — ready for the next pair
        useViewerStore.getState().clearMeasurePoints()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [tool])

  const handleRetry = async () => {
    if (!inspectionId) return
    setRetrying(true)
    try {
      await uploadsApi.reprocess(inspectionId)
      setLiveProgress({ pct: 0, message: 'Spouštím rekonstrukci…' })
      qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
    } catch (err: any) {
      alert(errorMessage(err, 'Rekonstrukci se nepodařilo spustit'))
    } finally {
      setRetrying(false)
    }
  }


  const finishPolygon = () => {
    if (polygonPoints.length < 3) return
    setPendingPolygon([...polygonPoints])
    clearPolygon()
    setTool('view')          // avoid dropping stray points while the form is open
    setAnnotationFormOpen(true)
    setActiveTab('annotations')
  }

  // clicking the first vertex in the viewer closes the polygon
  useEffect(() => {
    if (!polygonCloseRequested) return
    clearPolygonCloseRequest()
    finishPolygon()
  }, [polygonCloseRequested])

  const cropToPolygon = async () => {
    if (polygonPoints.length < 3 || !modelTransform || !inspectionId) return
    if (!confirm(`Oříznout model na vybranou oblast? Vše mimo polygon (${polygonPoints.length} bodů) se skryje. Původní model lze kdykoli obnovit.`)) return
    setCropping(true)
    try {
      const { scale, offset } = modelTransform
      const pts = polygonPoints.map(p => ({
        x: (p.x - offset.x) / scale,
        y: (p.y - offset.y) / scale,
        z: (p.z - offset.z) / scale,
      }))
      await uploadsApi.cropModel(inspectionId, pts)
      clearPolygon()
      setTool('view')
      qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
    } catch (err: any) {
      alert(errorMessage(err, 'Ořez modelu selhal'))
    } finally {
      setCropping(false)
    }
  }

  const restoreModel = async () => {
    if (!inspectionId) return
    try {
      await uploadsApi.restoreModel(inspectionId)
      qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
    } catch (err: any) {
      alert(errorMessage(err, 'Obnovení modelu selhalo'))
    }
  }

  const isCropped = photoSet?.model_path?.includes('_crop') ?? false
  const isReference = structure?.reference_inspection_id === inspectionId

  const setReference = async () => {
    if (!structureId || !inspectionId) return
    if (!confirm('Nastavit tuto inspekci jako referenční rámec konstrukce?\n\n'
      + 'Všechny ostatní inspekce se do ní zarovnají a natrvalo zapečou, aby '
      + 'seděly pohledy, souřadnice i měřítko. Běží na pozadí.')) return
    try {
      await inspectionsApi.setReference(structureId, inspectionId)
      qc.invalidateQueries({ queryKey: ['structures', structureId] })
      qc.invalidateQueries({ queryKey: ['inspections', structureId] })
      alert('Reference nastavena. Ostatní inspekce se zarovnávají a zapékají na pozadí — může chvíli trvat.')
    } catch (err: any) {
      alert(errorMessage(err, 'Nastavení reference selhalo'))
    }
  }

  const cancelPolygon = () => {
    clearPolygon()
    setTool('view')
  }

  return (
    <div className="flex flex-col h-screen">
      {/* Top bar */}
      <header className="bg-white border-b border-gray-200 px-4 py-2 flex items-center gap-3 shrink-0 overflow-x-auto">
        <button onClick={() => navigate(`/structures/${structureId}`)} className="btn-ghost p-1.5 shrink-0">
          <ArrowLeft size={16} />
        </button>
        <div className="min-w-0 shrink-0 lg:flex-1 max-w-[40vw] lg:max-w-none">
          <p className="text-sm font-semibold text-gray-900 truncate">{inspection?.name ?? '…'}</p>
          <p className="text-xs text-gray-400 truncate">
            {inspection?.inspection_date
              ? new Date(inspection.inspection_date).toLocaleDateString('cs-CZ')
              : ''}
            {inspection?.method ? ` · ${METHOD_LABELS[inspection.method] ?? inspection.method}` : ''}
          </p>
        </div>

        {/* Toolbar */}
        <div className="flex items-center gap-1 bg-gray-100 p-1 rounded-lg shrink-0">
          <ToolBtn active={tool === 'view'} onClick={() => setTool('view')} title="Prohlížení (V)">
            <Eye size={15} />
          </ToolBtn>
          {canEdit && (
            <>
              <ToolBtn active={tool === 'annotate'} onClick={() => setTool('annotate')} title="Označit bod poruchy">
                <MapPin size={15} />
              </ToolBtn>
              <ToolBtn active={tool === 'polygon'} onClick={() => setTool('polygon')} title="Zakreslit plochu poruchy">
                <Lasso size={15} />
              </ToolBtn>
            </>
          )}
          <ToolBtn active={tool === 'measure'} onClick={() => setTool('measure')} title="Měřit vzdálenost">
            <Ruler size={15} />
          </ToolBtn>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={toggleAnnotations}
            className={`btn-ghost text-xs ${showAnnotations ? 'text-brand-600' : ''}`}
            title="Zobrazit/skrýt poruchy"
          >
            <Layers size={15} />
          </button>
          <button
            onClick={toggleMeasurements}
            className={`btn-ghost text-xs ${showMeasurements ? 'text-brand-600' : ''}`}
            title="Zobrazit/skrýt měření"
          >
            <Ruler size={15} />
          </button>
          <button
            onClick={toggleCameras}
            className={`btn-ghost text-xs ${showCameras ? 'text-brand-600' : ''}`}
            title="Zobrazit/skrýt pozice kamer (klik = snímek, dvojklik = pohled kamery)"
          >
            <Video size={15} />
          </button>
          <button
            onClick={togglePointCloud}
            className={`btn-ghost text-xs ${showPointCloud ? 'text-brand-600' : ''}`}
            title="Zobrazit/skrýt řídké mračno bodů (SfM landmarky)"
          >
            <Sparkles size={15} />
          </button>
          <button
            onClick={togglePhotoShading}
            className={`btn-ghost text-xs ${photoShading ? '' : 'text-brand-600'}`}
            title={photoShading
              ? 'Osvětlení reliéfu: vypnuto — barvy přesně podle fotek (zapnout pro posouzení tvaru povrchu)'
              : 'Osvětlení reliéfu: zapnuto — stínování zvýrazní tvar povrchu (vypnout pro věrné barvy fotek)'}
          >
            <Sun size={15} />
          </button>
          <button
            onClick={() => setShowMap(m => !m)}
            className={`btn-ghost text-xs ${showMap ? 'text-brand-600' : ''}`}
            title={hasCoords
              ? 'Satelitní mapový podklad pod modelem'
              : 'Nejprve vyplňte souřadnice objektu (Upravit konstrukci)'}
          >
            <Map size={15} />
          </button>
          {isCropped && (
            <button onClick={restoreModel} className="btn-ghost text-xs" title="Obnovit celý (neoříznutý) model">
              <Undo2 size={14} /> Celý model
            </button>
          )}
          {canEdit && (
            <button
              onClick={() => setShowReprocess(true)}
              className="btn-ghost text-xs"
              title="Přegenerovat 3D model s jiným nastavením"
              disabled={photoSet ? ['processing', 'uploading'].includes(photoSet.status) : true}
            >
              <Wrench size={14} /> Model
            </button>
          )}
          {canEdit && modelReady && (
            isReference ? (
              <span className="badge bg-brand-100 text-brand-700 text-xs flex items-center gap-1" title="Tato inspekce je referenční rámec konstrukce">
                <Crosshair size={12} /> Reference
              </span>
            ) : (
              <button onClick={setReference} className="btn-ghost text-xs"
                title="Nastavit jako referenční rámec — ostatní inspekce se do ní zapečou">
                <Crosshair size={14} /> Reference
              </button>
            )
          )}
          {canEdit && (
            <button onClick={() => setShowUpload(true)} className="btn-secondary text-xs">
              <Upload size={14} /> Snímky
            </button>
          )}
          {canEdit && (
            <button onClick={() => setShowAi(true)} className="btn-ghost text-xs text-purple-600" title="AI segmentace poruch přes všechny snímky">
              <Sparkles size={14} /> AI
            </button>
          )}
          <button onClick={() => setShowAiDefects(true)} className="btn-ghost text-xs text-purple-600"
            title="AI projde fotky, najde trhliny a výlomy a zanese je do 3D modelu jako poruchy">
            <ScanSearch size={14} /> AI poruchy
          </button>
          <button onClick={() => setShowQuality(true)} className="btn-ghost text-xs" disabled={!inspection?.photo_set?.model_path}
            title="Objektivní vyhodnocení kvality 3D modelu + pohledy z pozice fotek">
            <Gauge size={14} /> Kvalita
          </button>
          <button onClick={() => setShowReport(true)} className="btn-ghost text-xs" title="Souhrnný PDF report (3D, měření, statistiky)">
            <FileText size={14} /> Souhrn
          </button>
          <button onClick={() => setShowBms(true)} className="btn-primary text-xs" disabled={!inspection}
            title="Protokol o prohlídce mostu ve formátu BMS (DOCX / PDF)">
            <FileText size={14} /> Protokol
          </button>
        </div>
      </header>

      {/* Approval workflow + read-only share */}
      {inspection && structureId && (
        <div className="bg-white border-b border-gray-200 px-4 py-2">
          <ApprovalControls structureId={structureId} inspection={inspection} />
        </div>
      )}

      {photoSet?.status === 'pending' && (
        <div className="bg-amber-50 border-b border-amber-200 px-4 py-2 text-sm text-amber-900">
          {photoSet.photo_count} snímků připraveno. Rekonstrukci spusťte tlačítkem Model.
          DNG originály se použijí přímo pro SfM; galerie zobrazuje náhledy.
        </div>
      )}
      {/* Processing banner */}
      {photoSet && ['processing', 'uploading'].includes(photoSet.status) && (
        <div className="bg-blue-50 border-b border-blue-200 px-4 py-2 flex items-center gap-3 text-sm">
          <div className="animate-spin rounded-full h-4 w-4 border-2 border-blue-500 border-t-transparent" />
          <span className="text-blue-700">
            {liveProgress?.message ?? 'Rekonstrukce 3D modelu…'}{' '}
            {liveProgress?.pct ?? photoSet.progress}%
          </span>
          <div className="flex-1 bg-blue-200 rounded-full h-1.5 max-w-xs">
            <div
              className="bg-blue-500 h-1.5 rounded-full transition-all"
              style={{ width: `${liveProgress?.pct ?? photoSet.progress}%` }}
            />
          </div>
        </div>
      )}

      {/* Failed banner with retry */}
      {photoSet?.status === 'failed' && (
        <div className="bg-red-50 border-b border-red-200 px-4 py-2 flex items-center gap-3 text-sm">
          <AlertTriangle size={16} className="text-red-500 shrink-0" />
          <span className="text-red-700 flex-1 min-w-0 truncate" title={photoSet.error_message ?? undefined}>
            Rekonstrukce selhala{photoSet.error_message ? `: ${photoSet.error_message}` : ''}
          </span>
          <button
            onClick={handleRetry}
            disabled={retrying}
            className="flex items-center gap-1.5 bg-red-600 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-red-500 disabled:opacity-50 transition-colors shrink-0"
          >
            <RefreshCw size={13} className={retrying ? 'animate-spin' : ''} />
            {retrying ? 'Spouštím…' : 'Zkusit znovu'}
          </button>
        </div>
      )}

      {/* Main layout — side-by-side on desktop, stacked (viewer over panel) on
          mobile/narrow so the 3D view stays usable on a phone */}
      <div className="flex flex-col lg:flex-row flex-1 overflow-hidden min-h-0">
        {/* 3D Viewer */}
        <div className="flex-1 relative bg-slate-800 overflow-hidden min-h-0 min-w-0">
          {inspectionId && (
            <Viewer3D
              inspectionId={inspectionId}
              modelPath={modelReady ? photoSet!.model_path! : undefined}
              canonical={inspection?.canonical ?? false}
              scaleFactor={inspection?.scale_factor ?? 1}
              map={showMap && hasCoords ? {
                lat: structure!.latitude!,
                lon: structure!.longitude!,
                rotationDeg: mapCfg.rotationDeg,
                sizeMeters: mapCfg.sizeMeters,
                offsetX: mapCfg.offsetX,
                offsetY: mapCfg.offsetY,
                offsetZ: mapCfg.offsetZ,
                scalePct: mapCfg.scalePct,
              } : null}
            />
          )}

            {/* Photo overlay of the camera we flew into — aligned by matching
                the viewer FOV to the lens, so model and photo superimpose */}
            {activeCameraView && photoOverlay && (
              <img
                src={`/uploads/inspections/${inspectionId}/photos/${activeCameraView}`}
                alt=""
                // centered via translate — `m-auto` with inset-0 does NOT center a
                // replaced element (over-constrained → left wins), which shifted the
                // overlay by half its overflow (~220 px here)
                className="absolute left-1/2 top-1/2 h-full w-auto max-w-none pointer-events-none select-none"
                style={{
                  opacity: photoOverlayOpacity,
                  // Match the 3D render exactly: center the photo, zoom it about
                  // the optical axis by the SAME factor the lens-FOV zoom uses
                  // (so both scale together), then shift by the lens' principal
                  // point so an off-centre PP still lines up. Order matters —
                  // scale sits between the two translates so the PP stays fixed
                  // at the container centre at every zoom.
                  transform: `translate(-50%, -50%) scale(${cameraViewZoom}) translate(${-(activeCameraPP?.[0] ?? 0) * 100}%, ${-(activeCameraPP?.[1] ?? 0) * 100}%)`,
                }}
              />
            )}

            {/* Camera viewpoint panel */}
            {activeCameraView && (
              <div className="absolute top-3 left-3 bg-black/70 text-white text-xs rounded-lg px-3 py-2 w-60 space-y-1.5">
                <div className="flex items-center gap-1.5">
                  <Video size={12} className="shrink-0" />
                  <span className="font-semibold truncate flex-1">{activeCameraView}</span>
                  <button
                    onClick={() => setActiveCameraView(null)}
                    className="text-white/50 hover:text-white"
                    title="Zavřít pohled kamery"
                  >
                    <X size={13} />
                  </button>
                </div>
                <p className="text-[10px] text-white/50 -mt-0.5">Pohled této kamery</p>

                <button
                  onClick={() => setPhotoOverlay(!photoOverlay)}
                  className={`w-full rounded px-2 py-1 text-[11px] font-semibold transition-colors
                    ${photoOverlay ? 'bg-brand-500 text-white' : 'bg-white/10 hover:bg-white/20'}`}
                >
                  {photoOverlay ? 'Skrýt překryv fotky' : 'Překrýt fotkou'}
                </button>

                {photoOverlay && (
                  <label className="block">
                    Průhlednost: {Math.round(photoOverlayOpacity * 100)} %
                    <input
                      type="range" min={0.05} max={1} step={0.05} value={photoOverlayOpacity}
                      onChange={e => setPhotoOverlayOpacity(parseFloat(e.target.value))}
                      className="w-full"
                    />
                  </label>
                )}

                <button
                  onClick={() => setCameraPhoto(activeCameraView)}
                  className="w-full rounded px-2 py-1 text-[11px] bg-white/10 hover:bg-white/20"
                >
                  Otevřít fotku v plné velikosti
                </button>
              </div>
            )}

            {/* Map underlay controls (collapsible) */}
            {showMap && hasCoords && (
              <div className={`absolute left-3 bg-black/65 text-white text-xs rounded-lg w-56 overflow-hidden ${activeCameraView ? 'top-[9.5rem]' : 'top-3'}`}>
                <button
                  onClick={() => setMapPanelOpen(o => !o)}
                  className="w-full flex items-center gap-1.5 px-3 py-2 font-semibold hover:bg-white/10 transition-colors"
                >
                  <Map size={12} /> Mapový podklad
                  <span className="flex-1" />
                  {mapPanelOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                </button>
                {mapPanelOpen && (
                  <div className="px-3 pb-2 space-y-1.5">
                    <label className="block">
                      Natočení (sever): {mapCfg.rotationDeg}°
                      <input
                        type="range" min={0} max={359} value={mapCfg.rotationDeg}
                        onChange={e => saveMapCfg({ ...mapCfg, rotationDeg: parseInt(e.target.value) })}
                        className="w-full"
                      />
                    </label>
                    <label className="block">
                      Velikost: {mapCfg.sizeMeters} m
                      <input
                        type="range" min={40} max={600} step={10} value={mapCfg.sizeMeters}
                        onChange={e => saveMapCfg({ ...mapCfg, sizeMeters: parseInt(e.target.value) })}
                        className="w-full"
                      />
                    </label>
                    <p className="font-semibold pt-1 border-t border-white/15">Kompenzace pozice</p>
                    <label className="block">
                      Posun X: {mapCfg.offsetX} m
                      <input
                        type="range" min={-80} max={80} step={0.5} value={mapCfg.offsetX}
                        onChange={e => saveMapCfg({ ...mapCfg, offsetX: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                    </label>
                    <label className="block">
                      Posun Z: {mapCfg.offsetZ} m
                      <input
                        type="range" min={-80} max={80} step={0.5} value={mapCfg.offsetZ}
                        onChange={e => saveMapCfg({ ...mapCfg, offsetZ: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                    </label>
                    <label className="block">
                      Výška Y: {mapCfg.offsetY} m
                      <input
                        type="range" min={-15} max={15} step={0.1} value={mapCfg.offsetY}
                        onChange={e => saveMapCfg({ ...mapCfg, offsetY: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                    </label>
                    <label className="block">
                      Měřítko: {mapCfg.scalePct} %
                      <input
                        type="range" min={25} max={400} step={1} value={mapCfg.scalePct}
                        onChange={e => saveMapCfg({ ...mapCfg, scalePct: parseInt(e.target.value) })}
                        className="w-full"
                      />
                    </label>
                    <button
                      onClick={() => saveMapCfg({ ...mapCfg, rotationDeg: 0, offsetX: 0, offsetY: 0, offsetZ: 0, scalePct: 100 })}
                      className="text-[10px] text-white/60 hover:text-white underline"
                    >
                      Vynulovat kompenzace
                    </button>
                    <p className="text-[9px] text-white/50">Podklad © Esri World Imagery · velikost dle kalibrace měřítka</p>
                  </div>
                )}
              </div>
            )}
            {/* Camera size control */}
            {showCameras && (
              <div className={`absolute left-3 bg-black/65 text-white text-xs rounded-lg px-3 py-2 w-56 ${activeCameraView ? 'top-[15rem]' : showMap && hasCoords ? 'top-14' : 'top-3'}`}
                   style={showMap && hasCoords && mapPanelOpen ? { top: 'auto', bottom: 12 } : undefined}>
                <label className="block">
                  Velikost kamer: {cameraSize.toFixed(2)}
                  <input
                    type="range" min={0.03} max={0.6} step={0.01} value={cameraSize}
                    onChange={e => setCameraSize(parseFloat(e.target.value))}
                    className="w-full"
                  />
                </label>
              </div>
            )}

            {showMap && !hasCoords && (
              <div className="absolute top-3 left-3 bg-amber-900/80 text-amber-100 text-xs rounded-lg px-3 py-2 w-64">
                Objekt nemá souřadnice — vyplňte zeměpisnou šířku/délku v „Upravit konstrukci" na kartě objektu.
              </div>
            )}

            {/* Model re-orientation — flip/rotate the whole model upright (e.g.
                a Z-up reality scan). Non-destructive, saved per inspection.
                A setup tool: shown only while the Info tab is open. */}
            {modelReady && activeTab === 'info' && !activeCameraView && tool !== 'polygon' && (
              <div className="absolute bottom-3 right-3 bg-black/65 text-white text-xs rounded-lg px-3 py-2 w-44">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="font-semibold flex items-center gap-1"><Move3d size={12} /> Orientace</span>
                  <button onClick={resetOrient} className="text-white/50 hover:text-white text-[10px]" title="Zpět na výchozí">reset</button>
                </div>
                {(['x', 'y', 'z'] as const).map(axis => (
                  <div key={axis} className="flex items-center gap-1.5 mb-1">
                    <span className="w-3 text-white/60 uppercase">{axis}</span>
                    <button onClick={() => rotateModel(axis, -90)}
                      className="flex-1 flex items-center justify-center gap-1 bg-white/10 hover:bg-white/20 rounded py-0.5">
                      <RotateCcw size={11} /> 90°
                    </button>
                    <button onClick={() => rotateModel(axis, 90)}
                      className="flex-1 flex items-center justify-center gap-1 bg-white/10 hover:bg-white/20 rounded py-0.5">
                      <RotateCw size={11} /> 90°
                    </button>
                  </div>
                ))}
                <p className="text-[9px] text-white/40 mt-0.5">Otočí celý model (uloží se).</p>
              </div>
            )}

          {/* Tool hints / polygon controls */}
          {tool === 'polygon' ? (
            <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex flex-col items-center gap-2">
              <div className="bg-black/75 text-white text-xs px-4 py-2 rounded-full pointer-events-none select-none">
                {polygonPoints.length === 0
                  ? 'Klikněte na model pro první vrchol polygonu'
                  : polygonPoints.length >= 3
                    ? `${polygonPoints.length} bodů · klikněte na zelený bod pro uzavření`
                    : `${polygonPoints.length} bod${polygonPoints.length >= 2 ? 'y' : ''} · klikněte pro další`}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={cancelPolygon}
                  className="bg-gray-800/80 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-gray-700 transition-colors"
                >
                  Zrušit (Esc)
                </button>
                <button
                  onClick={finishPolygon}
                  disabled={polygonPoints.length < 3}
                  className="bg-amber-500/90 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-amber-400 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  Dokončit polygon ({polygonPoints.length}/3+)
                </button>
                <button
                  onClick={cropToPolygon}
                  disabled={polygonPoints.length < 3 || cropping}
                  title="Ponechat jen oblast uvnitř polygonu, zbytek modelu skrýt"
                  className="bg-rose-600/90 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-rose-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1"
                >
                  <Scissors size={12} /> {cropping ? 'Ořezávám…' : 'Oříznout model'}
                </button>
              </div>
            </div>
          ) : tool === 'annotate' ? (
            <div className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-black/70 text-white text-xs px-4 py-2 rounded-full pointer-events-none">
              Klikněte na model pro označení poruchy
            </div>
          ) : tool === 'measure' ? (
            <div className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-black/70 text-white text-xs px-4 py-2 rounded-full pointer-events-none">
              Klikněte na 2 body pro změření vzdálenosti
            </div>
          ) : null}
        </div>

        {/* Right panel — full-width strip below the viewer on mobile */}
        <aside className="w-full lg:w-80 h-72 lg:h-auto shrink-0 bg-white border-t lg:border-t-0 lg:border-l border-gray-200 flex flex-col overflow-hidden">
          <div className="flex border-b border-gray-200">
            {(['annotations', 'measurements', 'photos', 'inspections', 'info'] as PanelTab[]).map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`flex-1 py-2.5 text-xs font-medium transition-colors relative
                  ${activeTab === tab
                    ? 'text-brand-600 border-b-2 border-brand-600'
                    : 'text-gray-500 hover:text-gray-700'}`}
              >
                {tab === 'annotations' ? 'Poruchy'
                  : tab === 'measurements' ? 'Měření'
                  : tab === 'inspections' ? 'Inspekce'
                  : tab === 'photos' ? (
                    <span className="flex items-center justify-center gap-1">
                      Foto
                      {clickedFaceInfo && (
                        <span className="w-1.5 h-1.5 rounded-full bg-brand-500 inline-block" />
                      )}
                    </span>
                  ) : 'Info'}
              </button>
            ))}
          </div>

          <div className="flex-1 overflow-y-auto" style={{ scrollbarGutter: 'stable' }}>
            {activeTab === 'annotations' && inspectionId && (
              <AnnotationPanel inspectionId={inspectionId} scaleFactor={inspection?.scale_factor ?? 1} />
            )}
            {activeTab === 'measurements' && inspectionId && structureId && (
              <MeasurementPanel
                inspectionId={inspectionId}
                structureId={structureId}
                scaleFactor={inspection?.scale_factor ?? 1}
              />
            )}
            {activeTab === 'photos' && inspectionId && (
              <PhotoSourcePanel inspectionId={inspectionId} structureId={structureId}
                canonical={inspection?.canonical ?? false}
                clickedFace={clickedFaceInfo} scaleFactor={inspection?.scale_factor ?? 1} />
            )}
            {activeTab === 'inspections' && inspectionId && structureId && (
              <InspectionsPanel structureId={structureId} currentId={inspectionId}
                referenceId={structure?.reference_inspection_id} />
            )}
            {activeTab === 'info' && inspection && (
              <InfoPanel inspection={inspection} onEdit={canEdit ? () => setShowEditInsp(true) : undefined} />
            )}
          </div>
        </aside>
      </div>

      {showUpload && inspectionId && (
        <PhotoUpload
          inspectionId={inspectionId}
          onClose={() => {
            setShowUpload(false)
            qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
          }}
        />
      )}

      {/* Photo opened by clicking a camera frustum */}
      {cameraPhoto && inspectionId && (
        <div
          className="fixed inset-0 z-[100] bg-black/92 flex items-center justify-center"
          onClick={() => setCameraPhoto(null)}
        >
          <button className="absolute top-4 right-4 text-white/70 hover:text-white bg-black/40 rounded-full p-2">
            <X size={20} />
          </button>
          <p className="absolute top-4 left-4 text-white/50 text-xs font-mono">{cameraPhoto}</p>
          <img
            src={`/uploads/inspections/${inspectionId}/photos/${cameraPhoto}`}
            alt={cameraPhoto}
            className="max-w-[92vw] max-h-[92vh] object-contain rounded shadow-2xl"
            onClick={e => e.stopPropagation()}
          />
        </div>
      )}

      {showEditInsp && inspection && structureId && (
        <EditInspectionModal
          structureId={structureId}
          inspection={inspection}
          onClose={() => setShowEditInsp(false)}
        />
      )}

      {showReprocess && inspectionId && (
        <ReprocessModal
          inspectionId={inspectionId}
          onClose={() => setShowReprocess(false)}
          onStarted={() => {
            setShowReprocess(false)
            setLiveProgress({ pct: 0, message: 'Spouštím rekonstrukci…' })
            qc.invalidateQueries({ queryKey: ['inspection', structureId, inspectionId] })
          }}
        />
      )}

      {showAiDefects && inspectionId && (
        <AiDefectsModal inspectionId={inspectionId} hasModel={!!inspection?.photo_set?.model_path}
          onClose={() => setShowAiDefects(false)} />
      )}
      {showQuality && inspectionId && (
        <QualityModal inspectionId={inspectionId} onClose={() => setShowQuality(false)} />
      )}
      {showBms && inspection && structureId && (
        <BmsProtocolModal structureId={structureId} inspection={inspection} onClose={() => setShowBms(false)} />
      )}
      {showReport && inspectionId && (
        <ReportModal inspectionId={inspectionId} onClose={() => setShowReport(false)} />
      )}

      {showAi && inspectionId && (
        <AiSegmentModal inspectionId={inspectionId} onClose={() => setShowAi(false)} />
      )}
    </div>
  )
}

// ─── Edit inspection modal ────────────────────────────────────────────────────

function EditInspectionModal({ structureId, inspection, onClose }: {
  structureId: string
  inspection: any
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [form, setForm] = useState({
    name: inspection.name ?? '',
    inspection_date: inspection.inspection_date ?? '',
    method: inspection.method ?? 'drone',
    status: inspection.status ?? 'draft',
    inspector_name: inspection.inspector_name ?? '',
    weather: inspection.weather ?? '',
    description: inspection.description ?? '',
  })

  const mut = useMutation({
    mutationFn: () => inspectionsApi.update(structureId, inspection.id, form as any),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['inspection', structureId, inspection.id] })
      qc.invalidateQueries({ queryKey: ['inspections', structureId] })
      onClose()
    },
  })

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={onClose}>
      <div className="card w-full max-w-lg mx-4 p-6 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <h2 className="text-lg font-semibold mb-5">Upravit inspekci</h2>
        <div className="space-y-4">
          <div>
            <label className="label">Název *</label>
            <input className="input" value={form.name} onChange={e => set('name', e.target.value)} autoFocus />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Datum *</label>
              <input className="input" type="date" value={form.inspection_date} onChange={e => set('inspection_date', e.target.value)} />
            </div>
            <div>
              <label className="label">Stav</label>
              <select className="input" value={form.status} onChange={e => set('status', e.target.value)}>
                <option value="draft">Koncept</option>
                <option value="in_progress">Probíhá</option>
                <option value="completed">Dokončená</option>
                <option value="archived">Archivovaná</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Metoda</label>
              <select className="input" value={form.method} onChange={e => set('method', e.target.value)}>
                <option value="drone">Dron</option>
                <option value="mobile">Mobilní zařízení</option>
                <option value="manual">Manuální</option>
                <option value="combined">Kombinovaná</option>
              </select>
            </div>
            <div>
              <label className="label">Inspektor</label>
              <input className="input" value={form.inspector_name} onChange={e => set('inspector_name', e.target.value)} />
            </div>
          </div>
          <div>
            <label className="label">Počasí</label>
            <input className="input" value={form.weather} onChange={e => set('weather', e.target.value)} placeholder="Jasno, 18 °C" />
          </div>
          <div>
            <label className="label">Poznámky</label>
            <textarea className="input h-16 resize-none" value={form.description} onChange={e => set('description', e.target.value)} />
          </div>
        </div>
        {mut.isError && (
          <p className="text-sm text-red-600 mt-3">
            {errorMessage(mut.error, 'Chyba při ukládání')}
          </p>
        )}
        <div className="flex justify-end gap-3 mt-6">
          <button onClick={onClose} className="btn-secondary">Zrušit</button>
          <button onClick={() => mut.mutate()} disabled={!form.name || mut.isPending} className="btn-primary disabled:opacity-50">
            {mut.isPending ? 'Ukládám…' : 'Uložit změny'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Reprocess modal ──────────────────────────────────────────────────────────

function ReprocessModal({ inspectionId, onClose, onStarted }: {
  inspectionId: string
  onClose: () => void
  onStarted: () => void
}) {
  const qc2 = useQueryClient()
  const [quality, setQuality] = useState<'draft' | 'standard' | 'high' | 'max'>('max')
  const [keepLargest, setKeepLargest] = useState(true)
  const [refineMesh, setRefineMesh] = useState(true)
  const [webPostprocess, setWebPostprocess] = useState(true)
  const [aiNormals, setAiNormals] = useState(true)
  const [aiDefects, setAiDefects] = useState(false)
  const [photoPrep, setPhotoPrep] = useState(false)
  const [pipeline, setPipeline] = useState('')   // '' = built-in default
  const [busy, setBusy] = useState(false)
  const mgRef = useRef<HTMLInputElement>(null)

  const { data: pipelines } = useQuery({
    queryKey: ['pipelines'],
    queryFn: configApi.listPipelines,
    retry: false,
  })

  const uploadMg = useMutation({
    mutationFn: (f: File) => configApi.uploadPipeline(f),
    onSuccess: (d: any) => {
      qc2.invalidateQueries({ queryKey: ['pipelines'] })
      if (d?.filename) setPipeline(d.filename)
    },
    onError: (e: any) => alert(errorMessage(e, 'Nahrání pipeline selhalo')),
  })

  const start = async () => {
    setBusy(true)
    try {
      await uploadsApi.reprocess(inspectionId, {
        quality,
        keep_largest_mesh: keepLargest,
        refine_mesh: refineMesh,
        web_postprocess: webPostprocess,
        ai_normals: aiNormals,
        ai_defects: aiDefects,
        photo_prep: photoPrep,
        pipeline: pipeline || undefined,
      })
      onStarted()
    } catch (err: any) {
      alert(errorMessage(err, 'Rekonstrukci se nepodařilo spustit'))
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] bg-black/50 flex items-center justify-center" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-[420px] p-5" onClick={e => e.stopPropagation()}>
        <h3 className="text-sm font-semibold text-gray-800 mb-4">Přegenerovat 3D model (AliceVision)</h3>

        {/* Pipeline template */}
        <label className="block text-xs font-medium text-gray-600 mb-1">Pipeline</label>
        <div className="flex gap-2 mb-1">
          <select className="input text-sm flex-1" value={pipeline} onChange={e => setPipeline(e.target.value)}>
            <option value="">Vestavěná (photogrammetry)</option>
            {pipelines?.pipelines.map(p => (
              <option key={p.filename} value={p.filename}>{p.filename}</option>
            ))}
          </select>
          <button
            onClick={() => mgRef.current?.click()}
            className="btn-secondary text-xs whitespace-nowrap"
            title="Importovat pipeline exportovanou z Meshroom GUI (Soubor → Uložit jako .mg)"
          >
            {uploadMg.isPending ? '…' : '+ Import .mg'}
          </button>
          <input
            ref={mgRef} type="file" accept=".mg" className="hidden"
            onChange={e => { const f = e.target.files?.[0]; if (f) uploadMg.mutate(f); e.target.value = '' }}
          />
        </div>
        <p className="text-[10px] text-gray-400 mb-3">
          Vlastní graf uzlů z Meshroomu. Preset kvality se aplikuje jen na uzly se shodnými názvy;
          průběh se hlásí podle standardních uzlů.
        </p>

        <label className="block text-xs font-medium text-gray-600 mb-1">Kvalita</label>
        <div className="space-y-1.5 mb-3">
          {([
            ['draft', 'Náhled — rychlé (~20 min), hrubší síť, textury 4K/half-res'],
            ['standard', 'Standard — vyvážené (~1 h), 2M bodů, ostré 8K textury'],
            ['high', 'Vysoká — pomalé (hodiny), plná hloubka, 5M bodů, ostré 8K textury'],
            ['max', 'Maximální přesnost — OpenMVS: fotometricky zpřesněná síť (~2× přesnější), uzavřené díry; nejpomalejší'],
          ] as const).map(([val, label]) => (
            <label key={val} className="flex items-start gap-2 text-xs text-gray-700 cursor-pointer">
              <input
                type="radio"
                name="quality"
                checked={quality === val}
                onChange={() => setQuality(val)}
                className="mt-0.5"
              />
              <span><b className="capitalize">{({ draft: 'Náhled', standard: 'Standard', high: 'Vysoká', max: 'Maximální přesnost' } as const)[val]}</b> — {label.split('— ')[1]}</span>
            </label>
          ))}
        </div>

        {quality === 'max' && (
          <label className="flex items-start gap-2 text-xs text-gray-700 mb-2 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={aiNormals} onChange={e => setAiNormals(e.target.checked)} />
            <span>
              AI odšumění povrchu — neuronová síť (DSINE) odhadne z každé fotky natočení ploch a podle
              nich se z modelu odstraní „chlupatý“ šum; rovné stěny zůstanou rovné, hrany a trhliny ostré
            </span>
          </label>
        )}

        <label className="flex items-start gap-2 text-xs text-gray-700 mb-2 cursor-pointer">
          <input type="checkbox" className="mt-0.5" checked={photoPrep} onChange={e => setPhotoPrep(e.target.checked)} />
          <span>Příprava fotek — vyrovná expozici podle metadat, zesvětlí stíny (ostré slunce) a vyřadí rozmazané
            a téměř stejné záběry. U DNG se tento krok přeskočí a SfM používá původní RAW.</span>
        </label>

        <label className="flex items-start gap-2 text-xs text-gray-700 mb-2 cursor-pointer">
          <input type="checkbox" className="mt-0.5" checked={aiDefects} onChange={e => setAiDefects(e.target.checked)} />
          <span>AI inspekce poruch — souběžně projde fotky, najde trhliny a výlomy a zanese je do modelu jako poruchy k ověření</span>
        </label>

        <label className="flex items-center gap-2 text-xs text-gray-700 mb-2 cursor-pointer">
          <input type="checkbox" checked={keepLargest} onChange={e => setKeepLargest(e.target.checked)} />
          Vyčistit plovoucí fragmenty (ponechat jen hlavní souvislou síť)
        </label>

        <label className="flex items-start gap-2 text-xs text-gray-700 mb-4 cursor-pointer">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={refineMesh}
            onChange={e => setRefineMesh(e.target.checked)}
          />
          <span>
            <b>Uzavřít díry a vyčistit síť (PyMeshLab)</b> — zacelí všechny díry kromě
            vnějšího okraje skenu, odstraní úlomky a opraví topologii; změřená geometrie
            zůstane beze změny (žádné vyhlazování ani zjednodušení). Textury se promítnou
            na opravenou síť. Doporučeno.
          </span>
        </label>

        <label className="flex items-start gap-2 text-xs text-gray-700 mb-4 cursor-pointer">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={webPostprocess}
            onChange={e => setWebPostprocess(e.target.checked)}
          />
          <span>
            <b>Web post-processing</b> — konverze textur EXR→JPG, LOD úrovně a mobilní
            varianta. Při vypnutí zůstane surový výstup pipeline (OBJ) — vhodné jen pro
            export/ladění, viewer nemusí zobrazit textury.
          </span>
        </label>

        <p className="text-[11px] text-amber-600 bg-amber-50 border border-amber-200 rounded p-2 mb-4">
          Stávající model bude nahrazen. Rekonstrukce běží na pozadí — průběh uvidíte v modrém pruhu.
        </p>

        <div className="flex gap-2 justify-end">
          <button onClick={onClose} className="btn-secondary text-xs">Zrušit</button>
          <button onClick={start} disabled={busy} className="btn-primary text-xs">
            {busy ? 'Spouštím…' : 'Spustit rekonstrukci'}
          </button>
        </div>
      </div>
    </div>
  )
}

function ToolBtn({ active, onClick, title, children }: {
  active: boolean; onClick: () => void; title: string; children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`p-2 rounded-md transition-colors text-sm
        ${active ? 'bg-white shadow-sm text-brand-600' : 'text-gray-500 hover:text-gray-700'}`}
    >
      {children}
    </button>
  )
}

function InspectionsPanel({ structureId, currentId, referenceId }: { structureId: string; currentId: string; referenceId?: string }) {
  const navigate = useNavigate()
  const { data: inspections = [] } = useQuery({
    queryKey: ['inspections', structureId],
    queryFn: () => inspectionsApi.list(structureId),
    enabled: !!structureId,
  })
  const sorted = [...inspections].sort((a, b) =>
    new Date(b.inspection_date).getTime() - new Date(a.inspection_date).getTime())
  const current = inspections.find(i => i.id === currentId)

  return (
    <div className="p-3 space-y-1.5">
      {sorted.length <= 1 && (
        <p className="text-xs text-gray-400 py-2 text-center">Konstrukce má jen tuto inspekci.</p>
      )}
      {sorted.map(i => {
        const active = i.id === currentId
        // compare with the older one as A (reference), the newer as B
        const older = current && new Date(i.inspection_date) <= new Date(current.inspection_date) ? i.id : currentId
        const newer = older === i.id ? currentId : i.id
        return (
          <div key={i.id}
            className={`rounded-lg border px-3 py-2 flex items-center gap-2
              ${active ? 'border-brand-300 bg-brand-50' : 'border-gray-200 hover:bg-gray-50'}`}>
            <button
              onClick={() => !active && navigate(`/structures/${structureId}/inspections/${i.id}`)}
              disabled={active}
              className="flex-1 min-w-0 text-left disabled:cursor-default"
              title={active ? 'Aktuální inspekce' : 'Přepnout na tuto inspekci'}
            >
              <div className="text-sm font-medium text-gray-800 truncate flex items-center gap-1">
                {i.name}
                {i.id === referenceId && (
                  <span className="badge bg-amber-100 text-amber-700 text-[9px] flex items-center gap-0.5 shrink-0" title="Referenční model">
                    <Crosshair size={9} /> Ref
                  </span>
                )}
                {active && <span className="text-[10px] font-semibold text-brand-600 uppercase">· zde</span>}
                {!active && <ArrowRight size={12} className="text-gray-300 shrink-0" />}
              </div>
              <div className="text-[11px] text-gray-400">
                {new Date(i.inspection_date).toLocaleDateString('cs-CZ')} · {i.annotation_count} poruch
              </div>
            </button>
            {!active && (
              <button
                onClick={() => navigate(`/structures/${structureId}/compare?a=${older}&b=${newer}`)}
                title="Porovnat s aktuální inspekcí"
                className="shrink-0 p-1.5 rounded-md text-brand-600 hover:bg-brand-100"
              >
                <GitCompareArrows size={15} />
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

function InfoPanel({ inspection, onEdit }: { inspection: any; onEdit?: () => void }) {
  const rows = [
    ['Datum', new Date(inspection.inspection_date).toLocaleDateString('cs-CZ')],
    ['Stav', STATUS_LABELS[inspection.status as keyof typeof STATUS_LABELS] ?? inspection.status],
    ['Metoda', METHOD_LABELS[inspection.method as keyof typeof METHOD_LABELS] ?? inspection.method ?? '—'],
    ['Inspektor', inspection.inspector_name ?? '—'],
    ['Počasí', inspection.weather ?? '—'],
    ['Poruchy', inspection.annotation_count],
    ['Měření', inspection.measurement_count],
  ]
  return (
    <div className="p-4 space-y-2">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-700">Detail inspekce</h3>
        {onEdit && (
          <button onClick={onEdit} className="btn-ghost text-xs text-brand-600 py-1">
            <Pencil size={12} /> Upravit
          </button>
        )}
      </div>
      {rows.map(([label, value]) => (
        <div key={label as string} className="flex justify-between text-sm py-1 border-b border-gray-50">
          <span className="text-gray-500">{label}</span>
          <span className="font-medium text-gray-800">{value}</span>
        </div>
      ))}
      {inspection.description && (
        <div className="pt-2">
          <p className="text-xs text-gray-500 mb-1">Poznámky</p>
          <p className="text-sm text-gray-700">{inspection.description}</p>
        </div>
      )}
    </div>
  )
}
