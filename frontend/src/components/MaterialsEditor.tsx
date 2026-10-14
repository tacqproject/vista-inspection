import { useQuery } from '@tanstack/react-query'
import { Plus, X, AlertTriangle } from 'lucide-react'
import { configApi } from '../api/config'
import type { StructureMaterial } from '../types'

export interface MaterialRow {
  material_code: string
  material_name: string
  color: string
  percentage: number
}

interface Props {
  value: MaterialRow[]
  onChange: (rows: MaterialRow[]) => void
}

export default function MaterialsEditor({ value, onChange }: Props) {
  const { data: materials = [] } = useQuery({
    queryKey: ['config', 'materials'],
    queryFn: configApi.listMaterials,
  })

  const total = value.reduce((s, r) => s + (r.percentage || 0), 0)
  const overLimit = total > 100

  const addRow = () => {
    if (!materials.length) return
    const unused = materials.find(m => !value.some(r => r.material_code === m.code))
    const mat = unused ?? materials[0]
    onChange([...value, { material_code: mat.code, material_name: mat.name, color: mat.color, percentage: 0 }])
  }

  const removeRow = (idx: number) => onChange(value.filter((_, i) => i !== idx))

  const updateRow = (idx: number, patch: Partial<MaterialRow>) =>
    onChange(value.map((r, i) => i === idx ? { ...r, ...patch } : r))

  const selectMaterial = (idx: number, code: string) => {
    const mat = materials.find(m => m.code === code)
    if (!mat) return
    updateRow(idx, { material_code: mat.code, material_name: mat.name, color: mat.color })
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <label className="label mb-0">Složení materiálů</label>
        <div className="flex items-center gap-2">
          {value.length > 0 && (
            <span className={`text-xs font-medium ${overLimit ? 'text-red-600' : total === 100 ? 'text-green-600' : 'text-gray-500'}`}>
              Celkem: {total.toFixed(1)}%
              {overLimit && <span className="ml-1">⚠ přesahuje 100 %</span>}
            </span>
          )}
          <button type="button" onClick={addRow} className="btn-ghost text-xs py-1 px-2">
            <Plus size={12} /> Přidat
          </button>
        </div>
      </div>

      {value.length === 0 ? (
        <div className="text-xs text-gray-400 text-center py-3 border border-dashed border-gray-200 rounded-lg">
          Žádné materiály — klikněte Přidat
        </div>
      ) : (
        <div className="space-y-2">
          {value.map((row, idx) => (
            <div key={idx} className="flex items-center gap-2">
              {/* Color dot */}
              <div
                className="w-3 h-3 rounded-full shrink-0 border border-white shadow"
                style={{ background: row.color }}
              />

              {/* Material selector */}
              <select
                className="input flex-1 text-xs py-1.5"
                value={row.material_code}
                onChange={e => selectMaterial(idx, e.target.value)}
              >
                {materials.map(m => (
                  <option key={m.code} value={m.code}>{m.name}</option>
                ))}
              </select>

              {/* Percentage input */}
              <div className="relative w-24 shrink-0">
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  className="input text-xs py-1.5 pr-6 text-right"
                  value={row.percentage === 0 ? '' : row.percentage}
                  onChange={e => {
                    const v = parseFloat(e.target.value)
                    updateRow(idx, { percentage: isNaN(v) ? 0 : Math.min(100, Math.max(0, v)) })
                  }}
                  placeholder="0"
                />
                <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400 pointer-events-none">%</span>
              </div>

              {/* Remove */}
              <button
                type="button"
                onClick={() => removeRow(idx)}
                className="text-gray-400 hover:text-red-500 p-1 shrink-0"
              >
                <X size={13} />
              </button>
            </div>
          ))}

          {/* Visual bar */}
          {value.length > 0 && total > 0 && (
            <div className="flex h-2.5 rounded-full overflow-hidden mt-1 gap-px">
              {value.filter(r => r.percentage > 0).map((r, i) => (
                <div
                  key={i}
                  title={`${r.material_name}: ${r.percentage}%`}
                  style={{ width: `${Math.min(r.percentage, 100)}%`, background: r.color }}
                />
              ))}
              {total < 100 && (
                <div className="flex-1 bg-gray-100" title="Nespecifikováno" />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
