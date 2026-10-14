import { useQuery } from '@tanstack/react-query'
import { X, Gauge } from 'lucide-react'
import { api } from '../api/client'

/**
 * Objective quality report of a reconstruction (backend quality-report step):
 * holes, surface noise, deviation from the SfM points, registered photos — and
 * the model rendered from the exact viewpoint of source photos
 * (photo | textured model | bare geometry), where lumpy walls or blurred /
 * doubled textures are obvious.
 */
type Report = {
  available: boolean
  engine?: string
  created?: string
  metrics?: Record<string, number | null>
  renders?: { file: string | null; photo: string; url?: string; error?: string }[]
  photo_prep?: { n_input: number; n_used: number; dropped: { name: string; reason: string }[];
    gain_min: number; gain_max: number; exposure_from_metadata: number }
}

const pct = (v?: number | null, d = 3) => (v == null ? '—' : `${(v * 100).toFixed(d)} %`)
const num = (v?: number | null) => (v == null ? '—' : v.toLocaleString('cs-CZ'))

export default function QualityModal({ inspectionId, onClose }: { inspectionId: string; onClose: () => void }) {
  const { data, isLoading } = useQuery<Report>({
    queryKey: ['quality', inspectionId],
    queryFn: () => api.get(`/inspections/${inspectionId}/quality`).then(r => r.data),
  })
  const m = data?.metrics ?? {}
  const rows: [string, string, string][] = [
    ['Zaregistrované fotky', `${num(m.views_registered)} / ${num(m.views_total)}`,
      'Kolik fotek se podařilo umístit do rekonstrukce. Chybějící fotky = díry a slabší detail.'],
    ['Odchylka od SfM bodů (medián)', pct(m.lm_median_rel),
      'Vzdálenost povrchu od bodů spočtených z mnoha fotek, v % úhlopříčky modelu. Nižší = přesnější.'],
    ['Odchylka od SfM bodů (90 %)', pct(m.lm_p90_rel), '90 % bodů je blíž než tato hodnota.'],
    ['Šum ploch (medián)', m.rough_median == null ? '—' : m.rough_median.toFixed(4),
      'Jak moc se povrch lokálně odchyluje od roviny. Nízká hodnota = rovné stěny, vysoká = „chlupatý“ / hrbolatý povrch.'],
    ['Šum nejrovnějších ploch', m.rough_p25 == null ? '—' : m.rough_p25.toFixed(4),
      'Totéž jen pro nejrovnější čtvrtinu modelu (stěny, pilíře) — tady by mělo být téměř 0.'],
    ['Významné díry', num(m.significant_holes), 'Díry s obvodem nad 1 % úhlopříčky, mimo vnější okraj skenu.'],
    ['Trojúhelníků', num(m.faces), ''],
    ['Samostatných kusů sítě', num(m.components), 'Ideálně 1 (bez plovoucích úlomků).'],
  ]

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={onClose}>
      <div className="card w-full max-w-5xl mx-4 p-6 max-h-[92vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold flex items-center gap-2"><Gauge size={18} /> Kvalita 3D modelu</h2>
          <button onClick={onClose} className="btn-ghost p-1"><X size={18} /></button>
        </div>
        {isLoading && <p className="text-sm text-gray-500">Načítám…</p>}
        {data && !data.available && (
          <p className="text-sm text-gray-500">
            Pro tento model zatím není vyhodnocení kvality — vytvoří se automaticky při příští rekonstrukci.
          </p>
        )}
        {data?.available && (
          <>
            <p className="text-xs text-gray-400 mb-3">
              Metoda: {data.engine ?? '—'} · vyhodnoceno {data.created?.replace('T', ' ')}
            </p>
            <table className="w-full text-sm mb-5">
              <tbody>
                {rows.map(([k, v, hint]) => (
                  <tr key={k} className="border-b border-gray-100">
                    <td className="py-1.5 pr-3 text-gray-600 w-64">{k}</td>
                    <td className="py-1.5 pr-3 font-medium tabular-nums w-32">{v}</td>
                    <td className="py-1.5 text-xs text-gray-400">{hint}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.photo_prep && (
              <div className="mb-5 text-xs text-gray-600 bg-gray-50 rounded p-3">
                <p className="font-semibold mb-1">Příprava fotek</p>
                <p>
                  Použito {data.photo_prep.n_used} z {data.photo_prep.n_input} fotek. Expozice vyrovnána podle
                  metadat u {data.photo_prep.exposure_from_metadata} fotek (zesílení
                  {' '}{data.photo_prep.gain_min.toFixed(2)}–{data.photo_prep.gain_max.toFixed(2)}×), stíny
                  zesvětleny jednou společnou křivkou.
                </p>
                {data.photo_prep.dropped.length > 0 && (
                  <details className="mt-1">
                    <summary className="cursor-pointer">Vyřazeno {data.photo_prep.dropped.length} fotek</summary>
                    <ul className="mt-1 space-y-0.5">
                      {data.photo_prep.dropped.map(d => <li key={d.name}>{d.name} — {d.reason}</li>)}
                    </ul>
                  </details>
                )}
              </div>
            )}
            <p className="text-xs font-semibold text-gray-600 mb-2">
              Pohled z pozice zdrojových fotek — vlevo fotka, uprostřed model s texturou, vpravo holá geometrie
            </p>
            <div className="space-y-3">
              {data.renders?.map(r => (
                <div key={r.photo}>
                  <div className="text-[11px] text-gray-400 mb-1">{r.photo}</div>
                  {r.url
                    ? <img src={r.url} className="w-full rounded border border-gray-200" />
                    : <div className="text-xs text-red-500">Render selhal: {r.error}</div>}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
