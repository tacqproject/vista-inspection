import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { X, Plus, Pencil, Trash2, Check, XCircle, GripVertical } from 'lucide-react'
import { configApi, type StructureTypeConfig, type MaterialConfig } from '../api/config'

interface Props { onClose: () => void }

type Tab = 'types' | 'materials'

export default function SettingsModal({ onClose }: Props) {
  const [tab, setTab] = useState<Tab>('types')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="card w-full max-w-2xl mx-4 flex flex-col max-h-[85vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 shrink-0">
          <h2 className="text-lg font-semibold">Nastavení katalogů</h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-200 shrink-0">
          <TabBtn active={tab === 'types'} onClick={() => setTab('types')}>
            Typy staveb
          </TabBtn>
          <TabBtn active={tab === 'materials'} onClick={() => setTab('materials')}>
            Materiály
          </TabBtn>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {tab === 'types' ? <StructureTypesTab /> : <MaterialsTab />}
        </div>
      </div>
    </div>
  )
}

function TabBtn({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`px-6 py-3 text-sm font-medium border-b-2 transition-colors
        ${active ? 'border-brand-600 text-brand-600' : 'border-transparent text-gray-500 hover:text-gray-700'}`}
    >
      {children}
    </button>
  )
}


// ─── Structure types tab ──────────────────────────────────────────────────────

function StructureTypesTab() {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

  const { data: types = [] } = useQuery({
    queryKey: ['config', 'structure-types'],
    queryFn: configApi.listStructureTypes,
  })

  const createMut = useMutation({
    mutationFn: configApi.createStructureType,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['config', 'structure-types'] }); setAdding(false) },
  })
  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => configApi.updateStructureType(id, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['config', 'structure-types'] }); setEditingId(null) },
  })
  const deleteMut = useMutation({
    mutationFn: configApi.deleteStructureType,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['config', 'structure-types'] }),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <p className="text-sm text-gray-500">
          Typy staveb se zobrazují při přidávání nové konstrukce.
        </p>
        <button onClick={() => setAdding(true)} className="btn-primary text-sm">
          <Plus size={14} /> Přidat typ
        </button>
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-left text-xs text-gray-500 uppercase tracking-wide">
            <th className="pb-2 w-8" />
            <th className="pb-2">Název</th>
            <th className="pb-2">Kód</th>
            <th className="pb-2 w-8" />
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {types.map(t => (
            editingId === t.id
              ? <EditTypeRow key={t.id} item={t} onSave={data => updateMut.mutate({ id: t.id, data })} onCancel={() => setEditingId(null)} />
              : <TypeRow key={t.id} item={t} onEdit={() => setEditingId(t.id)} onDelete={() => { if (confirm(`Smazat typ "${t.name}"?`)) deleteMut.mutate(t.id) }} />
          ))}
          {adding && (
            <NewTypeRow
              onSave={data => createMut.mutate(data)}
              onCancel={() => setAdding(false)}
              saving={createMut.isPending}
            />
          )}
        </tbody>
      </table>

      {types.length === 0 && !adding && (
        <p className="text-center text-gray-400 text-sm py-6">Žádné typy. Přidejte první.</p>
      )}
    </div>
  )
}

function TypeRow({ item: t, onEdit, onDelete }: { item: StructureTypeConfig; onEdit: () => void; onDelete: () => void }) {
  return (
    <tr className="group">
      <td className="py-2 text-gray-300"><GripVertical size={14} /></td>
      <td className="py-2 font-medium text-gray-800">{t.name}</td>
      <td className="py-2"><code className="text-xs bg-gray-100 px-1.5 py-0.5 rounded text-gray-600">{t.code}</code></td>
      <td className="py-2">
        <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button onClick={onEdit} className="text-gray-400 hover:text-brand-600 p-1"><Pencil size={13} /></button>
          <button onClick={onDelete} className="text-gray-400 hover:text-red-500 p-1"><Trash2 size={13} /></button>
        </div>
      </td>
    </tr>
  )
}

