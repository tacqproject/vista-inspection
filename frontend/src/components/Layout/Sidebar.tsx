import { useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { Building2, LayoutDashboard, Plus, Settings, BookOpen, Users, LogOut } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { structuresApi } from '../../api/structures'
import { useAuthStore } from '../../store/authStore'
import { ROLE_LABELS } from '../../types'
import NewStructureModal from '../NewStructureModal'
import SettingsModal from '../SettingsModal'

export default function Sidebar({ open = false, onClose }: { open?: boolean; onClose?: () => void }) {
  const [showNew, setShowNew] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const navigate = useNavigate()
  const { user, canEdit, isAdmin, logout } = useAuthStore()

  const { data: structures = [] } = useQuery({
    queryKey: ['structures'],
    queryFn: structuresApi.list,
  })

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <>
      {/* Backdrop (mobile only, when the drawer is open) */}
      {open && (
        <div className="lg:hidden fixed inset-0 z-30 bg-black/50" onClick={onClose} />
      )}
      <aside
        onClick={(e) => {
          // close the drawer after tapping any navigation link (mobile)
          if ((e.target as HTMLElement).closest('a')) onClose?.()
        }}
        className={`w-64 bg-slate-950 text-slate-100 flex flex-col h-full shrink-0 border-r border-slate-800
          fixed inset-y-0 left-0 z-40 transform transition-transform duration-200
          lg:static lg:z-auto lg:translate-x-0 ${open ? 'translate-x-0' : '-translate-x-full'}`}
      >
        {/* Logo */}
        <div className="px-4 py-4 border-b border-slate-800/80">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-md bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center shadow-inner">
              <Building2 size={17} className="text-white" />
            </div>
            <div>
              <span className="text-[15px] font-bold tracking-[0.14em]">VISTA RAW</span>
              <p className="text-[10px] text-slate-500 uppercase tracking-widest leading-none mt-0.5">Inspekce konstrukcí</p>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
          <NavLink
            to="/"
            end
            className={({ isActive }) =>
              `relative flex items-center gap-2.5 px-3 py-2 rounded-md text-[13px] font-medium transition-colors
              ${isActive
                ? 'bg-slate-800/80 text-white shadow-[inset_2px_0_0_0_theme(colors.brand.400)]'
                : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`
            }
          >
            <LayoutDashboard size={15} />
            Dashboard
          </NavLink>

          <div className="pt-3 pb-1 px-3">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-semibold text-slate-500 uppercase tracking-[0.15em]">
                Konstrukce
              </span>
              {canEdit() && (
                <button
                  onClick={() => setShowNew(true)}
                  className="text-slate-400 hover:text-white p-0.5 rounded"
                  title="Přidat konstrukci"
                >
                  <Plus size={14} />
                </button>
              )}
            </div>
          </div>

          {structures.map(s => (
            <NavLink
              key={s.id}
              to={`/structures/${s.id}`}
              className={({ isActive }) =>
                `relative flex items-center gap-2.5 px-3 py-2 rounded-md text-[13px] font-medium transition-colors truncate
                ${isActive
                  ? 'bg-slate-800/80 text-white shadow-[inset_2px_0_0_0_theme(colors.brand.400)]'
                  : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`
              }
            >
              <Building2 size={14} className="shrink-0 opacity-70" />
              <span className="truncate">{s.name}</span>
            </NavLink>
          ))}

          {structures.length === 0 && (
            <p className="text-xs text-slate-500 px-3 py-2">
              Žádné konstrukce. Přidejte první.
            </p>
          )}
        </nav>

        {/* Footer */}
        <div className="px-2 py-3 border-t border-slate-700">
          <NavLink
            to="/docs"
            className={({ isActive }) =>
              `relative flex items-center gap-2.5 w-full px-3 py-2 rounded-md text-[13px] font-medium transition-colors
              ${isActive
                ? 'bg-slate-800/80 text-white shadow-[inset_2px_0_0_0_theme(colors.brand.400)]'
                : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`
            }
          >
            <BookOpen size={14} />
            Dokumentace
          </NavLink>
          {isAdmin() && (
            <>
              <NavLink
                to="/users"
                className={({ isActive }) =>
                  `relative flex items-center gap-2.5 w-full px-3 py-2 rounded-md text-[13px] font-medium transition-colors
                  ${isActive
                    ? 'bg-slate-800/80 text-white shadow-[inset_2px_0_0_0_theme(colors.brand.400)]'
                    : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`
                }
              >
                <Users size={14} />
                Uživatelé
              </NavLink>
              <button
                onClick={() => setShowSettings(true)}
                className="flex items-center gap-2.5 w-full px-3 py-2 rounded-md text-[13px] font-medium text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors"
              >
                <Settings size={14} />
                Nastavení katalogů
              </button>
            </>
          )}

          {/* Current user */}
          <div className="mt-2 pt-2 border-t border-slate-800/80 flex items-center gap-2 px-3 py-1.5">
            <NavLink to="/account" className="flex-1 min-w-0 group" title="Můj účet">
              <div className="text-[13px] font-medium text-slate-200 truncate group-hover:text-white">
                {user?.full_name || user?.username}
              </div>
              <div className="text-[10px] text-slate-500 uppercase tracking-wide">
                {user ? ROLE_LABELS[user.role] : ''}
              </div>
            </NavLink>
            <button
              onClick={handleLogout}
              className="text-slate-500 hover:text-red-400 p-1 rounded shrink-0"
              title="Odhlásit se"
            >
              <LogOut size={15} />
            </button>
          </div>
          <p className="text-[9px] text-slate-600 px-3 pt-1.5 tracking-wider uppercase">VISTA v1.0 · AliceVision pipeline</p>
        </div>
      </aside>

      {showNew && <NewStructureModal onClose={() => setShowNew(false)} />}
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </>
  )
}
