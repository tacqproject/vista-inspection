import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Building2, Plus, MapPin, Calendar, ClipboardList, Search, X } from 'lucide-react'
import { structuresApi } from '../api/structures'
import { configApi } from '../api/config'
import NewStructureModal from '../components/NewStructureModal'
import StructuresMap from '../components/StructuresMap'
import { uploadsUrl } from '../api/client'
import { useAuthStore } from '../store/authStore'

export default function Dashboard() {
  const [showNew, setShowNew] = useState(false)
  const navigate = useNavigate()
  const canEdit = useAuthStore(s => s.canEdit())
  const { data: structures = [], isLoading } = useQuery({
    queryKey: ['structures'],
    queryFn: structuresApi.list,
  })
  const { data: structureTypes = [] } = useQuery({
    queryKey: ['config', 'structure-types'],
    queryFn: configApi.listStructureTypes,
  })
  const typeLabel = (code: string) => structureTypes.find(t => t.code === code)?.name ?? code

  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')

  const norm = (s: string) => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
  const filtered = useMemo(() => {
    const q = norm(search.trim())
    return structures.filter(s => {
      if (typeFilter && s.type !== typeFilter) return false
      if (!q) return true
      return norm(s.name).includes(q)
        || norm(s.location_name ?? '').includes(q)
        || norm(s.description ?? '').includes(q)
    })
  }, [structures, search, typeFilter])

  const total = structures.length
  const totalInspections = structures.reduce((s, x) => s + x.inspection_count, 0)

  return (
    <div className="p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-0.5">Přehled katalogizovaných konstrukcí</p>
        </div>
        {canEdit && (
          <button onClick={() => setShowNew(true)} className="btn-primary">
            <Plus size={16} /> Nová konstrukce
          </button>
        )}
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8">
        <StatCard label="Celkem konstrukcí" value={total} icon={<Building2 size={20} className="text-brand-600" />} />
        <StatCard label="Celkem inspekcí" value={totalInspections} icon={<ClipboardList size={20} className="text-brand-600" />} />
        <StatCard label="Poslední přidána" value={structures[0]?.name ?? '—'} small icon={<Calendar size={20} className="text-brand-600" />} />
      </div>

      {/* Map of georeferenced structures */}
      {structures.some(s => s.latitude != null && s.longitude != null) && (
        <div className="mb-8">
          <h2 className="text-sm font-semibold text-gray-500 mb-2 flex items-center gap-1.5">
            <MapPin size={14} /> Mapa konstrukcí
          </h2>
          <StructuresMap structures={structures} />
        </div>
      )}

      {/* Search & filter */}
      {structures.length > 0 && (
        <div className="flex gap-2 mb-5 flex-wrap">
          <div className="relative flex-1 min-w-[220px] max-w-md">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              className="input pl-9 pr-8"
              placeholder="Hledat podle názvu, adresy nebo popisu…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && (
              <button onClick={() => setSearch('')} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
                <X size={14} />
              </button>
            )}
          </div>
          <select className="input w-auto" value={typeFilter} onChange={e => setTypeFilter(e.target.value)}>
            <option value="">Všechny typy</option>
            {structureTypes.map(t => (
              <option key={t.code} value={t.code}>{t.name}</option>
            ))}
          </select>
          {(search || typeFilter) && (
            <span className="self-center text-xs text-gray-400">
              {filtered.length} z {structures.length}
            </span>
          )}
        </div>
      )}

      {/* Grid */}
      {isLoading ? (
        <div className="text-center py-20 text-gray-400">Načítám…</div>
      ) : structures.length === 0 ? (
        <EmptyState onNew={() => setShowNew(true)} />
      ) : filtered.length === 0 ? (
        <div className="text-center py-16 text-gray-400 text-sm">
          Nic neodpovídá hledání „{search}".
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {filtered.map(s => (
            <button
              key={s.id}
              onClick={() => navigate(`/structures/${s.id}`)}
              className="card overflow-hidden text-left hover:shadow-md hover:border-brand-200 transition-all group"
            >
              <div className="h-36 bg-gradient-to-br from-slate-100 to-slate-200 relative overflow-hidden">
                {s.thumbnail_path ? (
                  <img
                    src={uploadsUrl(s.thumbnail_path)}
                    alt={s.name}
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <Building2 size={40} className="text-slate-300" />
                  </div>
                )}
                <span className="absolute top-2 left-2 badge bg-white/90 text-slate-700 text-xs">
                  {typeLabel(s.type)}
                </span>
              </div>
              <div className="p-3">
                <p className="font-semibold text-gray-900 truncate group-hover:text-brand-600">{s.name}</p>
                {s.location_name && (
                  <p className="text-xs text-gray-500 flex items-center gap-1 mt-0.5 truncate">
                    <MapPin size={11} /> {s.location_name}
                  </p>
                )}
                <div className="flex items-center gap-3 mt-2 text-xs text-gray-500">
                  <span className="flex items-center gap-1">
                    <ClipboardList size={11} /> {s.inspection_count} inspekcí
                  </span>
                  {s.last_inspection_date && (
                    <span className="flex items-center gap-1">
                      <Calendar size={11} /> {new Date(s.last_inspection_date).toLocaleDateString('cs-CZ')}
                    </span>
                  )}
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      {showNew && <NewStructureModal onClose={() => setShowNew(false)} />}
    </div>
  )
}

function StatCard({ label, value, icon, small = false }: {
  label: string; value: string | number; icon: React.ReactNode; small?: boolean
}) {
  return (
    <div className="card p-4 flex items-center gap-4">
      <div className="p-2.5 bg-brand-50 border border-brand-100 rounded-md">{icon}</div>
      <div>
        <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</p>
        <p className={`font-bold text-slate-900 tabular-nums ${small ? 'text-sm truncate max-w-[150px]' : 'text-2xl'}`}>{value}</p>
      </div>
    </div>
  )
}

function EmptyState({ onNew }: { onNew: () => void }) {
  return (
    <div className="text-center py-24">
      <Building2 size={48} className="mx-auto text-gray-300 mb-4" />
      <h3 className="text-lg font-semibold text-gray-700 mb-2">Žádné konstrukce</h3>
      <p className="text-sm text-gray-500 mb-6">Začněte přidáním první konstrukce do katalogu.</p>
      <button onClick={onNew} className="btn-primary">
        <Plus size={16} /> Přidat první konstrukci
      </button>
    </div>
  )
}