function EditTypeRow({ item, onSave, onCancel }: { item: StructureTypeConfig; onSave: (d: any) => void; onCancel: () => void }) {
  const [name, setName] = useState(item.name)
  const [code, setCode] = useState(item.code)
  return (
    <tr>
      <td />
      <td className="py-1"><input className="input text-sm py-1" value={name} onChange={e => setName(e.target.value)} /></td>
      <td className="py-1"><input className="input text-sm py-1 font-mono" value={code} onChange={e => setCode(e.target.value.toLowerCase().replace(/\s+/g, '_'))} /></td>
      <td className="py-1">
        <div className="flex gap-1">
          <button onClick={() => onSave({ name, code })} className="text-green-600 hover:text-green-700 p-1"><Check size={14} /></button>
          <button onClick={onCancel} className="text-gray-400 hover:text-red-500 p-1"><XCircle size={14} /></button>
        </div>
      </td>
    </tr>
  )
}

function NewTypeRow({ onSave, onCancel, saving }: { onSave: (d: any) => void; onCancel: () => void; saving: boolean }) {
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  return (
    <tr className="bg-brand-50">
      <td />
      <td className="py-1.5 pr-2">
        <input
          className="input text-sm py-1"
          placeholder="Název"
          value={name}
          onChange={e => {
            setName(e.target.value)
            setCode(e.target.value.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, ''))
          }}
          autoFocus
        />
      </td>
      <td className="py-1.5 pr-2">
        <input
          className="input text-sm py-1 font-mono"
          placeholder="kod"
          value={code}
          onChange={e => setCode(e.target.value.toLowerCase().replace(/\s+/g, '_'))}
        />
      </td>
      <td className="py-1.5">
        <div className="flex gap-1">
          <button onClick={() => onSave({ name, code, sort_order: 99 })} disabled={!name || !code || saving} className="text-green-600 hover:text-green-700 p-1 disabled:opacity-40"><Check size={14} /></button>
          <button onClick={onCancel} className="text-gray-400 hover:text-red-500 p-1"><XCircle size={14} /></button>
        </div>
      </td>
    </tr>
  )
}


// ─── Materials tab ────────────────────────────────────────────────────────────

function MaterialsTab() {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

  const { data: materials = [] } = useQuery({
    queryKey: ['config', 'materials'],
    queryFn: configApi.listMaterials,
  })

  const createMut = useMutation({
    mutationFn: configApi.createMaterial,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['config', 'materials'] }); setAdding(false) },
  })
  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => configApi.updateMaterial(id, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['config', 'materials'] }); setEditingId(null) },
  })
  const deleteMut = useMutation({
    mutationFn: configApi.deleteMaterial,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['config', 'materials'] }),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <p className="text-sm text-gray-500">
          Materiály se přiřazují ke konstrukcím s procentuálním podílem.
        </p>
        <button onClick={() => setAdding(true)} className="btn-primary text-sm">
          <Plus size={14} /> Přidat materiál
        </button>
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-left text-xs text-gray-500 uppercase tracking-wide">
            <th className="pb-2 w-8" />
            <th className="pb-2">Barva</th>
            <th className="pb-2">Název</th>
            <th className="pb-2">Kód</th>
            <th className="pb-2 w-8" />
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {materials.map(m => (
            editingId === m.id
              ? <EditMatRow key={m.id} item={m} onSave={data => updateMut.mutate({ id: m.id, data })} onCancel={() => setEditingId(null)} />
              : <MatRow key={m.id} item={m} onEdit={() => setEditingId(m.id)} onDelete={() => { if (confirm(`Smazat materiál "${m.name}"?`)) deleteMut.mutate(m.id) }} />
          ))}
          {adding && (
            <NewMatRow
              onSave={data => createMut.mutate(data)}
              onCancel={() => setAdding(false)}
              saving={createMut.isPending}
            />
          )}
        </tbody>
      </table>

      {materials.length === 0 && !adding && (
        <p className="text-center text-gray-400 text-sm py-6">Žádné materiály. Přidejte první.</p>
      )}
    </div>
  )
}

