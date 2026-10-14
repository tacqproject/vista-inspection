import { create } from 'zustand'

export type Role = 'admin' | 'inspector' | 'viewer'

export interface AuthUser {
  id: string
  username: string
  full_name?: string | null
  role: Role
  is_active: boolean
  must_change_password: boolean
  created_at: string
  last_login?: string | null
}

const TOKEN_KEY = 'vista_token'
const USER_KEY = 'vista_user'

function loadUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as AuthUser) : null
  } catch {
    return null
  }
}

interface AuthState {
  token: string | null
  user: AuthUser | null
  setSession: (token: string, user: AuthUser) => void
  setUser: (user: AuthUser) => void
  logout: () => void
  /** role rank helper: true when the user can create/edit inspection data */
  canEdit: () => boolean
  isAdmin: () => boolean
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: localStorage.getItem(TOKEN_KEY),
  user: loadUser(),

  setSession: (token, user) => {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
    set({ token, user })
  },

  setUser: (user) => {
    localStorage.setItem(USER_KEY, JSON.stringify(user))
    set({ user })
  },

  logout: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    set({ token: null, user: null })
  },

  canEdit: () => {
    const r = get().user?.role
    return r === 'admin' || r === 'inspector'
  },
  isAdmin: () => get().user?.role === 'admin',
}))

/** Read the token synchronously (used by the axios interceptor). */
export const getToken = () => localStorage.getItem(TOKEN_KEY)
