import { useEffect, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '../lib/auth'

/**
 * Teacher landing and sign-in.
 *
 * Authentication is Supabase Auth throughout — there is no custom password
 * handling in this codebase, which is deliberate.
 */
export function LandingPage() {
  const { session, loading, signIn, signUp } = useAuth()
  const location = useLocation() as { state?: { from?: string } }

  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setError(null)
    setNotice(null)
  }, [mode])

  if (!loading && session) {
    return <Navigate to={location.state?.from ?? '/teacher/dashboard'} replace />
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      if (mode === 'signin') {
        await signIn(email.trim(), password)
      } else {
        await signUp(name.trim(), email.trim(), password)
        setNotice('Account created. Check your inbox if email confirmation is enabled, then sign in.')
        setMode('signin')
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto grid min-h-screen max-w-6xl gap-12 px-6 py-16 lg:grid-cols-2 lg:items-center">
        <section>
          <p className="text-sm font-medium text-brand-600">Secure Quiz</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight text-slate-900">
            Secure online assessment with integrity monitoring
          </h1>
          <p className="mt-4 text-base leading-relaxed text-slate-600">
            Every student opens the same link and receives a different randomized exam. Timing,
            question assignment, grading and submission are decided by the server, not the browser.
          </p>

          <ul className="mt-8 space-y-3">
            {[
              'AI-generated question variants, validated and approved by you before any student sees them',
              'Randomized question and option order drawn from an approved variant pool',
              'Server-authoritative timer and grading — the browser cannot change either',
              'Integrity event timeline with configurable automatic submission',
              'Results exported to Excel and emailed to you automatically',
            ].map((feature) => (
              <li key={feature} className="flex gap-3 text-sm text-slate-700">
                <span aria-hidden="true" className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500" />
                {feature}
              </li>
            ))}
          </ul>

          {/* Stated on the marketing surface on purpose: overclaiming here would
              mislead the people who rely on the product. */}
          <p className="mt-8 rounded-lg bg-slate-50 p-4 text-xs leading-relaxed text-slate-600 ring-1 ring-slate-200">
            <strong className="font-medium text-slate-800">What this does not claim.</strong> A web
            browser cannot see the rest of the operating system, a second device, or the room. This
            platform raises the cost of cheating through randomization, server authority and an
            auditable event timeline. It does not make cheating impossible.
          </p>
        </section>

        <section className="card p-8">
          <div className="mb-6 flex gap-1 rounded-lg bg-slate-100 p-1">
            {(['signin', 'signup'] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setMode(value)}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  mode === value ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600'
                }`}
              >
                {value === 'signin' ? 'Sign in' : 'Create account'}
              </button>
            ))}
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            {mode === 'signup' && (
              <div>
                <label className="label" htmlFor="name">
                  Full name
                </label>
                <input
                  id="name"
                  className="input"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  required
                  autoComplete="name"
                />
              </div>
            )}

            <div>
              <label className="label" htmlFor="email">
                Email
              </label>
              <input
                id="email"
                type="email"
                className="input"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                required
                autoComplete="email"
              />
            </div>

            <div>
              <label className="label" htmlFor="password">
                Password
              </label>
              <input
                id="password"
                type="password"
                className="input"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                minLength={8}
                autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
              />
            </div>

            {error && (
              <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
                {error}
              </p>
            )}
            {notice && (
              <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-700">{notice}</p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Create account'}
            </button>
          </form>

          <p className="mt-6 border-t border-slate-200 pt-4 text-xs text-slate-500">
            Students do not sign in here. They open the quiz link their teacher shares.
          </p>
        </section>
      </div>
    </div>
  )
}
