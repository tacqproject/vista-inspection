import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { X, FileText, FileDown, ImagePlus, Trash2 } from 'lucide-react'
import { inspectionsApi, reportsApi, annotationsApi } from '../api/inspections'
import { errorMessage } from '../api/client'
import { useViewerStore } from '../store/viewerStore'
import type { Inspection } from '../types'

/**
 * Inspection protocol in the ŘSD BMS layout (Protokol o běžné / hlavní prohlídce
 * mostu). The protocol data are stored on the inspection; bridge-register data
 * (ev. č., komunikace, staničení…) come from the structure, defects with their
 * bridge part and measure urgency from the annotations. Output: .docx, or a .pdf
 * rendered from the same .docx.
 */

type Field = keyof Pick<Inspection,
  'inspection_kind' | 'inspector_name' | 'inspector_company' | 'protocol_note' | 'weather' |
  'access_method' | 'air_temperature' | 'structure_temperature' | 'state_substructure' |
  'state_superstructure' | 'usability' | 'care_assessment' | 'negotiation_date' |
  'reference_number' | 'negotiation_note' | 'authority_opinion'>

const STATES = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII']

export default function BmsProtocolModal({ structureId, inspection, onClose }: {
  structureId: string
  inspection: Inspection
  onClose: () => void
}) {
  const qc = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const defectSnapshotsFn = useViewerStore(s => s.defectSnapshotsFn)
  const [busy, setBusy] = useState<'' | 'docx' | 'pdf'>('')
  const [err, setErr] = useState<string | null>(null)
  const [withViews, setWithViews] = useState(false)

  const init = (k: Field) => (inspection[k] as string | undefined) ?? ''
  const [f, setF] = useState<Record<Field, string>>({
    inspection_kind: inspection.inspection_kind ?? 'bezna',
    inspector_name: init('inspector_name'),
    inspector_company: init('inspector_company'),
    protocol_note: init('protocol_note'),
    weather: init('weather'),
    access_method: init('access_method'),
    air_temperature: init('air_temperature'),
    structure_temperature: init('structure_temperature'),
    state_substructure: init('state_substructure'),
    state_superstructure: init('state_superstructure'),
    usability: init('usability'),
    care_assessment: init('care_assessment'),
    negotiation_date: init('negotiation_date'),
    reference_number: init('reference_number'),
    negotiation_note: init('negotiation_note'),
    authority_opinion: init('authority_opinion'),
  })
  const set = (k: Field, v: string) => setF(p => ({ ...p, [k]: v }))

  const { data: catalogue } = useQuery({ queryKey: ['bms-catalogue'], queryFn: reportsApi.bmsCatalogue, staleTime: Infinity })
  const { data: annotations = [] } = useQuery({
    queryKey: ['annotations', inspection.id],
    queryFn: () => annotationsApi.list(inspection.id),
  })
  const { data: appendix } = useQuery({
    queryKey: ['appendix-photos', inspection.id],
    queryFn: () => reportsApi.appendixPhotos(inspection.id),
  })
  const noPart = annotations.filter(a => !a.bridge_part && a.status !== 'dismissed').length

  const save = useMutation({
    mutationFn: () => inspectionsApi.update(structureId, inspection.id, f as Partial<Inspection>),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['inspection', structureId, inspection.id] }),
  })
  const upload = useMutation({
    mutationFn: (files: File[]) => reportsApi.uploadAppendixPhotos(inspection.id, files),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['appendix-photos', inspection.id] }),
  })
  const remove = useMutation({
    mutationFn: (name: string) => reportsApi.deleteAppendixPhoto(inspection.id, name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['appendix-photos', inspection.id] }),
  })

  const download = async (format: 'docx' | 'pdf') => {
    setBusy(format); setErr(null)
    try {
      await save.mutateAsync()                 // the protocol prints the saved data
      let defect_snapshots: Record<string, string> | undefined
      if (withViews && defectSnapshotsFn && annotations.length) {
        const views = await defectSnapshotsFn(annotations)
        defect_snapshots = {}
        annotations.forEach((a, i) => { if (views[i]) defect_snapshots![a.id] = views[i]! })
      }
      await reportsApi.downloadBms(inspection.id, { format, defect_snapshots })
    } catch (e) {
      setErr(errorMessage(e, 'Vytvoření protokolu selhalo'))
    } finally {
      setBusy('')
    }
  }

  const input = (k: Field, label: string, ph = '', cls = '') => (
    <div className={cls}>
      <label className="label">{label}</label>
      <input className="input" value={f[k]} placeholder={ph} onChange={e => set(k, e.target.value)} />
    </div>
  )
  const area = (k: Field, label: string, ph = '', h = 'h-16') => (
    <div>
      <label className="label">{label}</label>
      <textarea className={`input ${h} resize-y`} value={f[k]} placeholder={ph} onChange={e => set(k, e.target.value)} />
    </div>
  )
  const state = (k: Field, label: string) => (
    <div>
      <label className="label">{label}</label>
      <select className="input" value={f[k]} onChange={e => set(k, e.target.value)}>
        <option value="">—</option>
        {STATES.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
    </div>
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="card w-full max-w-3xl mx-4 p-6 max-h-[92vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <FileText size={18} /> Protokol o prohlídce mostu (BMS)
          </h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>

        <p className="text-xs text-gray-500 mb-4">
          Údaje mostu (ev. č., okres, komunikace, staničení, zatížitelnost) se berou z karty konstrukce;
          závady a opatření ze zakreslených poruch (část mostu + naléhavost ve formuláři poruchy).
          {noPart > 0 && (
            <span className="block mt-1 text-amber-600">
              {noPart} {noPart === 1 ? 'porucha nemá' : 'poruch nemá'} vyplněnou část mostu — v oddílu C
              budou zařazeny jako „Ostatní“.
            </span>
          )}
        </p>

        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="label">Druh prohlídky</label>
              <select className="input" value={f.inspection_kind} onChange={e => set('inspection_kind', e.target.value)}>
                {Object.entries(catalogue?.kinds ?? { bezna: 'BĚŽNÁ PROHLÍDKA' }).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </div>
            {input('inspector_name', 'Prohlídku provedl', 'Příjmení Jméno, Ing.')}
            {input('inspector_company', 'Firma', 'SAFETYPRO')}
          </div>
          {area('protocol_note', 'Poznámka (oprávnění, zadavatel, smlouva, zkratky…)', 'Č. oprávnění: …', 'h-24')}
          <div className="grid grid-cols-4 gap-3">
            {input('weather', 'Počasí', 'Jasno')}
            {input('access_method', 'Způsob zpřístupnění', 'z vozovky, z terénu')}
            {input('air_temperature', 'Teplota vzduchu [°C]', '22.0')}
            {input('structure_temperature', 'Teplota NK [°C]')}
          </div>
          <div className="grid grid-cols-3 gap-3">
            {state('state_substructure', 'Stav – spodní stavba')}
            {state('state_superstructure', 'Stav – nosná konstrukce')}
            {input('usability', 'Použitelnost')}
          </div>
          {area('care_assessment', 'D. Hodnocení péče o most, výkonu prohlídek, údržby a oprav',
            'Údržba mostu se provádí v rozsahu možností správce.')}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">F. Datum projednání</label>
              <input type="date" className="input" value={f.negotiation_date} onChange={e => set('negotiation_date', e.target.value)} />
            </div>
            {input('reference_number', 'F. Číslo jednací')}
          </div>
          {area('negotiation_note', 'F. Poznámka k projednání', 'Závěry prohlídky projednány se správcem…')}
          {area('authority_opinion', 'H. Stanovisko nadřízeného orgánu')}

          {/* J. general appendix photos */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="label mb-0">J. Obrazové přílohy – obecné snímky</label>
              <button className="btn-secondary text-xs" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
                <ImagePlus size={13} /> {upload.isPending ? 'Nahrávám…' : 'Přidat snímky'}
              </button>
              <input ref={fileRef} type="file" accept="image/*" multiple hidden
                onChange={e => { const fs = Array.from(e.target.files ?? []); if (fs.length) upload.mutate(fs); e.target.value = '' }} />
            </div>
            <p className="text-[11px] text-gray-400 mb-2">
              Např. „01 Pohled ve směru staničení.jpg“ — číslo na začátku názvu určuje pořadí, popisek je název souboru.
              Za nimi následují fotky přiřazené k jednotlivým poruchám.
            </p>
            <div className="flex flex-wrap gap-2">
              {appendix?.photos.map(p => (
                <div key={p.filename} className="relative w-28">
                  <img src={p.url} className="w-28 h-20 object-cover rounded border border-gray-200" />
                  <div className="text-[10px] text-gray-500 truncate" title={p.filename}>{p.filename}</div>
                  <button className="absolute top-1 right-1 bg-white/90 rounded p-0.5 text-red-600"
                    title="Odebrat" onClick={() => remove.mutate(p.filename)}>
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
              {!appendix?.photos.length && <span className="text-xs text-gray-400">Zatím žádné.</span>}
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={withViews} onChange={e => setWithViews(e.target.checked)} />
            Přidat do příloh 3D pohled na každou poruchu
          </label>
        </div>

        {(err || save.isError) && (
          <p className="text-sm text-red-600 mt-3">{err ?? errorMessage(save.error, 'Uložení selhalo')}</p>
        )}

        <div className="flex flex-wrap justify-end gap-3 mt-6">
          <button onClick={() => save.mutate()} className="btn-secondary" disabled={save.isPending || !!busy}>
            {save.isPending ? 'Ukládám…' : 'Uložit údaje'}
          </button>
          <button onClick={() => download('pdf')} className="btn-secondary" disabled={!!busy}>
            <FileDown size={14} /> {busy === 'pdf' ? 'Vytvářím PDF…' : 'Stáhnout PDF'}
          </button>
          <button onClick={() => download('docx')} className="btn-primary" disabled={!!busy}>
            <FileDown size={14} /> {busy === 'docx' ? 'Vytvářím…' : 'Stáhnout DOCX'}
          </button>
        </div>
      </div>
    </div>
  )
}
