import { useState } from 'react'
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { structuresApi } from '../api/structures'
import { configApi } from '../api/config'
import type { Structure } from '../types'
import { errorMessage } from '../api/client'

interface Props {
  structure: Structure
  onClose: () => void
}

export default function EditStructureModal({ structure, onClose }: Props) {
  const qc = useQueryClient()

  const { data: structureTypes = [] } = useQuery({
    queryKey: ['config', 'structure-types'],
    queryFn: configApi.listStructureTypes,
  })

  const [form, setForm] = useState({
    name: structure.name,
    type: structure.type,
    description: structure.description ?? '',
    location_name: structure.location_name ?? '',
    year_built: structure.year_built?.toString() ?? '',
    latitude: structure.latitude?.toString() ?? '',
    longitude: structure.longitude?.toString() ?? '',
    evidence_number: structure.evidence_number ?? '',
    district: structure.district ?? '',
    road_number: structure.road_number ?? '',
    chainage_km: structure.chainage_km ?? '',
    chainage_direction: structure.chainage_direction ?? '',
    load_vn: structure.load_vn ?? '',
    load_vr: structure.load_vr ?? '',
    load_ve: structure.load_ve ?? '',
    max_axle_load: structure.max_axle_load ?? '',
    administrator: structure.administrator ?? '',
  })

  const num = (s: string) => {
    const v = parseFloat(s.replace(',', '.'))
    return Number.isFinite(v) ? v : undefined
  }

  const mut = useMutation({
    mutationFn: () => structuresApi.update(structure.id, {
      name: form.name,
      type: form.type,
      description: form.description || undefined,
      location_name: form.location_name || undefined,
      year_built: form.year_built ? parseInt(form.year_built) : undefined,
      latitude: num(form.latitude),
      longitude: num(form.longitude),
      // BMS register — sent as-is ('' clears a value)
      evidence_number: form.evidence_number,
      district: form.district,
      road_number: form.road_number,
      chainage_km: form.chainage_km,
      chainage_direction: form.chainage_direction,
      load_vn: form.load_vn,
      load_vr: form.load_vr,
      load_ve: form.load_ve,
      max_axle_load: form.max_axle_load,
      administrator: form.administrator,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['structures'] })
      qc.invalidateQueries({ queryKey: ['structures', structure.id] })
      onClose()
    },
  })

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="card w-full max-w-lg mx-4 p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold">Upravit konstrukci</h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="label">Název *</label>
            <input
              className="input"
              value={form.name}
              onChange={e => set('name', e.target.value)}
              autoFocus
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Typ *</label>
              <select className="input" value={form.type} onChange={e => set('type', e.target.value)}>
                {structureTypes.map(t => (
                  <option key={t.code} value={t.code}>{t.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label">Rok výstavby</label>
              <input
                className="input"
                type="number"
                value={form.year_built}
                onChange={e => set('year_built', e.target.value)}
                placeholder="2005"
              />
            </div>
          </div>

          <div>
            <label className="label">Lokalita</label>
            <input
              className="input"
              value={form.location_name}
              onChange={e => set('location_name', e.target.value)}
              placeholder="Praha 1, Na Příkopě"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Zeměpisná šířka</label>
              <input
                className="input"
                inputMode="decimal"
                value={form.latitude}
                onChange={e => set('latitude', e.target.value)}
                placeholder="50.0755"
              />
            </div>
            <div>
              <label className="label">Zeměpisná délka</label>
              <input
                className="input"
                inputMode="decimal"
                value={form.longitude}
                onChange={e => set('longitude', e.target.value)}
                placeholder="14.4378"
              />
            </div>
          </div>
          <p className="text-[10px] text-gray-400 -mt-2">
            Souřadnice umístí 3D model na satelitní mapový podklad (pravý klik v Mapách Google → souřadnice).
          </p>

          <fieldset className="border border-gray-200 rounded-lg p-3 space-y-3">
            <legend className="text-xs font-semibold text-gray-600 px-1">Mostní evidence (BMS) — do protokolu prohlídky</legend>
            <div className="grid grid-cols-2 gap-3">
              {([
                ['evidence_number', 'Ev. č. mostu', '16-045a'],
                ['district', 'Okres', 'Jičín'],
                ['road_number', 'Číslo komunikace', '16'],
                ['chainage_km', 'Staničení [km]', '123.305'],
                ['chainage_direction', 'Staničení ve směru', 'Trutnov'],
                ['administrator', 'Správce mostu', 'ŘSD, Správa Hradec Králové'],
              ] as const).map(([k, label, ph]) => (
                <div key={k}>
                  <label className="label">{label}</label>
                  <input className="input" value={form[k]} onChange={e => set(k, e.target.value)} placeholder={ph} />
                </div>
              ))}
            </div>
            <div className="grid grid-cols-4 gap-3">
              {([
                ['load_vn', 'Vn [t]'], ['load_vr', 'Vr [t]'], ['load_ve', 'Ve [t]'], ['max_axle_load', 'Max. nápr. tlak'],
              ] as const).map(([k, label]) => (
                <div key={k}>
                  <label className="label">{label}</label>
                  <input className="input" value={form[k]} onChange={e => set(k, e.target.value)} />
                </div>
              ))}
            </div>
          </fieldset>

          <div>
            <label className="label">Popis</label>
            <textarea
              className="input h-20 resize-none"
              value={form.description}
              onChange={e => set('description', e.target.value)}
            />
          </div>
        </div>

        {mut.isError && (
          <p className="text-sm text-red-600 mt-3">
            {errorMessage(mut.error, 'Chyba při ukládání')}
          </p>
        )}

        <div className="flex justify-end gap-3 mt-6">
          <button onClick={onClose} className="btn-secondary">Zrušit</button>
          <button
            onClick={() => mut.mutate()}
            disabled={!form.name || !form.type || mut.isPending}
            className="btn-primary disabled:opacity-50"
          >
            {mut.isPending ? 'Ukládám…' : 'Uložit změny'}
          </button>
        </div>
      </div>
    </div>
  )
}
