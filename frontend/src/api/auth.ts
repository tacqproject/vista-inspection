import { api } from './client'
import type { AuthUser, Role } from '../store/authStore'

interface LoginResponse {
  access_token: string
  token_type: string
  user: AuthUser
}

export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>('/auth/login', { username, password }).then(r => r.data),

  me: () => api.get<AuthUser>('/auth/me').then(r => r.data),

  changeOwnPassword: (current_password: string, new_password: string) =>
    api.post('/auth/me/password', { current_password, new_password }),

  // ─ admin ─
  listUsers: () => api.get<AuthUser[]>('/auth/users').then(r => r.data),

  createUser: (data: { username: string; password: string; full_name?: string; role: Role }) =>
    api.post<AuthUser>('/auth/users', data).then(r => r.data),

  updateUser: (id: string, data: { full_name?: string; role?: Role; is_active?: boolean }) =>
    api.patch<AuthUser>(`/auth/users/${id}`, data).then(r => r.data),

  resetPassword: (id: string, new_password: string) =>
    api.post(`/auth/users/${id}/password`, { new_password }),

  deleteUser: (id: string) => api.delete(`/auth/users/${id}`),
}
