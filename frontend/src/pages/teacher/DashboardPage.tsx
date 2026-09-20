import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { ErrorNote, StatusBadge, formatDuration } from '../../components/ui'
import { api, type HealthStatus, type QuizSummary } from '../../lib/api'
import { useAuth } from '../../lib/auth'

/**
 * Dashboard.
 *
 * Every figure here is derived from quizzes the API actually returned. Counters
 * that depend on unbuilt phases show an em dash and say which phase delivers
 * them, rather than a plausible-looking number.
 */
export function DashboardPage() {
  const { profile } = useAuth()
  const [quizzes, setQuizzes] = useState<QuizSummary[] | null>(null)
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api
      .listQuizzes()
      .then((list) => active && setQuizzes(list))
      .catch((caught) => active && setError(caught instanceof Error ? caught.message : 'Failed to load'))
    api
      .health()
      .then((result) => active && setHealth(result))
      .catch(() => active && setHealth(null))
    return () => {
      active = false
    }
  }, [])

  if (!quizzes && !error) return <Spinner label="Loading your dashboard" />

  const all = quizzes ?? []
  const published = all.filter((quiz) => quiz.status === 'PUBLISHED')
  const totalAttempts = all.reduce((sum, quiz) => sum + quiz.attempt_count, 0)
  const approvedVariants = all.reduce((sum, quiz) => sum + quiz.assignable_variant_count, 0)

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">
            Welcome back, {profile?.name?.split(' ')[0] ?? 'there'}
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Create a quiz, review generated variants, then publish one link for your class.
          </p>
        </div>
        <Link to="/teacher/quizzes/new" className="btn-primary">
          Create quiz
        </Link>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="Total quizzes" value={all.length} />
        <Stat label="Published" value={published.length} />
        <Stat label="Approved variants" value={approvedVariants} />
        <Stat label="Total attempts" value={totalAttempts} />
        {/* Grading ships with the student exam engine, so there is no average
            score to show yet. An invented figure would be indistinguishable
            from a real one. */}
        <Stat label="Average score" value={null} hint="Phase 6" />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card p-6 lg:col-span-2">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold text-slate-900">Recent quizzes</h2>
            <Link to="/teacher/quizzes" className="text-sm text-brand-600 hover:underline">
              View all
            </Link>
          </div>

          {all.length === 0 ? (
            <p className="mt-4 text-sm text-slate-600">
              You have not created a quiz yet.{' '}
              <Link to="/teacher/quizzes/new" className="text-brand-600 hover:underline">
                Create one
              </Link>
              .
            </p>
          ) : (
            <ul className="mt-4 divide-y divide-slate-200">
              {all.slice(0, 5).map((quiz) => (
                <li key={quiz.id} className="flex items-center gap-3 py-3">
                  <Link
                    to={`/teacher/quizzes/${quiz.id}`}
                    className="min-w-0 flex-1 truncate text-sm font-medium text-slate-900 hover:text-brand-700"
                  >
                    {quiz.title}
                  </Link>
                  <span className="shrink-0 text-xs text-slate-500">
                    {quiz.question_count} q · {formatDuration(quiz.duration_seconds)}
                  </span>
                  <StatusBadge status={quiz.status} />
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card p-6">
          <h2 className="text-base font-semibold text-slate-900">System status</h2>
          <dl className="mt-4 space-y-3 text-sm">
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">API</dt>
              <dd>
                {health ? (
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                    {health.status}
                  </span>
                ) : (
                  <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
                    unreachable
                  </span>
                )}
              </dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Version</dt>
              <dd className="font-mono text-xs text-slate-700">{health?.version ?? '—'}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Server time</dt>
              <dd className="font-mono text-xs text-slate-700">
                {health ? new Date(health.server_time).toLocaleTimeString() : '—'}
              </dd>
            </div>
          </dl>
          <p className="mt-4 border-t border-slate-200 pt-3 text-xs leading-relaxed text-slate-500">
            Exam timing uses the server clock shown here, never the clock on a student&apos;s
            computer.
          </p>
        </section>
      </div>
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: number | null; hint?: string }) {
  return (
    <div className="card p-5">
      <p className="text-sm font-medium text-slate-600">{label}</p>
      <p
        className={`mt-2 text-3xl font-semibold tracking-tight ${
          value === null ? 'text-slate-300' : 'text-slate-900'
        }`}
      >
        {value === null ? '—' : value}
      </p>
      {hint && <p className="mt-1 text-xs text-slate-400">Available in {hint}</p>}
    </div>
  )
}
