import axios from 'axios'
import { getToken, useAuthStore } from '../store/authStore'

const BASE_URL = import.meta.env.VITE_API_URL ?? ''

export const api = axios.create({
  baseURL: `${BASE_URL}/api`,
  headers: { 'Content-Type': 'application/json' },
})

// Attach the bearer token to every request.
api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// On 401 the token is missing/expired — drop the session and let the route
// guard bounce the user to the login screen. (Login itself is exempt so a
// wrong-password 401 doesn't wipe an existing session.)
api.interceptors.response.use(
  (r) => r,
  (error) => {
    const url: string = error.config?.url ?? ''
    if (error.response?.status === 401 && !url.includes('/auth/login')) {
      useAuthStore.getState().logout()
    }
    return Promise.reject(error)
  },
)

export const uploadsUrl = (path: string) =>
  path ? `${BASE_URL}/uploads/${path}` : ''

/**
 * Extract a human-readable message from an axios error. FastAPI's `detail` is
 * a plain string for our HTTPExceptions but an ARRAY of objects for 422
 * validation errors — rendering that array as a React child crashes, so always
 * funnel error text through here.
 */
export function errorMessage(err: any, fallback = 'Něco se nepovedlo'): string {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const msgs = detail.map((d: any) => d?.msg).filter(Boolean)
    if (msgs.length) return msgs.join('; ')
  }
  if (typeof err?.message === 'string' && !err.response) return err.message
  return fallback
}
