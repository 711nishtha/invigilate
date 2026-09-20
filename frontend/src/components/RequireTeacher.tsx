import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '../lib/auth'
import { Spinner } from './Spinner'

/**
 * Route guard for teacher pages.
 *
 * This decides what to *render*, nothing more. It is not a security control:
 * the backend authorizes every teacher endpoint independently, so bypassing
 * this component in the browser yields a dashboard full of 401s.
 */
export function RequireTeacher({ children }: { children: ReactNode }) {
  const { session, profile, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Checking your session" />
      </div>
    )
  }

  if (!session) {
    return <Navigate to="/" replace state={{ from: location.pathname }} />
  }

  if (profile && profile.role !== 'TEACHER' && profile.role !== 'ADMIN') {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="card max-w-md p-6 text-center">
          <h1 className="text-lg font-semibold text-slate-900">Teacher access required</h1>
          <p className="mt-2 text-sm text-slate-600">
            This account is not registered as a teacher. Ask an administrator to update your role.
          </p>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
