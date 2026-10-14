import { useState } from 'react'
import { KeyRound, Loader2, CheckCircle2, AlertCircle, ShieldAlert } from 'lucide-react'
import { authApi } from '../api/auth'
import { useAuthStore } from '../store/authStore'
import { ROLE_LABELS } from '../types'
import { errorMessage } from '../api/client'

export default function Account() {
  const { user, setUser } = useAuthStore()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setMsg(null)
    if (next !== confirm) {
      setMsg({ ok: false, text: 'Nová hesla se neshodují' })
      return
    }
    setBusy(true)
    try {
      await authApi.changeOwnPassword(current, next)
      if (user) setUser({ ...user, must_change_password: false })
      setMsg({ ok: true, text: 'Heslo bylo změněno' })
      setCurrent(''); setNext(''); setConfirm('')
    } catch (err: any) {
      setMsg({ ok: false, text: errorMessage(err, 'Změna se nezdařila') })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-lg mx-auto px-6 py-8">
      <h1 className="text-lg font-bold text-slate-900 mb-1">Můj účet</h1>
      <p className="text-sm text-slate-500 mb-6">
        {user?.full_name || user?.username} · {user ? ROLE_LABELS[user.role] : ''}
      </p>

      {user?.must_change_password && (
        <div className="flex items-center gap-2 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-md px-3 py-2 mb-4">
          <ShieldAlert size={16} className="shrink-0" />
          Používáte výchozí nebo resetované heslo. Nastavte si prosím vlastní.
        </div>
      )}

      <form onSubmit={submit} className="card p-6 space-y-4">
        <div className="flex items-center gap-2 text-slate-800 font-semibold text-sm">
          <KeyRound size={15} /> Změna hesla
        </div>
        <div>
          <label className="label">Současné heslo</label>
          <input className="input" type="password" autoComplete="current-password"
            value={current} onChange={e => setCurrent(e.target.value)} />
        </div>
        <div>
          <label className="label">Nové heslo</label>
          <input className="input" type="password" autoComplete="new-password"
            value={next} onChange={e => setNext(e.target.value)} />
        </div>
        <div>
          <label className="label">Nové heslo znovu</label>
          <input className="input" type="password" autoComplete="new-password"
            value={confirm} onChange={e => setConfirm(e.target.value)} />
        </div>

        {msg && (
          <div className={`flex items-center gap-2 text-sm rounded-md px-3 py-2 border ${
            msg.ok ? 'text-green-700 bg-green-50 border-green-200'
                   : 'text-red-600 bg-red-50 border-red-200'}`}>
            {msg.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
            {msg.text}
          </div>
        )}

        <button className="btn-primary" disabled={busy || !current || !next}>
          {busy && <Loader2 size={15} className="animate-spin" />} Uložit heslo
        </button>
      </form>
    </div>
  )
}
