import { useMemo, useEffect } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useCameraSyncStore } from '../store/cameraSyncStore'
import { ArrowLeft, GitCompareArrows, TrendingUp, TrendingDown, Minus, Plus, CheckCircle2, Layers, Crosshair, AlertTriangle, EyeOff, MousePointerClick, SlidersHorizontal, ChevronDown, FileText } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { api, errorMessage } from '../api/client'
import { inspectionsApi, reportsApi } from '../api/inspections'
import { structuresApi } from '../api/structures'
import { DEFECT_LABELS, SEVERITY_LABELS, SEVERITY_BG } from '../types'
import type { Annotation, Severity } from '../types'
import Viewer3D from '../components/Viewer3D/Viewer3D'
import { useViewerStore } from '../store/viewerStore'
import { useAuthStore } from '../store/authStore'

interface ComparePair {
  old: Annotation
  new: Annotation
  match_distance: number
  status: 'worse' | 'improved' | 'unchanged'
  severity_delta: number
}

interface CompareResult {
  aligned?: boolean
  scene_matrix?: number[] | null
  pairs: ComparePair[]
  new: Annotation[]
  resolved: Annotation[]
  uncovered?: Annotation[]
  summary: { matched: number; worse: number; improved: number; unchanged: number; new: number; resolved: number; uncovered?: number }
  inspection_a: { id: string; name: string; date: string }
  inspection_b: { id: string; name: string; date: string }
}

