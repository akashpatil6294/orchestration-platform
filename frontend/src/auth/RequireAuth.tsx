/**
 * Route guard.
 *
 * While the stored session is being read the visitor sees a loading state rather
 * than a flash of the login page. Once resolved, a visitor without a session is
 * sent to `/login` and the page they wanted is remembered so they land on it
 * after signing in.
 */
import type { ReactNode } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { useAuth } from './AuthProvider'
import { rememberDestination } from './AuthProvider'
import { FullPageLoader } from '../components/StatusView'

export function RequireAuth({ children }: { children?: ReactNode }) {
  const { session, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return <FullPageLoader label="Checking your session…" />
  }

  if (!session) {
    rememberDestination(`${location.pathname}${location.search}`)
    return <Navigate to="/login" replace />
  }

  return children ? <>{children}</> : <Outlet />
}

export default RequireAuth
