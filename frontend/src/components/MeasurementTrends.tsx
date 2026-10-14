import { useQuery } from '@tanstack/react-query'
import { TrendingUp, TrendingDown, Minus, AlertTriangle, Activity } from 'lucide-react'
import { structuresApi, type MeasurementTrend } from '../api/structures'

/**
 * Degradation tracking: measurements sharing a name across a structure's
 * inspections are grouped into a time series. A growing value (e.g. crack
 * width) is flagged as worsening — the core "watch it get worse over time"
 * signal for periodic inspections.
 */
export default function MeasurementTrends({ structureId }: { structureId: string }) {
  const { data } = useQuery({
    queryKey: ['measurement-trends', structureId],
    queryFn: () => structuresApi.measurementTrends(structureId),
  })
  const series = data?.series ?? []
  if (!series.length) return null

  return (
    <div className="card p-4">
      <h3 className="font-semibold text-gray-900 flex items-center gap-2 mb-3">
        <Activity size={16} className="text-brand-600" /> Trendy měření v čase
      </h3>
      <div className="space-y-2.5">
        {series.map((s, i) => <TrendRow key={i} s={s} />)}
      </div>
      <p className="text-[11px] text-gray-400 mt-3">
        Měření se stejným názvem napříč inspekcemi se považují za stejné místo. Rostoucí hodnota (např. šířka trhliny) = zhoršování.
      </p>
    </div>
  )
}

function TrendRow({ s }: { s: MeasurementTrend }) {
  // A rising value is treated as degradation → red; falling → green.
  const worse = s.trend === 'up'
  const Icon = s.trend === 'up' ? TrendingUp : s.trend === 'down' ? TrendingDown : Minus
  const color = s.trend === 'up' ? 'text-red-600' : s.trend === 'down' ? 'text-green-600' : 'text-gray-400'

  return (
    <div className="flex items-center gap-3 py-1.5 border-b border-gray-100 last:border-0">
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-800 truncate flex items-center gap-1.5">
          {worse && <AlertTriangle size={13} className="text-red-500 shrink-0" />}
          {s.name}
        </p>
        <p className="text-[11px] text-gray-400">
          {s.points.length} měření · {s.first.toFixed(2)} → {s.last.toFixed(2)} {s.unit}
        </p>
      </div>
      <Sparkline points={s.points.map(p => p.value)} worse={worse} />
      <div className={`flex items-center gap-1 text-xs font-semibold ${color} shrink-0 w-20 justify-end`}>
        <Icon size={13} />
        {s.change > 0 ? '+' : ''}{s.change.toFixed(2)} {s.unit}
      </div>
    </div>
  )
}

function Sparkline({ points, worse }: { points: number[]; worse: boolean }) {
  const w = 90, h = 26, pad = 3
  const min = Math.min(...points), max = Math.max(...points)
  const span = max - min || 1
  const step = points.length > 1 ? (w - pad * 2) / (points.length - 1) : 0
  const coords = points.map((v, i) => {
    const x = pad + i * step
    const y = h - pad - ((v - min) / span) * (h - pad * 2)
    return `${x},${y}`
  })
  const stroke = worse ? '#dc2626' : '#16a34a'
  return (
    <svg width={w} height={h} className="shrink-0">
      <polyline points={coords.join(' ')} fill="none" stroke={stroke} strokeWidth={1.5}
        strokeLinejoin="round" strokeLinecap="round" />
      {points.map((v, i) => {
        const x = pad + i * step
        const y = h - pad - ((v - min) / span) * (h - pad * 2)
        return <circle key={i} cx={x} cy={y} r={1.6} fill={stroke} />
      })}
    </svg>
  )
}
