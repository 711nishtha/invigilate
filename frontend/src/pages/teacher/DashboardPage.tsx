import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { api, type HealthStatus } from '../../lib/api'
import { useAuth } from '../../lib/auth'

/**
 * Dashboard shell.
 *
 * The statistic tiles are wired to the quiz and results endpoints in phases 2
 * and 6. Until those exist they show an em dash rather than a plausible-looking
 * number — an invented "42 attempts" is worse than an honest blank.
 */
const STATS = [
  { label: 'Total quizzes', hint: 'Phase 2' },
  { label: 'Active quizzes', hint: 'Phase 2' },
  { label: 'Total attempts', hint: 'Phase 4' },
  { label: 'Average score', hint: 'Phase 6' },
  { label: 'Flagged attempts', hint: 'Phase 5' },
]

export function DashboardPage() {
  const { profile } = useAuth()
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api
      .health()
      .then((result) => active && setHealth(result))
      .catch((caught) => active && setHealthError(caught instanceof Error ? caught.message : 'API unreachable'))
    return () => {
      active = false
    }
  }, [])

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">
            Welcome back, {profile?.name?.split(' ')[0] ?? 'there'}
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Create a quiz, review AI-generated variants, then publish one link for your class.
          </p>
        </div>
        <Link to="/teacher/quizzes/new" className="btn-primary">
          Create quiz
        </Link>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {STATS.map((stat) => (
          <div key={stat.label} className="card p-5">
            <p className="text-sm font-medium text-slate-600">{stat.label}</p>
            <p className="mt-2 text-3xl font-semibold tracking-tight text-slate-300">—</p>
            <p className="mt-1 text-xs text-slate-400">Available in {stat.hint}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card p-6 lg:col-span-2">
          <h2 className="text-base font-semibold text-slate-900">Getting started</h2>
          <ol className="mt-4 space-y-4">
            {[
              ['Create a quiz', 'Set the duration, question count, security level and violation budget.'],
              ['Add your original questions', 'One question family per question; variants are generated from it.'],
              ['Generate and review variants', 'Nothing reaches a student until you approve it.'],
              ['Publish and share one link', 'Every student gets the same URL and a different exam.'],
              ['Review results', 'Scores, integrity timelines, Excel export and email delivery.'],
            ].map(([title, body], index) => (
              <li key={title} className="flex gap-4">
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700">
                  {index + 1}
                </span>
                <div>
                  <p className="text-sm font-medium text-slate-900">{title}</p>
                  <p className="text-sm text-slate-600">{body}</p>
                </div>
              </li>
            ))}
          </ol>
        </section>

        <section className="card p-6">
          <h2 className="text-base font-semibold text-slate-900">System status</h2>
          <dl className="mt-4 space-y-3 text-sm">
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">API</dt>
              <dd>
                {healthError ? (
                  <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
                    Unreachable
                  </span>
                ) : health ? (
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                    {health.status}
                  </span>
                ) : (
                  <Spinner label="" />
                )}
              </dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Version</dt>
              <dd className="font-mono text-xs text-slate-700">{health?.version ?? '—'}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Environment</dt>
              <dd className="font-mono text-xs text-slate-700">{health?.environment ?? '—'}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Server time</dt>
              <dd className="font-mono text-xs text-slate-700">
                {health ? new Date(health.server_time).toLocaleTimeString() : '—'}
              </dd>
            </div>
          </dl>
          <p className="mt-4 border-t border-slate-200 pt-3 text-xs leading-relaxed text-slate-500">
            Exam timing uses the server clock shown here, never the clock on a student's computer.
          </p>
        </section>
      </div>
    </div>
  )
}
