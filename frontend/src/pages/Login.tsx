import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Building2, LogIn, Loader2, AlertCircle } from 'lucide-react'
import { authApi } from '../api/auth'
import { useAuthStore } from '../store/authStore'
import { errorMessage } from '../api/client'

export default function Login() {
  const navigate = useNavigate()
  const location = useLocation()
  const setSession = useAuthStore(s => s.setSession)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const from = (location.state as any)?.from ?? '/'

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await authApi.login(username.trim(), password)
      setSession(res.access_token, res.user)
      navigate(res.user.must_change_password ? '/account' : from, { replace: true })
    } catch (err: any) {
      setError(errorMessage(err, 'Přihlášení se nezdařilo'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-950 px-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center shadow-lg mb-3">
            <Building2 size={28} className="text-white" />
          </div>
          <h1 className="text-xl font-bold tracking-[0.14em] text-white">VISTA RAW</h1>
          <p className="text-[11px] text-slate-500 uppercase tracking-widest mt-1">
            Inspekce konstrukcí
          </p>
        </div>

        <form onSubmit={submit} className="card p-6 space-y-4">
          <div>
            <label className="label">Uživatelské jméno</label>
            <input
              className="input" autoFocus autoComplete="username"
              value={username} onChange={e => setUsername(e.target.value)}
              placeholder="admin"
            />
          </div>
          <div>
            <label className="label">Heslo</label>
            <input
              className="input" type="password" autoComplete="current-password"
              value={password} onChange={e => setPassword(e.target.value)}
              placeholder="••••••••"
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-200 rounded-md px-3 py-2">
              <AlertCircle size={15} className="shrink-0" />
              {error}
            </div>
          )}

          <button type="submit" disabled={busy || !username || !password} className="btn-primary w-full">
            {busy ? <Loader2 size={15} className="animate-spin" /> : <LogIn size={15} />}
            Přihlásit se
          </button>
        </form>
      </div>
    </div>
  )
}
