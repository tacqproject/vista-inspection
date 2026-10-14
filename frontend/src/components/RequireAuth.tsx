import { useEffect } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { authApi } from '../api/auth'
import { useAuthStore } from '../store/authStore'

/**
 * Gate for authenticated routes. Redirects to /login when there's no token.
 * With a token present it renders immediately (optimistic) and revalidates it
 * against /auth/me in the background — a rejected token trips the 401
 * interceptor, which clears the session and bounces back here.
 */
export default function RequireAuth({ children, adminOnly = false }: {
  children: JSX.Element
  adminOnly?: boolean
}) {
  const location = useLocation()
  const { token, user, setUser } = useAuthStore()

  useEffect(() => {
    if (token) authApi.me().then(setUser).catch(() => {})
  }, [token, setUser])

  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  if (adminOnly && user?.role !== 'admin') {
    return <Navigate to="/" replace />
  }
  return children
}