function MatRow({ item: m, onEdit, onDelete }: { item: MaterialConfig; onEdit: () => void; onDelete: () => void }) {
  return (
    <tr className="group">
      <td className="py-2 text-gray-300"><GripVertical size={14} /></td>
      <td className="py-2">
        <div className="w-5 h-5 rounded border border-gray-200 shadow-sm" style={{ background: m.color }} />
      </td>
      <td className="py-2 font-medium text-gray-800">{m.name}</td>
      <td className="py-2"><code className="text-xs bg-gray-100 px-1.5 py-0.5 rounded text-gray-600">{m.code}</code></td>
      <td className="py-2">
        <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button onClick={onEdit} className="text-gray-400 hover:text-brand-600 p-1"><Pencil size={13} /></button>
          <button onClick={onDelete} className="text-gray-400 hover:text-red-500 p-1"><Trash2 size={13} /></button>
        </div>
      </td>
    </tr>
  )
}

function EditMatRow({ item, onSave, onCancel }: { item: MaterialConfig; onSave: (d: any) => void; onCancel: () => void }) {
  const [name, setName] = useState(item.name)
  const [code, setCode] = useState(item.code)
  const [color, setColor] = useState(item.color)
  return (
    <tr>
      <td />
      <td className="py-1">
        <input type="color" value={color} onChange={e => setColor(e.target.value)} className="w-7 h-7 rounded cursor-pointer border border-gray-200" />
      </td>
      <td className="py-1 pr-2"><input className="input text-sm py-1" value={name} onChange={e => setName(e.target.value)} /></td>
      <td className="py-1 pr-2"><input className="input text-sm py-1 font-mono" value={code} onChange={e => setCode(e.target.value.toLowerCase().replace(/\s+/g, '_'))} /></td>
      <td className="py-1">
        <div className="flex gap-1">
          <button onClick={() => onSave({ name, code, color })} className="text-green-600 hover:text-green-700 p-1"><Check size={14} /></button>
          <button onClick={onCancel} className="text-gray-400 hover:text-red-500 p-1"><XCircle size={14} /></button>
        </div>
      </td>
    </tr>
  )
}

function NewMatRow({ onSave, onCancel, saving }: { onSave: (d: any) => void; onCancel: () => void; saving: boolean }) {
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [color, setColor] = useState('#6b7280')
  return (
    <tr className="bg-brand-50">
      <td />
      <td className="py-1.5">
        <input type="color" value={color} onChange={e => setColor(e.target.value)} className="w-7 h-7 rounded cursor-pointer border border-gray-200" />
      </td>
      <td className="py-1.5 pr-2">
        <input
          className="input text-sm py-1"
          placeholder="Název materiálu"
          value={name}
          onChange={e => {
            setName(e.target.value)
            setCode(e.target.value.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, ''))
          }}
          autoFocus
        />
      </td>
      <td className="py-1.5 pr-2">
        <input
          className="input text-sm py-1 font-mono"
          placeholder="kod"
          value={code}
          onChange={e => setCode(e.target.value.toLowerCase().replace(/\s+/g, '_'))}
        />
      </td>
      <td className="py-1.5">
        <div className="flex gap-1">
          <button onClick={() => onSave({ name, code, color, sort_order: 99 })} disabled={!name || !code || saving} className="text-green-600 hover:text-green-700 p-1 disabled:opacity-40"><Check size={14} /></button>
          <button onClick={onCancel} className="text-gray-400 hover:text-red-500 p-1"><XCircle size={14} /></button>
        </div>
      </td>
    </tr>
  )
}
