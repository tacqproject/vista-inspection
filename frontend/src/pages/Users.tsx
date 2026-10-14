import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Users as UsersIcon, UserPlus, Trash2, KeyRound, ShieldCheck,
  Loader2, X, Check,
} from 'lucide-react'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/client'
import { useAuthStore, type Role } from '../store/authStore'
import { ROLE_LABELS, ROLE_DESCRIPTIONS } from '../types'

const ROLES: Role[] = ['admin', 'inspector', 'viewer']

const roleBadge: Record<Role, string> = {
  admin: 'bg-brand-100 text-brand-800',
  inspector: 'bg-sky-100 text-sky-700',
  viewer: 'bg-slate-100 text-slate-600',
}

export default function Users() {
  const qc = useQueryClient()
  const me = useAuthStore(s => s.user)
  const [showNew, setShowNew] = useState(false)
  const [resetFor, setResetFor] = useState<string | null>(null)

  const { data: users = [], isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: authApi.listUsers,
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['users'] })

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => authApi.updateUser(id, data),
    onSuccess: invalidate,
    onError: (e: any) => alert(errorMessage(e, 'Úprava se nezdařila')),
  })
  const deleteMut = useMutation({
    mutationFn: (id: string) => authApi.deleteUser(id),
    onSuccess: invalidate,
    onError: (e: any) => alert(errorMessage(e, 'Smazání se nezdařilo')),
  })

  return (
    <div className="max-w-4xl mx-auto px-6 py-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-bold text-slate-900 flex items-center gap-2">
            <UsersIcon size={18} className="text-brand-600" /> Správa uživatelů
          </h1>
          <p className="text-sm text-slate-500 mt-0.5">Účty, role a přístupová práva.</p>
        </div>
        <button className="btn-primary" onClick={() => setShowNew(true)}>
          <UserPlus size={15} /> Nový uživatel
        </button>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wider text-slate-400 border-b border-slate-200">
              <th className="px-4 py-2.5 font-semibold">Uživatel</th>
              <th className="px-4 py-2.5 font-semibold">Role</th>
              <th className="px-4 py-2.5 font-semibold">Stav</th>
              <th className="px-4 py-2.5 font-semibold">Poslední přihlášení</th>
              <th className="px-4 py-2.5"></th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-slate-400">
                <Loader2 className="inline animate-spin" size={16} /></td></tr>
            )}
            {users.map(u => {
              const isSelf = u.id === me?.id
              return (
                <tr key={u.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50/60">
                  <td className="px-4 py-2.5">
                    <div className="font-medium text-slate-800">
                      {u.full_name || u.username}
                      {isSelf && <span className="text-slate-400 font-normal"> (vy)</span>}
                    </div>
                    <div className="text-xs text-slate-400">@{u.username}</div>
                  </td>
                  <td className="px-4 py-2.5">
                    <select
                      value={u.role}
                      disabled={updateMut.isPending}
                      onChange={e => updateMut.mutate({ id: u.id, data: { role: e.target.value } })}
                      className={`text-xs font-semibold rounded px-2 py-1 border-0 cursor-pointer ${roleBadge[u.role]}`}
                      title={ROLE_DESCRIPTIONS[u.role]}
                    >
                      {ROLES.map(r => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
                    </select>
                  </td>
                  <td className="px-4 py-2.5">
                    <button
                      onClick={() => updateMut.mutate({ id: u.id, data: { is_active: !u.is_active } })}
                      disabled={updateMut.isPending}
                      className={`badge ${u.is_active ? 'bg-green-100 text-green-700' : 'bg-slate-200 text-slate-500'}`}
                      title="Přepnout aktivní / deaktivovaný"
                    >
                      {u.is_active ? 'Aktivní' : 'Neaktivní'}
                    </button>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-slate-500">
                    {u.last_login ? new Date(u.last_login).toLocaleString('cs-CZ') : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-right whitespace-nowrap">
                    <button className="btn-ghost p-1.5" title="Resetovat heslo"
                      onClick={() => setResetFor(u.id)}>
                      <KeyRound size={14} />
                    </button>
                    {!isSelf && (
                      <button className="btn-ghost p-1.5 text-red-500 hover:text-red-700" title="Smazat"
                        onClick={() => confirm(`Smazat uživatele ${u.username}?`) && deleteMut.mutate(u.id)}>
                        <Trash2 size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-slate-400 mt-3 flex items-center gap-1.5">
        <ShieldCheck size={13} />
        {ROLE_LABELS.admin}: {ROLE_DESCRIPTIONS.admin} · {ROLE_LABELS.inspector}: {ROLE_DESCRIPTIONS.inspector} · {ROLE_LABELS.viewer}: {ROLE_DESCRIPTIONS.viewer}
      </p>

      {showNew && <NewUserModal onClose={() => setShowNew(false)} onSaved={invalidate} />}
      {resetFor && <ResetPasswordModal userId={resetFor} onClose={() => setResetFor(null)} />}
    </div>
  )
}

function NewUserModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [username, setUsername] = useState('')
  const [fullName, setFullName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('viewer')
  const [err, setErr] = useState<string | null>(null)
  const mut = useMutation({
    mutationFn: () => authApi.createUser({ username, password, full_name: fullName || undefined, role }),
    onSuccess: () => { onSaved(); onClose() },
    onError: (e: any) => setErr(errorMessage(e, 'Vytvoření se nezdařilo')),
  })

  return (
    <Modal title="Nový uživatel" onClose={onClose}>
      <div className="space-y-3">
        <div>
          <label className="label">Uživatelské jméno</label>
          <input className="input" value={username} onChange={e => setUsername(e.target.value)} autoFocus />
        </div>
        <div>
          <label className="label">Celé jméno</label>
          <input className="input" value={fullName} onChange={e => setFullName(e.target.value)} />
        </div>
        <div>
          <label className="label">Heslo</label>
          <input className="input" type="text" value={password} onChange={e => setPassword(e.target.value)}
            placeholder="min. 6 znaků" />
        </div>
        <div>
          <label className="label">Role</label>
          <select className="input" value={role} onChange={e => setRole(e.target.value as Role)}>
            {ROLES.map(r => <option key={r} value={r}>{ROLE_LABELS[r]} — {ROLE_DESCRIPTIONS[r]}</option>)}
          </select>
        </div>
        {err && <p className="text-sm text-red-600">{err}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-secondary" onClick={onClose}>Zrušit</button>
          <button className="btn-primary" disabled={mut.isPending || username.trim().length < 3 || password.length < 6}
            onClick={() => { setErr(null); mut.mutate() }}>
            {mut.isPending ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />} Vytvořit
          </button>
        </div>
        <p className="text-[11px] text-slate-400">Jméno alespoň 3 znaky, heslo alespoň 6 znaků.</p>
      </div>
    </Modal>
  )
}

function ResetPasswordModal({ userId, onClose }: { userId: string; onClose: () => void }) {
  const [pw, setPw] = useState('')
  const [done, setDone] = useState(false)
  const mut = useMutation({
    mutationFn: () => authApi.resetPassword(userId, pw),
    onSuccess: () => setDone(true),
    onError: (e: any) => alert(errorMessage(e, 'Reset se nezdařil')),
  })
  return (
    <Modal title="Resetovat heslo" onClose={onClose}>
      {done ? (
        <div className="space-y-4">
          <p className="text-sm text-slate-600">
            Heslo bylo nastaveno. Předejte ho uživateli — při dalším přihlášení bude vyzván ke změně.
          </p>
          <button className="btn-primary w-full" onClick={onClose}>Hotovo</button>
        </div>
      ) : (
        <div className="space-y-3">
          <div>
            <label className="label">Nové heslo</label>
            <input className="input" type="text" value={pw} onChange={e => setPw(e.target.value)}
              placeholder="min. 6 znaků" autoFocus />
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button className="btn-secondary" onClick={onClose}>Zrušit</button>
            <button className="btn-primary" disabled={mut.isPending || pw.length < 6} onClick={() => mut.mutate()}>
              {mut.isPending ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />} Nastavit
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}

function Modal({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 bg-slate-900/50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="card w-full max-w-md p-5" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold text-slate-900">{title}</h2>
          <button className="btn-ghost p-1" onClick={onClose}><X size={16} /></button>
        </div>
        {children}
      </div>
    </div>
  )
}