export default function ComparePage() {
  const { structureId } = useParams<{ structureId: string }>()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const { setSelectedAnnotation, selectedAnnotation } = useViewerStore()
  const a = params.get('a'), b = params.get('b')
  const [overlayMode, setOverlayMode] = useState(false)
  const [overlayOpacity, setOverlayOpacity] = useState(0.6)
  const [baseOpacityA, setBaseOpacityA] = useState(1)
  // manual fine-tune of B in the overlay (A's scene frame): translate + rotate + scale
  const ZERO_ADJ = { tx: 0, ty: 0, tz: 0, rx: 0, ry: 0, rz: 0, sc: 1 }
  const [adjust, setAdjust] = useState(ZERO_ADJ)
  const [reportBusy, setReportBusy] = useState(false)
  const canEdit = useAuthStore(s => s.canEdit())

  // manual point-pair alignment
  const [manualMode, setManualMode] = useState(false)
  const [aPts, setAPts] = useState<Array<{ x: number; y: number; z: number }>>([])
  const [bPts, setBPts] = useState<Array<{ x: number; y: number; z: number }>>([])
  // guide the user to keep the two sides balanced: pick A until it's ahead, then B
  const nextSide: 'a' | 'b' = aPts.length <= bPts.length ? 'a' : 'b'
  const pairCount = Math.min(aPts.length, bPts.length)
  const resetManual = () => { setAPts([]); setBPts([]) }
  const exitManual = () => { setManualMode(false); resetManual() }
  const undoLast = () => {
    // remove the most recently added point (the side that's ahead, else B)
    if (aPts.length > bPts.length) setAPts(v => v.slice(0, -1))
    else if (bPts.length > 0) setBPts(v => v.slice(0, -1))
    else setAPts(v => v.slice(0, -1))
  }

  const manualMut = useMutation({
    mutationFn: () => inspectionsApi.alignManual(
      structureId!, b!, a!,
      Array.from({ length: pairCount }, (_, i) => ({ a: aPts[i], b: bPts[i] })),
    ),
    onSuccess: (data: any) => {
      exitManual(); refetchInspections()
      const res: number[] | undefined = data?.pair_residuals
      let msg = ''
      if (res?.length && data?.metric) {
        const cm = (m: number) => `${Math.round(m * 100)} cm`
        const lines = res.map((r, i) => `  • dvojice ${i + 1}: ${cm(r)} ${r > 0.15 ? '⚠️' : ''}`)
        const worst = Math.max(...res)
        msg = `Odchylka dvojic (jak daleko od sebe páry sedí):\n${lines.join('\n')}\n\n`
          + (worst > 0.15
            ? 'Dvojice s ⚠️ míří na jiné fyzické místo — přiblížit a překlikat, nebo smazat.'
            : 'Všechny páry sedí těsně. Pokud to i tak vypadá špatně, chybí bod v jiné hloubce (ne všechny na líci).')
      }
      if (data?.warning) msg = data.warning + (msg ? '\n\n' + msg : '')
      if (msg) alert(msg)
    },
    onError: (e: any) => alert(errorMessage(e, 'Ruční zarovnání selhalo')),
  })

  const { data: inspections = [], refetch: refetchInspections } = useQuery({
    queryKey: ['inspections', structureId],
    queryFn: () => inspectionsApi.list(structureId!),
    enabled: !!structureId,
    refetchInterval: (q) =>
      (q.state.data ?? []).some(i => i.align_status === 'processing') ? 3000 : false,
  })

  const alignMut = useMutation({
    mutationFn: () => inspectionsApi.align(structureId!, b!, a!),
    onSuccess: () => refetchInspections(),
    onError: (e: any) => alert(errorMessage(e, 'Zarovnání se nepodařilo spustit')),
  })

  const adjustMut = useMutation({
    mutationFn: () => inspectionsApi.adjustOverlay(structureId!, b!, adjust),
    onSuccess: async () => { setAdjust(ZERO_ADJ); await refetchInspections() },
    onError: (e: any) => alert(errorMessage(e, 'Uložení doladění selhalo')),
  })

  const { data: cmp, isLoading } = useQuery({
    queryKey: ['compare', structureId, a, b],
    queryFn: () => api.get<CompareResult>(
      `/structures/${structureId}/inspections/compare`, { params: { a, b } },
    ).then(r => r.data),
    enabled: !!structureId && !!a && !!b,
  })

  const { data: structure } = useQuery({
    queryKey: ['structures', structureId],
    queryFn: () => structuresApi.get(structureId!),
    enabled: !!structureId,
  })
  const refId = structure?.reference_inspection_id
  const refTag = (id?: string | null) => (id && id === refId ? ' · REFERENCE' : '')

  const inspA = inspections.find(i => i.id === a)
  const inspB = inspections.find(i => i.id === b)

  const modelA = inspA?.photo_set?.status === 'completed' ? inspA.photo_set.model_path : undefined
  const modelB = inspB?.photo_set?.status === 'completed' ? inspB.photo_set.model_path : undefined

  const alignStatus = inspB?.align_ref_id === a ? inspB?.align_status : undefined
  const isAligned = alignStatus === 'done' && !!inspB?.align_matrix && !!modelA && !!modelB

  // camera sync in side-by-side (only when aligned and not overlaying/picking)
  const syncOn = isAligned && !overlayMode && !manualMode
  const setDriver = useCameraSyncStore(s => s.setDriver)
  useEffect(() => {
    useCameraSyncStore.getState().setMatrixBA(cmp?.scene_matrix ?? null)
  }, [cmp?.scene_matrix])
  useEffect(() => {
    // dropping out of side-by-side (or leaving) must stop one pane driving the other
    if (!syncOn) useCameraSyncStore.getState().reset()
    return () => useCameraSyncStore.getState().reset()
  }, [syncOn])

  // reset the manual fine-tune whenever we leave the overlay
  useEffect(() => { if (!overlayMode) setAdjust(ZERO_ADJ) }, [overlayMode])   // eslint-disable-line react-hooks/exhaustive-deps

  const rows = useMemo(() => {
    if (!cmp) return []
    return [
      ...cmp.pairs.map(p => ({ kind: p.status, old: p.old as Annotation | null, neu: p.new as Annotation | null })),
      ...cmp.new.map(n => ({ kind: 'new' as const, old: null, neu: n })),
      ...cmp.resolved.map(r => ({ kind: 'resolved' as const, old: r, neu: null })),
      ...(cmp.uncovered ?? []).map(u => ({ kind: 'uncovered' as const, old: u, neu: null })),
    ]
  }, [cmp])

  if (!a || !b) return <div className="p-8 text-gray-400">Chybí parametry porovnání.</div>

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-4 py-2 flex items-center gap-3 shrink-0">
        <button onClick={() => navigate(`/structures/${structureId}`)} className="btn-ghost p-1.5">
          <ArrowLeft size={16} />
        </button>
        <GitCompareArrows size={16} className="text-brand-600" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-gray-900 truncate">Porovnání inspekcí</p>
          <p className="text-xs text-gray-400 truncate">
            {cmp ? `${cmp.inspection_a.name} (${new Date(cmp.inspection_a.date).toLocaleDateString('cs-CZ')})
              → ${cmp.inspection_b.name} (${new Date(cmp.inspection_b.date).toLocaleDateString('cs-CZ')})` : '…'}
          </p>
        </div>
        {cmp && (
          <div className="flex gap-1.5 flex-wrap">
            <SummaryChip icon={<TrendingUp size={11} />} label="Zhoršené" value={cmp.summary.worse} cls="bg-red-100 text-red-700" />
            <SummaryChip icon={<TrendingDown size={11} />} label="Zlepšené" value={cmp.summary.improved} cls="bg-green-100 text-green-700" />
            <SummaryChip icon={<Minus size={11} />} label="Beze změny" value={cmp.summary.unchanged} cls="bg-gray-100 text-gray-600" />
            <SummaryChip icon={<Plus size={11} />} label="Nové" value={cmp.summary.new} cls="bg-amber-100 text-amber-700" />
            <SummaryChip icon={<CheckCircle2 size={11} />} label="Opravené?" value={cmp.summary.resolved} cls="bg-sky-100 text-sky-700" />
            {!!cmp.summary.uncovered && (
              <SummaryChip icon={<EyeOff size={11} />} label="Mimo pokrytí" value={cmp.summary.uncovered} cls="bg-slate-100 text-slate-500" />
            )}
          </div>
        )}
        {a && b && (
          <button
            onClick={async () => {
              setReportBusy(true)
              try { await reportsApi.downloadComparison({ inspection_a: a, inspection_b: b }) }
              finally { setReportBusy(false) }
            }}
            disabled={reportBusy}
            className="btn-primary text-xs shrink-0"
          >
            <FileText size={14} /> {reportBusy ? '…' : 'PDF porovnání'}
          </button>
        )}
      </header>

      {/* Registration bar */}
      {manualMode ? (
        <div className="bg-brand-50 border-b border-brand-200 px-4 py-1.5 flex items-center gap-3 text-xs shrink-0">
          <Crosshair size={13} className="text-brand-600 shrink-0" />
          <span className="font-semibold text-brand-800">Ruční zarovnání</span>
          <span className="text-gray-600 flex-1">
            Přibliž si stejné místo a klikni na{' '}
            <b className={nextSide === 'a' ? 'text-brand-700' : 'text-amber-600'}>{nextSide === 'a' ? 'levém (A)' : 'pravém (B)'}</b>{' '}
            modelu · dvojic: {pairCount} · {pairCount < 3 ? 'min. 3, ' : ''}ideálně 4+ v <b>různé hloubce</b> (ne vše v jedné rovině)
          </span>
          <button onClick={undoLast} disabled={!aPts.length && !bPts.length}
            className="btn-ghost text-xs py-1 text-gray-500">Zpět</button>
          <button onClick={resetManual} disabled={!aPts.length && !bPts.length}
            className="btn-ghost text-xs py-1 text-gray-500">Vymazat</button>
          <button onClick={exitManual} className="btn-secondary text-xs py-1">Zrušit</button>
          <button
            onClick={() => manualMut.mutate()}
            disabled={pairCount < 3 || manualMut.isPending}
            className="btn-primary text-xs py-1"
          >
            <Crosshair size={12} /> {manualMut.isPending ? 'Počítám…' : `Zarovnat (${pairCount})`}
          </button>
        </div>
      ) : (
      <div className="bg-slate-50 border-b border-gray-200 px-4 py-1.5 flex items-center gap-3 text-xs shrink-0">
        <Crosshair size={13} className="text-slate-400 shrink-0" />
        {isAligned ? (
          <>
            <span className="badge bg-green-100 text-green-700">Zarovnáno</span>
            <span className="text-gray-500">
              překryv {Math.round((inspB!.align_fitness ?? 0) * 100)} % · odchylka {(inspB!.align_rmse ?? 0).toFixed(3)} m
            </span>
            <button
              onClick={() => setOverlayMode(o => !o)}
              className={overlayMode ? 'btn-primary text-xs py-1' : 'btn-secondary text-xs py-1'}
            >
              <Layers size={12} /> {overlayMode ? 'Vedle sebe' : 'Překryv v jednom pohledu'}
            </button>
            {overlayMode && (
              <label className="flex items-center gap-1.5 text-gray-500">
                Průhlednost B:
                <input
                  type="range" min={0.1} max={1} step={0.05} value={overlayOpacity}
                  onChange={e => setOverlayOpacity(parseFloat(e.target.value))}
                  className="w-28"
                />
              </label>
            )}
            {overlayMode && (
              <label className="flex items-center gap-1.5 text-gray-500">
                Průhlednost A:
                <input
                  type="range" min={0.1} max={1} step={0.05} value={baseOpacityA}
                  onChange={e => setBaseOpacityA(parseFloat(e.target.value))}
                  className="w-28"
                />
              </label>
            )}
            {canEdit && (
              <button
                onClick={() => alignMut.mutate()}
                disabled={alignMut.isPending}
                className="btn-ghost text-xs py-1 text-gray-500"
              >
                Přepočítat
              </button>
            )}
          </>
        ) : alignStatus === 'processing' ? (
          <span className="text-brand-700 flex items-center gap-2">
            <span className="animate-spin rounded-full h-3 w-3 border-2 border-brand-500 border-t-transparent" />
            Zarovnávám model B na A…
          </span>
        ) : (
          <>
            <span className="text-gray-500 flex-1">
              Modely nejsou prostorově zarovnané{canEdit ? ' — inspekce B (i částečná) se dá namapovat na model A.' : '.'}
              {alignStatus === 'failed' && inspB?.align_error && (
                <span className="text-red-600 ml-2 inline-flex items-center gap-1">
                  <AlertTriangle size={12} /> {inspB.align_error}
                </span>
              )}
            </span>
            {canEdit && (
              <button
                onClick={() => alignMut.mutate()}
                disabled={alignMut.isPending || !modelA || !modelB}
                className="btn-primary text-xs py-1"
                title="Zaregistruje model B do souřadnic modelu A (vyžaduje zkalibrované měřítko obou inspekcí)"
              >
                <Crosshair size={12} /> {alignMut.isPending ? 'Spouštím…' : 'Zarovnat B na A'}
              </button>
            )}
          </>
        )}
        {canEdit && modelA && modelB && (
          <button
            onClick={() => { setOverlayMode(false); resetManual(); setManualMode(true) }}
            className="btn-ghost text-xs py-1 text-gray-500 shrink-0"
            title="Zarovnat ručně klikáním odpovídajících bodů — spolehlivé i pro malý detail na velkém modelu"
          >
            <MousePointerClick size={12} /> Ručně
          </button>
        )}
      </div>
      )}

      {/* Viewers */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {overlayMode && isAligned && !manualMode ? (
          <div className="flex-1 relative min-w-0 overflow-hidden">
            <ViewerPane
              label={`Překryv · A ${inspA?.name ?? ''}${refTag(a)} + B ${inspB?.name ?? ''}${refTag(b)} (zarovnáno)`}
              inspectionId={a}
              modelPath={modelA}
              baseOpacity={baseOpacityA}
              overlay={{
                modelPath: modelB!,
                refModelPath: modelA!,
                matrix: inspB!.align_matrix!,
                opacity: overlayOpacity,
                canonical: !!inspB?.canonical,
                adjust,
              }}
            />
            {canEdit && !inspB?.canonical && (
              <AdjustPanel
                adjust={adjust}
                onChange={setAdjust}
                onReset={() => setAdjust(ZERO_ADJ)}
                onSave={() => adjustMut.mutate()}
                saving={adjustMut.isPending}
                dirty={JSON.stringify(adjust) !== JSON.stringify(ZERO_ADJ)}
              />
            )}
          </div>
        ) : (
          <>
            <div onPointerEnter={() => syncOn && setDriver('a')}
              className={`flex-1 relative min-w-0 overflow-hidden border-r border-slate-700 ${manualMode && nextSide === 'a' ? 'ring-2 ring-inset ring-brand-400' : ''}`}>
              <ViewerPane label={`A · ${inspA?.name ?? ''}${refTag(a)} · ${inspA ? new Date(inspA.inspection_date).toLocaleDateString('cs-CZ') : ''}`}
                inspectionId={a} modelPath={modelA}
                onPick={manualMode ? (p) => setAPts(v => [...v, p]) : undefined}
                pickMarkers={manualMode ? aPts : undefined}
                cameraSync={syncOn ? 'a' : undefined}
                canonical={!!inspA?.canonical}
                pickColor="#0ea5e9" />
            </div>
            <div onPointerEnter={() => syncOn && setDriver('b')}
              className={`flex-1 relative min-w-0 overflow-hidden ${manualMode && nextSide === 'b' ? 'ring-2 ring-inset ring-brand-400' : ''}`}>
              <ViewerPane label={`B · ${inspB?.name ?? ''}${refTag(b)} · ${inspB ? new Date(inspB.inspection_date).toLocaleDateString('cs-CZ') : ''}`}
                inspectionId={b} modelPath={modelB}
                onPick={manualMode ? (p) => setBPts(v => [...v, p]) : undefined}
                pickMarkers={manualMode ? bPts : undefined}
                cameraSync={syncOn ? 'b' : undefined}
                canonical={!!inspB?.canonical}
                pickColor="#f59e0b" />
            </div>
          </>
        )}
      </div>

      {/* Diff table */}
      <div className="h-56 shrink-0 bg-white border-t border-gray-200 overflow-y-auto">
        {isLoading ? (
          <p className="text-xs text-gray-400 text-center py-6">Porovnávám…</p>
        ) : rows.length === 0 ? (
          <p className="text-xs text-gray-400 text-center py-6">Žádné poruchy k porovnání.</p>
        ) : (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-gray-50 text-gray-500">
              <tr>
                <th className="text-left px-3 py-1.5 font-medium">Stav</th>
                <th className="text-left px-3 py-1.5 font-medium">Porucha</th>
                <th className="text-left px-3 py-1.5 font-medium">A ({cmp && new Date(cmp.inspection_a.date).toLocaleDateString('cs-CZ')})</th>
                <th className="text-left px-3 py-1.5 font-medium">B ({cmp && new Date(cmp.inspection_b.date).toLocaleDateString('cs-CZ')})</th>
                <th className="text-left px-3 py-1.5 font-medium">Poznámka</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const ann = r.neu ?? r.old!
                const selected = selectedAnnotation?.id === r.neu?.id || selectedAnnotation?.id === r.old?.id
                return (
                  <tr
                    key={i}
                    onClick={() => setSelectedAnnotation(selected ? null : (r.neu ?? r.old))}
                    className={`border-t border-gray-100 cursor-pointer ${selected ? 'bg-brand-50' : 'hover:bg-gray-50'}`}
                  >
                    <td className="px-3 py-1.5"><KindBadge kind={r.kind} /></td>
                    <td className="px-3 py-1.5 font-medium text-gray-800">
                      {DEFECT_LABELS[ann.defect_type] ?? ann.defect_type}
                      {ann.description ? <span className="text-gray-400 font-normal"> — {ann.description.slice(0, 60)}</span> : null}
                    </td>
                    <td className="px-3 py-1.5">{r.old ? <SevBadge s={r.old.severity} /> : <span className="text-gray-300">—</span>}</td>
                    <td className="px-3 py-1.5">{r.neu ? <SevBadge s={r.neu.severity} /> : <span className="text-gray-300">—</span>}</td>
                    <td className="px-3 py-1.5 text-gray-400">
                      {r.kind === 'new' && 'Nová porucha — v A nenalezena'}
                      {r.kind === 'resolved' && 'V B nenalezena — ověřte, zda opravena'}
                      {r.kind === 'uncovered' && 'Mimo záběr novější inspekce — nezkontrolováno'}
                      {r.kind === 'worse' && 'Závažnost vzrostla'}
                      {r.kind === 'improved' && 'Závažnost klesla'}
                      {r.kind === 'unchanged' && ''}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        {cmp && (
          <p className="text-[10px] text-gray-400 px-3 py-2">
            {cmp.aligned
              ? 'Poruchy jsou párované podle skutečné vzdálenosti ve společných souřadnicích (modely jsou zarovnané, tolerance 0,6 m). Novější inspekce může pokrývat jen část objektu — poruchy z A mimo její záběr jsou označené „mimo pokrytí“, ne „opravené“.'
              : 'Párování poruch je heuristické — modely nejsou zarovnané, takže se porovnává jen typ a přibližná poloha v rámci objektu. Zarovnáním získáte přesné párování.'}
          </p>
        )}
      </div>
    </div>
  )
}

function ViewerPane({ label, inspectionId, modelPath, overlay, onPick, pickMarkers, pickColor, baseOpacity, cameraSync, canonical }: {
  label: string
  inspectionId: string
  modelPath?: string | null
  overlay?: { modelPath: string; refModelPath: string; matrix: number[]; opacity: number; color?: string; canonical?: boolean; adjust?: { tx: number; ty: number; tz: number; rx: number; ry: number; rz: number; sc: number } }
  onPick?: (p: { x: number; y: number; z: number }) => void
  pickMarkers?: Array<{ x: number; y: number; z: number }>
  pickColor?: string
  baseOpacity?: number
  cameraSync?: 'a' | 'b'
  canonical?: boolean
}) {
  return (
    <>
      <Viewer3D inspectionId={inspectionId} modelPath={modelPath ?? undefined} overlay={overlay}
        onPick={onPick} pickMarkers={pickMarkers} pickColor={pickColor} baseOpacity={baseOpacity}
        cameraSync={cameraSync} canonical={canonical} />
      <div className="absolute top-2 left-2 bg-black/60 text-white text-xs px-3 py-1 rounded-full pointer-events-none">
        {label}
      </div>
      {!modelPath && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <p className="text-slate-400 text-sm">Model není k dispozici</p>
        </div>
      )}
    </>
  )
}

interface Adjust { tx: number; ty: number; tz: number; rx: number; ry: number; rz: number; sc: number }

// A number input where ↑/↓ nudge by 0.1, Alt+↑/↓ by 1, Shift+↑/↓ by 10.
function NumberField({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const round3 = (v: number) => Math.round(v * 1000) / 1000
  return (
    <input
      type="number"
      value={round3(value)}
      step={0.1}
      onChange={e => onChange(e.target.value === '' ? 0 : parseFloat(e.target.value))}
      onKeyDown={e => {
        if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
        e.preventDefault()
        const step = e.shiftKey ? 10 : e.altKey ? 1 : 0.1
        onChange(round3(value + (e.key === 'ArrowUp' ? step : -step)))
      }}
      className="w-14 bg-slate-800 text-white text-[11px] rounded px-1 py-0.5 tabular-nums
        [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
    />
  )
}

// display units: translation ×1000 (fine units), rotation in degrees
const T_SCALE = 1000
const R_SCALE = 180 / Math.PI

// module-level so it isn't recreated each render (which would drop input focus)
function AdjustRow({ label, keys, scale, adjust, onChange }: {
  label: string; keys: [keyof Adjust, keyof Adjust, keyof Adjust]; scale: number
  adjust: Adjust; onChange: (a: Adjust) => void
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="w-16 text-[10px] text-slate-300 shrink-0">{label}</span>
      {(['X', 'Y', 'Z'] as const).map((axis, i) => (
        <label key={axis} className="flex items-center gap-0.5">
          <span className="text-[10px] text-slate-500">{axis}</span>
          <NumberField
            value={adjust[keys[i]] * scale}
            onChange={v => onChange({ ...adjust, [keys[i]]: v / scale })}
          />
        </label>
      ))}
    </div>
  )
}

function AdjustPanel({ adjust, onChange, onReset, onSave, saving, dirty }: {
  adjust: Adjust
  onChange: (a: Adjust) => void
  onReset: () => void
  onSave: () => void
  saving: boolean
  dirty: boolean
}) {
  const [open, setOpen] = useState(true)
  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        title="Doladit překryv"
        className="absolute bottom-3 left-3 bg-black/75 text-white rounded-lg px-2.5 py-1.5 backdrop-blur flex items-center gap-1.5 hover:bg-black/85">
        <SlidersHorizontal size={13} className="text-amber-300" />
        <span className="text-[11px] font-medium">Doladit{dirty ? ' •' : ''}</span>
      </button>
    )
  }
  return (
    <div className="absolute bottom-3 left-3 bg-black/75 text-white rounded-lg p-3 pt-2 space-y-2 backdrop-blur">
      <button onClick={() => setOpen(false)}
        className="flex items-center justify-between w-full text-amber-300 hover:text-amber-200">
        <span className="text-[11px] font-semibold flex items-center gap-1.5">
          <SlidersHorizontal size={12} /> Doladit překryv (model B)
        </span>
        <ChevronDown size={14} />
      </button>
      <AdjustRow label="Posun" keys={['tx', 'ty', 'tz']} scale={T_SCALE} adjust={adjust} onChange={onChange} />
      <AdjustRow label="Otočení °" keys={['rx', 'ry', 'rz']} scale={R_SCALE} adjust={adjust} onChange={onChange} />
      <div className="flex items-center gap-1.5">
        <span className="w-16 text-[10px] text-slate-300 shrink-0">Měřítko %</span>
        <NumberField value={adjust.sc * 100} onChange={v => onChange({ ...adjust, sc: (v || 100) / 100 })} />
      </div>
      <div className="text-[9px] text-slate-400 leading-tight">
        šipky ↑↓ · Alt = po&nbsp;1 · Shift = po&nbsp;10
      </div>
      <div className="flex items-center gap-2 pt-0.5">
        <button onClick={onReset} disabled={!dirty}
          className="text-[11px] px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-40">Vynulovat</button>
        <button onClick={onSave} disabled={!dirty || saving}
          className="text-[11px] px-2 py-1 rounded bg-amber-600 hover:bg-amber-500 disabled:opacity-40 font-medium">
          {saving ? 'Ukládám…' : 'Uložit'}
        </button>
      </div>
    </div>
  )
}

function SummaryChip({ icon, label, value, cls }: { icon: React.ReactNode; label: string; value: number; cls: string }) {
  return (
    <span className={`badge ${cls} flex items-center gap-1 text-[10px]`}>
      {icon} {label}: {value}
    </span>
  )
}

function KindBadge({ kind }: { kind: string }) {
  const map: Record<string, [string, string]> = {
    worse: ['Zhoršeno', 'bg-red-100 text-red-700'],
    improved: ['Zlepšeno', 'bg-green-100 text-green-700'],
    unchanged: ['Beze změny', 'bg-gray-100 text-gray-600'],
    new: ['Nová', 'bg-amber-100 text-amber-700'],
    resolved: ['Opraveno?', 'bg-sky-100 text-sky-700'],
    uncovered: ['Mimo pokrytí', 'bg-slate-100 text-slate-500'],
  }
  const [label, cls] = map[kind] ?? [kind, 'bg-gray-100 text-gray-600']
  return <span className={`badge text-[10px] ${cls}`}>{label}</span>
}

function SevBadge({ s }: { s: Severity }) {
  return <span className={`badge text-[10px] ${SEVERITY_BG[s]}`}>{SEVERITY_LABELS[s]}</span>
}
