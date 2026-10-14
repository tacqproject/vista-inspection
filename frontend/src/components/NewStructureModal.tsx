import { useState } from 'react'
import { useMutation, useQueryClient, useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { structuresApi } from '../api/structures'
import { configApi } from '../api/config'
import MaterialsEditor, { type MaterialRow } from './MaterialsEditor'
import { errorMessage } from '../api/client'

interface Props { onClose: () => void }

export default function NewStructureModal({ onClose }: Props) {
  const qc = useQueryClient()

  const { data: structureTypes = [] } = useQuery({
    queryKey: ['config', 'structure-types'],
    queryFn: configApi.listStructureTypes,
  })

  const [form, setForm] = useState({
    name: '',
    type: '',
    description: '',
    location_name: '',
    year_built: '',
  })
  const [materials, setMaterials] = useState<MaterialRow[]>([])

  // Set default type once loaded
  const effectiveType = form.type || structureTypes[0]?.code || ''

  const mut = useMutation({
    mutationFn: () => structuresApi.create({
      name: form.name,
      type: effectiveType,
      description: form.description || undefined,
      location_name: form.location_name || undefined,
      year_built: form.year_built ? parseInt(form.year_built) : undefined,
      materials: materials.filter(m => m.percentage > 0).map((m, i) => ({
        ...m,
        sort_order: i,
      })),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['structures'] })
      onClose()
    },
  })

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="card w-full max-w-lg mx-4 p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold">Nová konstrukce</h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="label">Název *</label>
            <input
              className="input"
              value={form.name}
              onChange={e => set('name', e.target.value)}
              placeholder="např. Most přes Vltavu"
              autoFocus
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Typ *</label>
              <select
                className="input"
                value={effectiveType}
                onChange={e => set('type', e.target.value)}
              >
                {structureTypes.map(t => (
                  <option key={t.code} value={t.code}>{t.name}</option>
                ))}
                {structureTypes.length === 0 && (
                  <option value="">Načítám…</option>
                )}
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

          {/* Materials with percentages */}
          <MaterialsEditor value={materials} onChange={setMaterials} />

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
            disabled={!form.name || !effectiveType || mut.isPending}
            className="btn-primary disabled:opacity-50"
          >
            {mut.isPending ? 'Ukládám…' : 'Vytvořit'}
          </button>
        </div>
      </div>
    </div>
  )
}
