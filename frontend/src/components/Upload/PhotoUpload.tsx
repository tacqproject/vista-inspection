import { useState, useCallback, useEffect } from 'react'
import { useDropzone } from 'react-dropzone'
import { Upload, X, Image, FileArchive, CheckCircle, AlertCircle, Box, Cpu, Loader2, Film } from 'lucide-react'
import { uploadsApi, photosApi, configApi } from '../../api/inspections'
import type { EngineStatus } from '../../api/inspections'
import { errorMessage } from '../../api/client'

interface Props {
  inspectionId: string
  onClose: () => void
}

type Tab = 'photos' | 'model'

export default function PhotoUpload({ inspectionId, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('photos')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="card w-full max-w-xl mx-4 p-0 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-5 pb-0">
          <h3 className="font-semibold text-gray-900">Správa 3D modelu</h3>
          <button onClick={onClose} className="btn-ghost p-1"><X size={16} /></button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-200 mt-4 px-6 gap-4">
          <TabBtn active={tab === 'photos'} onClick={() => setTab('photos')}>
            <Image size={14} /> Fotografie
          </TabBtn>
          <TabBtn active={tab === 'model'} onClick={() => setTab('model')}>
            <Box size={14} /> Import 3D modelu
          </TabBtn>
        </div>

        <div className="p-6">
          {tab === 'photos'
            ? <PhotosTab inspectionId={inspectionId} onClose={onClose} />
            : <ModelTab inspectionId={inspectionId} onClose={onClose} />}
        </div>
      </div>
    </div>
  )
}

// ─── Tabs ─────────────────────────────────────────────────────────────────────

