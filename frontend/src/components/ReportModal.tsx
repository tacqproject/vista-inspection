import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { X, FileText, Image as ImageIcon } from 'lucide-react'
import { annotationsApi, reportsApi } from '../api/inspections'
import { SEVERITY_LABELS, type Severity } from '../types'
import { useViewerStore } from '../store/viewerStore'

/**
 * Report options dialog: protocol/branding header, section toggles, severity
 * filter, and an optional 3D snapshot with numbered defect markers captured
 * live from the viewer canvas and embedded in the PDF.
 */
export default function ReportModal({ inspectionId, onClose }: {
  inspectionId: string
  onClose: () => void
}) {
  const snapshotFn = useViewerStore(s => s.snapshotFn)
  const defectSnapshotsFn = useViewerStore(s => s.defectSnapshotsFn)
  const logoRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)

  const { data: annotations = [] } = useQuery({
    queryKey: ['annotations', inspectionId],
    queryFn: () => annotationsApi.list(inspectionId),
  })

  const [form, setForm] = useState({
    protocol_number: '',
    company_name: '',
    prepared_by: '',
    notes: '',
    logo: '' as string,
    include_annotations: true,
    include_measurements: true,
    include_map: true,
    include_defect_views: true,
  })
  const [severities, setSeverities] = useState<Set<Severity>>(new Set())

  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) =>
    setForm(f => ({ ...f, [k]: v }))

  const onLogo = (file?: File) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => set('logo', String(reader.result))
    reader.readAsDataURL(file)
  }

  const toggleSev = (s: Severity) =>
    setSeverities(prev => {
      const next = new Set(prev)
      next.has(s) ? next.delete(s) : next.add(s)
      return next
    })

  const generate = async () => {
    setBusy(true)
    try {
      const scene_snapshot = form.include_map && snapshotFn ? await snapshotFn(annotations) : null

      // one face-on 3D close-up per defect, keyed by id so it survives the
      // backend's severity filtering (which changes the annotation order)
      let defect_snapshots: Record<string, string> | undefined
      if (form.include_defect_views && defectSnapshotsFn && annotations.length) {
        const views = await defectSnapshotsFn(annotations)
        defect_snapshots = {}
        annotations.forEach((a, i) => { if (views[i]) defect_snapshots![a.id] = views[i]! })
      }

      await reportsApi.downloadInspection(inspectionId, {
        protocol_number: form.protocol_number || undefined,
        company_name: form.company_name || undefined,
        prepared_by: form.prepared_by || undefined,
        logo: form.logo || undefined,
        notes: form.notes || undefined,
        include_annotations: form.include_annotations,
        include_measurements: form.include_measurements,
        severity_filter: severities.size ? Array.from(severities) : undefined,
        scene_snapshot: scene_snapshot || undefined,
        defect_snapshots,
      })
      onClose()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="card w-full max-w-lg mx-4 p-5 max-h-[92vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-gray-900 flex items-center gap-2">
            <FileText size={16} /> Generovat protokol
          </h3>
          <button onClick={onClose} className="btn-ghost p-1"><X size={16} /></button>
        </div>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">Číslo protokolu</label>
              <input className="input" value={form.protocol_number}
                onChange={e => set('protocol_number', e.target.value)} placeholder="2026/0042" />
            </div>
            <div>
              <label className="label">Zpracoval</label>
              <input className="input" value={form.prepared_by}
                onChange={e => set('prepared_by', e.target.value)} placeholder="Jméno" />
            </div>
          </div>

          <div>
            <label className="label">Firma / zpracovatel</label>
            <input className="input" value={form.company_name}
              onChange={e => set('company_name', e.target.value)} placeholder="Např. ACME Inspekce s.r.o." />
          </div>

          <div>
            <label className="label">Logo firmy</label>
            <div className="flex items-center gap-2">
              <button onClick={() => logoRef.current?.click()} className="btn-secondary text-xs">
                <ImageIcon size={13} /> {form.logo ? 'Změnit logo' : 'Nahrát logo'}
              </button>
              {form.logo && (
                <>
                  <img src={form.logo} alt="logo" className="h-8 rounded border border-gray-200" />
                  <button onClick={() => set('logo', '')} className="btn-ghost text-xs text-red-500">Odebrat</button>
                </>
              )}
              <input ref={logoRef} type="file" accept="image/*" className="hidden"
                onChange={e => { onLogo(e.target.files?.[0]); e.target.value = '' }} />
            </div>
          </div>

          <div>
            <label className="label">Závěrečné poznámky</label>
            <textarea className="input h-16 resize-none" value={form.notes}
              onChange={e => set('notes', e.target.value)} placeholder="Volitelný text na konec protokolu…" />
          </div>

          <div>
            <label className="label">Filtr závažnosti (prázdné = vše)</label>
            <div className="grid grid-cols-4 gap-2">
              {(['critical', 'high', 'medium', 'low'] as Severity[]).map(s => (
                <button key={s} type="button" onClick={() => toggleSev(s)}
                  className={`text-xs py-1.5 rounded-lg border-2 font-medium transition-all
                    ${severities.has(s) ? 'border-brand-400 bg-brand-50 text-brand-800' : 'border-gray-200 bg-white text-gray-500 hover:border-gray-300'}`}>
                  {SEVERITY_LABELS[s]}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5 pt-1">
            {([
              ['include_annotations', 'Zahrnout tabulku poruch + fotodokumentaci'],
              ['include_measurements', 'Zahrnout měření'],
              ['include_map', '3D mapa poruch (přehledový snímek s očíslovanými značkami)'],
              ['include_defect_views', '3D pohled na KAŽDOU porucha (čelní detail z modelu)'],
            ] as const).map(([k, lbl]) => (
              <label key={k} className="flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" checked={form[k]} onChange={e => set(k, e.target.checked)} />
                {lbl}
              </label>
            ))}
            {form.include_map && !snapshotFn && (
              <p className="text-[11px] text-amber-600 pl-6">
                Model není načtený ve 3D — mapa poruch se do protokolu nepřidá.
              </p>
            )}
          </div>
        </div>

        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary">Zrušit</button>
          <button onClick={generate} disabled={busy} className="btn-primary disabled:opacity-50">
            {busy ? 'Generuji…' : 'Stáhnout PDF'}
          </button>
        </div>
      </div>
    </div>
  )
}
