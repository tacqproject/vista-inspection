import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, Workflow, Braces, ChevronRight, Image, Box, Cpu } from 'lucide-react'
import { api } from '../api/client'

interface Stage { name: string; doc: string }
interface EngineDoc { name: string; doc: string; stages: Stage[] }
interface StepDoc { name: string; order: number; doc: string }
interface PipelineDocs { engines: EngineDoc[]; postprocess: StepDoc[] }

const STEP_LABELS: Record<string, string> = {
  'refine-mesh': 'Čištění sítě (PyMeshLab)',
  'harvest-cameras': 'Uložení pozic kamer',
  'convert-exr-textures': 'Konverze textur EXR→JPG',
  'generate-lods': 'Generování LOD úrovní',
  'mobile-variant': 'Mobilní varianta (2K)',
}

export default function DocsPage() {
  const [tab, setTab] = useState<'pipeline' | 'api'>('pipeline')

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center gap-3 mb-6">
        <BookOpen size={22} className="text-brand-600" />
        <h1 className="text-2xl font-bold text-gray-900">Dokumentace</h1>
        <div className="flex rounded-lg overflow-hidden border border-gray-200 ml-4">
          <button
            onClick={() => setTab('pipeline')}
            className={`px-4 py-1.5 text-sm flex items-center gap-1.5
              ${tab === 'pipeline' ? 'bg-brand-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
          >
            <Workflow size={14} /> Pipeline rekonstrukce
          </button>
          <button
            onClick={() => setTab('api')}
            className={`px-4 py-1.5 text-sm flex items-center gap-1.5
              ${tab === 'api' ? 'bg-brand-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
          >
            <Braces size={14} /> REST API
          </button>
        </div>
      </div>

      {tab === 'pipeline' ? <PipelineTab /> : <ApiTab />}
    </div>
  )
}

// ─── Pipeline node graph ──────────────────────────────────────────────────────

function PipelineTab() {
  const { data } = useQuery({
    queryKey: ['pipeline-docs'],
    queryFn: () => api.get<PipelineDocs>('/config/pipeline').then(r => r.data),
    staleTime: Infinity,
  })
  const [selected, setSelected] = useState<{ title: string; doc: string } | null>(null)

  if (!data) return <p className="text-gray-400">Načítám…</p>

  const meshroom = data.engines.find(e => e.name === 'meshroom')

  return (
    <div>
      <p className="text-sm text-gray-500 mb-5 max-w-3xl">
        Živý pohled na skutečnou pipeline (generováno introspekce backendu — vždy odpovídá kódu).
        Kliknutím na uzel zobrazíte podrobnosti. Nový zpracovatelský modul = jeden soubor
        v <code className="text-xs bg-gray-100 px-1 rounded">app/services/photogrammetry/postprocess/</code> s
        dekorátorem <code className="text-xs bg-gray-100 px-1 rounded">@postprocess_step</code>.
      </p>

      {/* Row 1: input → engine stages */}
      <SectionLabel icon={<Cpu size={13} />} text="Engine: Meshroom (AliceVision) — z fotek na surovou síť" />
      <div className="flex items-stretch gap-1 flex-wrap mb-6">
        <Node title="Fotky" subtitle="vstup" color="slate" icon={<Image size={13} />}
          onClick={() => setSelected({ title: 'Vstupní fotky', doc: 'Přehledové snímky objektu (60–150 ks, překryv ~70 %). Nahrávají se jednotlivě nebo jako ZIP; detailní snímky poruch se přidávají zvlášť k anotacím.' })} />
        {meshroom?.stages.map(s => (
          <div key={s.name} className="flex items-center gap-1">
            <Arrow />
            <Node
              title={s.name}
              subtitle={s.name === 'DepthMap' ? 'GPU' : undefined}
              color={s.name === 'DepthMap' ? 'amber' : 'sky'}
              onClick={() => setSelected({ title: s.name, doc: s.doc })}
            />
          </div>
        ))}
      </div>

      {/* Row 2: postprocess chain */}
      <SectionLabel icon={<Workflow size={13} />} text="Post-processing — modulární řetěz (řazeno dle order)" />
      <div className="flex items-stretch gap-1 flex-wrap mb-6">
        <Node title="Surová síť" subtitle="z enginu" color="slate" icon={<Box size={13} />}
          onClick={() => setSelected({ title: 'Surová síť', doc: 'Výstup enginu — hustá trojúhelníková síť s dírami v zakrytých místech, případně s texturami (bez PyMeshLab čištění).' })} />
        {data.postprocess.map(s => (
          <div key={s.name} className="flex items-center gap-1">
            <Arrow />
            <Node
              title={STEP_LABELS[s.name] ?? s.name}
              subtitle={`order ${s.order}`}
              color="emerald"
              onClick={() => setSelected({ title: STEP_LABELS[s.name] ?? s.name, doc: s.doc })}
            />
          </div>
        ))}
        <Arrow />
        <Node title="Model ve vieweru" subtitle="glTF + LOD" color="slate" icon={<Box size={13} />}
          onClick={() => setSelected({ title: 'Výsledek', doc: 'Texturovaný glTF model ve 3 LOD úrovních + mobilní varianta, pozice kamer (cameras.sfm) a SfM landmarky (sfm.json) pro projekci kliknutí do fotek.' })} />
      </div>

      {/* Engines overview */}
      <SectionLabel icon={<Cpu size={13} />} text="Dostupné enginy" />
      <div className="flex gap-2 flex-wrap mb-6">
        {data.engines.map(e => (
          <Node key={e.name} title={e.name} color="violet"
            onClick={() => setSelected({ title: `Engine: ${e.name}`, doc: e.doc })} />
        ))}
      </div>

      {/* Detail panel */}
      {selected && (
        <div className="card p-5 border-brand-200 bg-brand-50/40">
          <h3 className="font-semibold text-gray-900 mb-2">{selected.title}</h3>
          <pre className="text-xs text-gray-600 whitespace-pre-wrap font-sans leading-relaxed">{selected.doc || 'Bez popisu.'}</pre>
        </div>
      )}
    </div>
  )
}

function SectionLabel({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
      {icon} {text}
    </p>
  )
}

const NODE_COLORS: Record<string, string> = {
  slate: 'bg-slate-100 border-slate-300 text-slate-700 hover:border-slate-400',
  sky: 'bg-sky-50 border-sky-300 text-sky-800 hover:border-sky-500',
  amber: 'bg-amber-50 border-amber-300 text-amber-800 hover:border-amber-500',
  emerald: 'bg-emerald-50 border-emerald-300 text-emerald-800 hover:border-emerald-500',
  violet: 'bg-violet-50 border-violet-300 text-violet-800 hover:border-violet-500',
}

function Node({ title, subtitle, color, icon, onClick }: {
  title: string
  subtitle?: string
  color: string
  icon?: React.ReactNode
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={`border-2 rounded-lg px-2.5 py-1.5 text-left transition-colors ${NODE_COLORS[color]}`}
    >
      <span className="text-[11px] font-semibold flex items-center gap-1 whitespace-nowrap">
        {icon} {title}
      </span>
      {subtitle && <span className="text-[9px] opacity-70 block">{subtitle}</span>}
    </button>
  )
}

function Arrow() {
  return <ChevronRight size={14} className="text-gray-300 shrink-0 self-center" />
}

// ─── API docs (Swagger UI z backendu) ─────────────────────────────────────────

function ApiTab() {
  const swaggerUrl = `${window.location.protocol}//${window.location.hostname}:8000/docs`
  return (
    <div>
      <p className="text-sm text-gray-500 mb-3">
        Interaktivní Swagger dokumentace generovaná přímo z FastAPI backendu
        (<a className="text-brand-600 underline" href={swaggerUrl} target="_blank" rel="noreferrer">otevřít v novém okně</a>).
        Každý endpoint lze rovnou vyzkoušet tlačítkem „Try it out".
      </p>
      <iframe
        src={swaggerUrl}
        title="API dokumentace"
        className="w-full rounded-xl border border-gray-200 bg-white"
        style={{ height: 'calc(100vh - 220px)' }}
      />
    </div>
  )
}