function TabBtn({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 pb-2.5 text-sm font-medium border-b-2 transition-colors
        ${active ? 'text-brand-600 border-brand-600' : 'text-gray-500 border-transparent hover:text-gray-700'}`}
    >
      {children}
    </button>
  )
}

// ─── Engine status (AliceVision only) ─────────────────────────────────────────

function EngineStatusBanner({ status, loading }: { status: EngineStatus | null; loading: boolean }) {
  if (loading) {
    return (
      <div className="mb-4 flex items-center gap-2 text-xs text-gray-400">
        <Loader2 size={13} className="animate-spin" /> Zjišťuji dostupnost AliceVision…
      </div>
    )
  }
  const available = status?.meshroom?.available
  return (
    <div className={`mb-4 rounded-lg px-3 py-2 text-xs border flex items-center gap-2
      ${available ? 'bg-green-50 border-green-200 text-green-800' : 'bg-red-50 border-red-200 text-red-700'}`}>
      <Cpu size={13} className="shrink-0" />
      {available
        ? <span><b>AliceVision (Meshroom)</b> připraven — rekonstrukce poběží na GPU na pozadí (~1 h dle počtu snímků).</span>
        : <span><b>AliceVision nenalezen.</b> Zkontrolujte MESHROOM_PATH v .env na serveru.</span>}
    </div>
  )
}

// ─── Photos tab ───────────────────────────────────────────────────────────────

function PhotosTab({ inspectionId, onClose }: { inspectionId: string; onClose: () => void }) {
  const [files, setFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // AliceVision availability
  const [engineStatus, setEngineStatus] = useState<EngineStatus | null>(null)
  const [engineLoading, setEngineLoading] = useState(true)

  useEffect(() => {
    configApi.engines()
      .then(setEngineStatus)
      .catch(() => setEngineStatus(null))
      .finally(() => setEngineLoading(false))
  }, [])

  const onDrop = useCallback((accepted: File[]) => {
    setFiles(f => {
      const existing = new Set(f.map(x => x.name))
      return [...f, ...accepted.filter(a => !existing.has(a.name))]
    })
  }, [])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      'image/*': ['.jpg', '.jpeg', '.png', '.tif', '.tiff'],
      'video/*': ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v'],
      'application/zip': ['.zip'],
      'application/x-zip-compressed': ['.zip'],
    },
    multiple: true,
  })

  const handleUpload = async () => {
    if (!files.length) return
    setUploading(true)
    setError(null)
    try {
      await uploadsApi.uploadPhotos(inspectionId, files, setProgress)
      setDone(true)
      setTimeout(onClose, 1500)
    } catch (e: any) {
      setError(errorMessage(e, 'Chyba při nahrávání'))
    } finally {
      setUploading(false)
    }
  }

  const fmt = (b: number) => b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`

  if (done) {
    return (
      <div className="text-center py-8">
        <CheckCircle size={48} className="mx-auto text-green-500 mb-3" />
        <p className="font-semibold text-green-700">Snímky nahrány!</p>
        <p className="text-sm text-gray-500 mt-1">AliceVision rekonstrukce probíhá na pozadí (GPU)…</p>
      </div>
    )
  }

  return (
    <>
      {/* AliceVision status */}
      <EngineStatusBanner status={engineStatus} loading={engineLoading} />

      {/* Dropzone */}
      <div
        {...getRootProps()}
        className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors mb-4
          ${isDragActive ? 'border-brand-400 bg-brand-50' : 'border-gray-300 hover:border-brand-300 hover:bg-gray-50'}`}
      >
        <input {...getInputProps()} />
        <Upload size={32} className="mx-auto text-gray-400 mb-3" />
        <p className="text-sm font-medium text-gray-700">
          {isDragActive ? 'Pusťte soubory zde…' : 'Přetáhněte snímky nebo video, nebo klikněte'}
        </p>
        <p className="text-xs text-gray-400 mt-1">JPG, PNG, TIFF, ZIP archiv nebo video (MP4/MOV…)</p>
        <p className="text-[11px] text-gray-400 mt-0.5">Z videa se automaticky vyberou ostré snímky pro rekonstrukci.</p>
      </div>

      {files.length > 0 && (
        <div className="mb-4 max-h-36 overflow-y-auto space-y-1">
          {files.map(f => (
            <div key={f.name} className="flex items-center gap-2 text-sm py-1 px-2 bg-gray-50 rounded-lg">
              {f.name.toLowerCase().endsWith('.zip')
                ? <FileArchive size={14} className="text-orange-500 shrink-0" />
                : /\.(mp4|mov|avi|mkv|webm|m4v|mpe?g|wmv)$/i.test(f.name)
                ? <Film size={14} className="text-purple-500 shrink-0" />
                : <Image size={14} className="text-brand-500 shrink-0" />}
              <span className="flex-1 truncate text-gray-700">{f.name}</span>
              <span className="text-xs text-gray-400 shrink-0">{fmt(f.size)}</span>
              {!uploading && (
                <button onClick={() => setFiles(f2 => f2.filter(x => x.name !== f.name))} className="text-gray-400 hover:text-red-500">
                  <X size={12} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {uploading && (
        <div className="mb-4">
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>Nahrávám…</span><span>{progress}%</span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div className="bg-brand-500 h-2 rounded-full transition-all" style={{ width: `${progress}%` }} />
          </div>
        </div>
      )}

      {error && (
        <div className="mb-4 flex items-center gap-2 text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">
          <AlertCircle size={15} /> {error}
        </div>
      )}

      <div className="flex justify-end gap-3">
        <button onClick={onClose} className="btn-secondary" disabled={uploading}>Zrušit</button>
        <button onClick={handleUpload} disabled={!files.length || uploading} className="btn-primary disabled:opacity-50">
          <Upload size={14} /> {uploading ? 'Nahrávám…' : `Nahrát (${files.length})`}
        </button>
      </div>
    </>
  )
}

// ─── Model import tab ─────────────────────────────────────────────────────────

function ModelTab({ inspectionId, onClose }: { inspectionId: string; onClose: () => void }) {
  const [files, setFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const onDrop = useCallback((accepted: File[]) => {
    setFiles(f => {
      const existing = new Set(f.map(x => x.name))
      return [...f, ...accepted.filter(a => !existing.has(a.name))]
    })
  }, [])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      'model/obj': ['.obj'],
      'model/mtl': ['.mtl'],
      'model/gltf-binary': ['.glb'],
      'model/gltf+json': ['.gltf'],
      'image/*': ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'],
      'application/octet-stream': ['.obj', '.mtl', '.glb', '.gltf', '.fbx', '.ply', '.bin'],
    },
    multiple: true,
  })

  // any importable mesh (photogrammetry OBJ or a reality-scan export)
  const hasMesh = files.some(f => /\.(obj|glb|gltf|fbx|ply)$/i.test(f.name))

  const handleImport = async () => {
    if (!hasMesh) return
    setUploading(true)
    setError(null)
    try {
      await photosApi.importModel(inspectionId, files, setProgress)
      setDone(true)
      setTimeout(onClose, 1500)
    } catch (e: any) {
      setError(errorMessage(e, 'Chyba při importu'))
    } finally {
      setUploading(false)
    }
  }

  if (done) {
    return (
      <div className="text-center py-8">
        <CheckCircle size={48} className="mx-auto text-green-500 mb-3" />
        <p className="font-semibold text-green-700">Model importován!</p>
        <p className="text-sm text-gray-500 mt-1">3D model je připraven k prohlížení.</p>
      </div>
    )
  }

  return (
    <>
      {/* Tools info */}
      <div className="mb-4 bg-blue-50 border border-blue-200 rounded-lg px-3 py-3 text-xs text-blue-800 space-y-1.5">
        <p className="font-semibold">Jak získat 3D model / naskenovat realitu:</p>
        <div className="space-y-1">
          <Tool name="RealityScan / RealityCapture" desc="iOS/desktop · export GLB/OBJ/FBX/PLY" badge="Reality scan" />
          <Tool name="Polycam / Scaniverse" desc="iOS/Android · LiDAR i foto · export GLB/OBJ/GLTF" />
          <Tool name="Luma AI" desc="Zdarma · web · ideální pro interiéry · export OBJ/GLB" />
          <Tool name="Meshroom / COLMAP" desc="Zdarma · desktop · offline (záložka Fotografie)" />
        </div>
        <p className="text-blue-600 pt-0.5">
          Nejlépe <b>.glb</b> (vše v jednom souboru). Nebo <b>.obj</b> (+ .mtl a textury),
          případně <b>.gltf</b>/<b>.fbx</b>/<b>.ply</b> (mesh i mračno bodů).
        </p>
      </div>

      {/* Dropzone */}
      <div
        {...getRootProps()}
        className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors mb-4
          ${isDragActive ? 'border-brand-400 bg-brand-50' : 'border-gray-300 hover:border-brand-300 hover:bg-gray-50'}`}
      >
        <input {...getInputProps()} />
        <Box size={32} className="mx-auto text-gray-400 mb-3" />
        <p className="text-sm font-medium text-gray-700">
          {isDragActive ? 'Pusťte soubory zde…' : 'Přetáhněte model z reality scanu'}
        </p>
        <p className="text-xs text-gray-400 mt-1">GLB · glTF · OBJ (+.mtl+textury) · FBX · PLY</p>
      </div>

      {files.length > 0 && (
        <div className="mb-4 max-h-36 overflow-y-auto space-y-1">
          {files.map(f => {
            const meshExt = f.name.toLowerCase().match(/\.(obj|glb|gltf|fbx|ply)$/)?.[1]
            const isMtl = f.name.toLowerCase().endsWith('.mtl')
            return (
              <div key={f.name} className={`flex items-center gap-2 text-sm py-1 px-2 rounded-lg ${meshExt ? 'bg-brand-50' : 'bg-gray-50'}`}>
                <span className={`text-[10px] font-bold w-8 shrink-0 ${meshExt ? 'text-brand-600' : isMtl ? 'text-amber-600' : 'text-gray-400'}`}>
                  {meshExt ? meshExt.toUpperCase() : isMtl ? 'MTL' : 'TEX'}
                </span>
                <span className="flex-1 truncate text-gray-700">{f.name}</span>
                {!uploading && (
                  <button onClick={() => setFiles(f2 => f2.filter(x => x.name !== f.name))} className="text-gray-400 hover:text-red-500">
                    <X size={12} />
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}

      {!hasMesh && files.length > 0 && (
        <p className="mb-3 text-xs text-amber-600 flex items-center gap-1">
          <AlertCircle size={12} /> Přidejte model: GLB, glTF, OBJ, FBX nebo PLY
        </p>
      )}

      {uploading && (
        <div className="mb-4">
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>Nahrávám model…</span><span>{progress}%</span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div className="bg-brand-500 h-2 rounded-full transition-all" style={{ width: `${progress}%` }} />
          </div>
        </div>
      )}

      {error && (
        <div className="mb-4 flex items-center gap-2 text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">
          <AlertCircle size={15} /> {error}
        </div>
      )}

      <div className="flex justify-end gap-3">
        <button onClick={onClose} className="btn-secondary" disabled={uploading}>Zrušit</button>
        <button onClick={handleImport} disabled={!hasMesh || uploading} className="btn-primary disabled:opacity-50">
          <Box size={14} /> {uploading ? 'Nahrávám…' : 'Importovat model'}
        </button>
      </div>
    </>
  )
}

function Tool({ name, desc, badge }: { name: string; desc: string; badge?: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-semibold text-blue-900 w-24 shrink-0">{name}</span>
      {badge && <span className="bg-green-100 text-green-700 text-[9px] font-bold px-1.5 py-0.5 rounded shrink-0">{badge}</span>}
      <span className="text-blue-700">{desc}</span>
    </div>
  )
}
