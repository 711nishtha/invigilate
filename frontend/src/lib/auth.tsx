/**
 * Authentication context.
 *
 * Holds the Supabase session and the backend profile. The profile — not the
 * session — carries the role, because the backend is the authority on what a
 * user may do. The UI uses the role only to decide what to render; every
 * privileged action is authorized again server-side.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { Session } from '@supabase/supabase-js'

import { ApiError, api, type UserProfile } from './api'
import { supabase } from './supabase'

interface AuthState {
  session: Session | null
  profile: UserProfile | null
  loading: boolean
  error: string | null
  signIn: (email: string, password: string) => Promise<void>
  signUp: (name: string, email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  refreshProfile: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadProfile = useCallback(async (activeSession: Session | null) => {
    if (!activeSession) {
      setProfile(null)
      return
    }
    try {
      setProfile(await api.me())
    } catch (caught) {
      // A verified session with no profile row is the normal state straight
      // after sign-up; create it rather than treating it as an error.
      if (caught instanceof ApiError && caught.code === 'PROFILE_NOT_FOUND') {
        const fallbackName =
          (activeSession.user.user_metadata?.name as string | undefined) ??
          activeSession.user.email ??
          'Teacher'
        setProfile(await api.upsertProfile(fallbackName))
        return
      }
      setProfile(null)
      setError(caught instanceof Error ? caught.message : 'Could not load your profile.')
    }
  }, [])

  useEffect(() => {
    let active = true

    supabase.auth.getSession().then(async ({ data }) => {
      if (!active) return
      setSession(data.session)
      await loadProfile(data.session)
      if (active) setLoading(false)
    })

    const { data: listener } = supabase.auth.onAuthStateChange(async (_event, nextSession) => {
      if (!active) return
      setSession(nextSession)
      await loadProfile(nextSession)
    })

    return () => {
      active = false
      listener.subscription.unsubscribe()
    }
  }, [loadProfile])

  const signIn = useCallback(async (email: string, password: string) => {
    setError(null)
    const { error: signInError } = await supabase.auth.signInWithPassword({ email, password })
    if (signInError) throw new Error(signInError.message)
  }, [])

  const signUp = useCallback(async (name: string, email: string, password: string) => {
    setError(null)
    const { data, error: signUpError } = await supabase.auth.signUp({
      email,
      password,
      options: { data: { name } },
    })
    if (signUpError) throw new Error(signUpError.message)
    // With email confirmation enabled there is no session yet; the profile is
    // created on first sign-in instead.
    if (data.session) await api.upsertProfile(name)
  }, [])

  const signOut = useCallback(async () => {
    await supabase.auth.signOut()
    setProfile(null)
  }, [])

  const refreshProfile = useCallback(async () => {
    const { data } = await supabase.auth.getSession()
    await loadProfile(data.session)
  }, [loadProfile])

  const value = useMemo<AuthState>(
    () => ({ session, profile, loading, error, signIn, signUp, signOut, refreshProfile }),
    [session, profile, loading, error, signIn, signUp, signOut, refreshProfile],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside an AuthProvider.')
  return context
}
