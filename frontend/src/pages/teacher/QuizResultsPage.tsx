import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { EmptyState, ErrorNote, formatDuration } from '../../components/ui'
import {
  api,
  type AttemptRow,
  type Delivery,
  type QuizResults,
} from '../../lib/api'

/**
 * Results dashboard for one quiz.
 *
 * Every figure comes from the API, which computes it from the database. With
 * no attempts the counters are genuinely zero and the averages are null, so
 * this renders a real empty state rather than a fabricated one.
 */
export function QuizResultsPage() {
  const { id = '' } = useParams()
  const [results, setResults] = useState<QuizResults | null>(null)
  const [deliveries, setDeliveries] = useState<Delivery[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [body, history] = await Promise.all([
        api.getQuizResults(id),
        api.listDeliveries(id),
      ])
      setResults(body)
      setDeliveries(history)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load results.')
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  async function download() {
    if (!results) return
    setBusy('export')
    setError(null)
    try {
      await api.downloadExport(id, `${results.title.replace(/[^\w -]/g, '_')}-results.xlsx`)
      setNotice('Export downloaded.')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not generate the export.')
    } finally {
      setBusy(null)
    }
  }

  async function email() {
    setBusy('email')
    setError(null)
    setNotice(null)
    try {
      const outcome = await api.emailResults(id)
      // A delivery failure is reported in the body, not as an exception: the
      // results are already safe and the teacher can retry.
      if (outcome.sent) {
        setNotice(`Results emailed to ${outcome.delivery.recipient}.`)
      } else {
        setError(
          `Email failed (${outcome.error_code}): ${outcome.error_message ?? 'unknown error'}. ` +
            'Your results are unaffected — you can retry.',
        )
      }
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not send the email.')
    } finally {
      setBusy(null)
    }
  }

  if (!results) {
    return error ? <ErrorNote error={error} /> : <Spinner label="Loading results" />
  }

  const { stats } = results

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link to={`/teacher/quizzes/${id}`} className="text-sm text-brand-600 hover:underline">
            ← {results.title}
          </Link>
          <h1 className="mt-1 text-2xl font-semibold text-slate-900">Results</h1>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            className="btn-secondary"
            disabled={busy !== null || stats.attempt_count === 0}
            onClick={download}
          >
            {busy === 'export' ? 'Generating…' : 'Download Excel'}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={busy !== null || stats.attempt_count === 0}
            onClick={email}
          >
            {busy === 'email' ? 'Sending…' : 'Email results to me'}
          </button>
        </div>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />
      {notice && (
        <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800 ring-1 ring-emerald-200">
          {notice}
        </p>
      )}

      {stats.attempt_count === 0 ? (
        <EmptyState
          title="No attempts yet"
          description={
            results.public_code
              ? `Share the link /q/${results.public_code} with your class. Results appear here as students submit.`
              : 'Publish this quiz to share it with your class.'
          }
        />
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Attempts" value={stats.attempt_count} />
            <Stat
              label="Completed"
              value={stats.finalized_count}
              hint={
                stats.completion_rate !== null ? `${stats.completion_rate}% completion` : undefined
              }
            />
            <Stat
              label="Average score"
              value={stats.average_percentage !== null ? `${stats.average_percentage}%` : null}
            />
            <Stat
              label="Flagged"
              value={stats.flagged_count}
              tone={stats.flagged_count > 0 ? 'warn' : undefined}
            />
          </div>

          <div className="grid gap-6 lg:grid-cols-3">
            <section className="card p-6">
              <h2 className="text-base font-semibold text-slate-900">How exams ended</h2>
              <dl className="mt-4 space-y-2 text-sm">
                <Row label="Submitted by student" value={stats.submitted_count} />
                <Row label="Auto-submitted (integrity)" value={stats.auto_submitted_count} />
                <Row label="Expired (time)" value={stats.expired_count} />
                <Row label="Still in progress" value={stats.in_progress_count} />
              </dl>
            </section>

            <section className="card p-6">
              <h2 className="text-base font-semibold text-slate-900">Scores</h2>
              {stats.average_percentage === null ? (
                <p className="mt-4 text-sm text-slate-600">
                  No completed attempts yet, so there is nothing to average.
                </p>
              ) : (
                <dl className="mt-4 space-y-2 text-sm">
                  <Row label="Average" value={`${stats.average_percentage}%`} />
                  <Row label="Highest" value={`${stats.highest_percentage}%`} />
                  <Row label="Lowest" value={`${stats.lowest_percentage}%`} />
                  <Row
                    label="Marks available"
                    value={stats.max_score !== null ? stats.max_score : '—'}
                  />
                </dl>
              )}
            </section>

            <section className="card p-6">
              <h2 className="text-base font-semibold text-slate-900">Integrity</h2>
              <dl className="mt-4 space-y-2 text-sm">
                <Row label="Clean" value={stats.clean_count} />
                <Row label="Warning" value={stats.warning_count} />
                <Row label="Flagged" value={stats.flagged_count} />
                <Row label="Recorded events" value={stats.integrity_event_count} />
              </dl>
              <p className="mt-4 border-t border-slate-200 pt-3 text-xs leading-relaxed text-slate-500">
                Integrity signals are browser-level observations, not proof of misconduct.
              </p>
            </section>
          </div>

          {stats.score_distribution.length > 0 && (
            <section className="card p-6">
              <h2 className="text-base font-semibold text-slate-900">Score distribution</h2>
              <div className="mt-4 space-y-2">
                {stats.score_distribution.map((band) => {
                  const widest = Math.max(...stats.score_distribution.map((b) => b.count), 1)
                  return (
                    <div key={band.band} className="flex items-center gap-3 text-sm">
                      <span className="w-20 shrink-0 text-slate-600">{band.band}</span>
                      <div className="h-5 flex-1 overflow-hidden rounded bg-slate-100">
                        <div
                          className="h-full rounded bg-brand-500"
                          style={{ width: `${(band.count / widest) * 100}%` }}
                        />
                      </div>
                      <span className="w-8 shrink-0 text-right font-medium text-slate-900">
                        {band.count}
                      </span>
                    </div>
                  )
                })}
              </div>
            </section>
          )}

          <section className="card overflow-hidden">
            <h2 className="border-b border-slate-200 px-6 py-4 text-base font-semibold text-slate-900">
              Attempts
            </h2>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-6 py-3">Student</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Score</th>
                    <th className="px-4 py-3">Answered</th>
                    <th className="px-4 py-3">Duration</th>
                    <th className="px-4 py-3">Integrity</th>
                    <th className="px-4 py-3" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {results.attempts.map((attempt) => (
                    <AttemptTableRow key={attempt.attempt_id} attempt={attempt} />
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}

      {deliveries.length > 0 && (
        <section className="card p-6">
          <h2 className="text-base font-semibold text-slate-900">Email delivery</h2>
          <ul className="mt-4 divide-y divide-slate-200">
            {deliveries.map((delivery) => (
              <li key={delivery.id} className="flex flex-wrap items-center gap-3 py-3 text-sm">
                <span
                  className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    delivery.status === 'SENT'
                      ? 'bg-emerald-50 text-emerald-700'
                      : delivery.status === 'FAILED'
                        ? 'bg-red-50 text-red-700'
                        : 'bg-slate-100 text-slate-700'
                  }`}
                >
                  {delivery.status.toLowerCase()}
                </span>
                <span className="text-slate-700">{delivery.recipient}</span>
                <span className="text-xs text-slate-500">
                  {delivery.attempt_count} attempt{delivery.attempt_count === 1 ? '' : 's'} ·{' '}
                  {new Date(delivery.created_at).toLocaleString()}
                </span>
                {delivery.error_code && (
                  <span className="text-xs text-red-700">{delivery.error_code}</span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-3 border-t border-slate-200 pt-3 text-xs text-slate-500">
            A failed email never affects stored results. Use “Email results to me” to retry.
          </p>
        </section>
      )}
    </div>
  )
}

function AttemptTableRow({ attempt }: { attempt: AttemptRow }) {
  return (
    <tr className="hover:bg-slate-50">
      <td className="px-6 py-3">
        <div className="font-medium text-slate-900">{attempt.student_name}</div>
        <div className="text-xs text-slate-500">{attempt.student_email}</div>
      </td>
      <td className="px-4 py-3">
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
            attempt.status === 'SUBMITTED'
              ? 'bg-emerald-50 text-emerald-700'
              : attempt.status === 'AUTO_SUBMITTED'
                ? 'bg-red-50 text-red-700'
                : attempt.status === 'TIME_EXPIRED'
                  ? 'bg-amber-50 text-amber-800'
                  : 'bg-slate-100 text-slate-700'
          }`}
        >
          {attempt.status.replace('_', ' ').toLowerCase()}
        </span>
      </td>
      <td className="px-4 py-3">
        {attempt.percentage !== null ? (
          <span className="font-medium text-slate-900">
            {attempt.score}/{attempt.max_score}{' '}
            <span className="text-xs text-slate-500">({attempt.percentage}%)</span>
          </span>
        ) : (
          <span className="text-slate-400">—</span>
        )}
      </td>
      <td className="px-4 py-3 text-slate-700">
        {attempt.answered_count}/{attempt.question_count}
      </td>
      <td className="px-4 py-3 text-slate-700">
        {attempt.duration_seconds !== null ? formatDuration(attempt.duration_seconds) : '—'}
      </td>
      <td className="px-4 py-3">
        <span
          className={
            attempt.integrity_status === 'CLEAN'
              ? 'text-slate-500'
              : attempt.integrity_status === 'WARNING'
                ? 'text-amber-700'
                : 'font-medium text-red-700'
          }
        >
          {attempt.integrity_status.toLowerCase()}
          {attempt.violation_count > 0 && ` (${attempt.violation_count})`}
        </span>
      </td>
      <td className="px-4 py-3 text-right">
        <Link
          to={`/teacher/attempts/${attempt.attempt_id}`}
          className="text-sm text-brand-600 hover:underline"
        >
          Inspect
        </Link>
      </td>
    </tr>
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: number | string | null
  hint?: string
  tone?: 'warn'
}) {
  return (
    <div className="card p-5">
      <p className="text-sm font-medium text-slate-600">{label}</p>
      <p
        className={`mt-2 text-3xl font-semibold tracking-tight ${
          value === null ? 'text-slate-300' : tone === 'warn' ? 'text-danger' : 'text-slate-900'
        }`}
      >
        {value === null ? '—' : value}
      </p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  )
}

function Row({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-slate-600">{label}</dt>
      <dd className="font-medium text-slate-900">{value}</dd>
    </div>
  )
}
