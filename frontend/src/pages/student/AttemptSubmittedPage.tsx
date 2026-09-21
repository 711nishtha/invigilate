import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { api, type AttemptResult } from '../../lib/api'
import { recallAttemptToken } from '../../lib/attemptSession'

/**
 * Post-submission screen.
 *
 * Whether a score appears is the server's decision: when the quiz does not set
 * `show_score_after_submission`, the numbers are absent from the payload
 * entirely rather than hidden here. There is nothing in the response for a
 * student with DevTools to uncover.
 */
export function AttemptSubmittedPage() {
  const { attemptId = '' } = useParams()
  const token = recallAttemptToken(attemptId)

  const [result, setResult] = useState<AttemptResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!token) {
      setError('This result is not available in this tab.')
      return
    }
    let active = true
    api
      .getAttemptResult(attemptId, token)
      .then((value) => active && setResult(value))
      .catch(() => active && setError('Could not load your result.'))
    return () => {
      active = false
    }
  }, [attemptId, token])

  if (error) {
    return (
      <Shell title="Exam submitted">
        <p className="mt-2 text-sm text-slate-600">
          Your answers were recorded. {error}
        </p>
      </Shell>
    )
  }

  if (!result) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Loading your result" />
      </div>
    )
  }

  const heading =
    result.status === 'AUTO_SUBMITTED'
      ? 'Exam submitted automatically'
      : result.status === 'TIME_EXPIRED'
        ? 'Time expired'
        : 'Exam submitted'

  const explanation =
    result.status === 'AUTO_SUBMITTED'
      ? 'Recorded activity outside the exam exceeded the limit your teacher set, so the exam was submitted for you.'
      : result.status === 'TIME_EXPIRED'
        ? 'Your time ran out, so the exam was submitted automatically. The answers you saved were kept.'
        : 'Your answers have been recorded. You may close this window.'

  return (
    <Shell title={heading}>
      <p className="mt-2 text-sm text-slate-600">{explanation}</p>

      <dl className="mt-6 grid grid-cols-2 gap-4 border-y border-slate-200 py-4 text-center">
        <div>
          <dt className="text-xs text-slate-500">Questions answered</dt>
          <dd className="mt-1 text-xl font-semibold text-slate-900">
            {result.answered_count} / {result.question_count}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Submitted</dt>
          <dd className="mt-1 text-sm font-medium text-slate-900">
            {result.submitted_at ? new Date(result.submitted_at).toLocaleTimeString() : '—'}
          </dd>
        </div>
      </dl>

      {result.score_visible ? (
        <div className="mt-6 rounded-lg bg-emerald-50 p-6 text-center ring-1 ring-emerald-200">
          <p className="text-xs uppercase tracking-wide text-emerald-800">Your score</p>
          <p className="mt-1 text-4xl font-semibold text-emerald-900">
            {result.score} <span className="text-2xl opacity-60">/ {result.max_score}</span>
          </p>
          {result.percentage !== null && (
            <p className="mt-1 text-sm text-emerald-800">{result.percentage}%</p>
          )}
        </div>
      ) : (
        <p className="mt-6 rounded-lg bg-slate-50 p-4 text-center text-sm text-slate-600 ring-1 ring-slate-200">
          Your teacher has chosen not to show scores immediately. They will release results
          separately.
        </p>
      )}

      {result.integrity_status !== 'CLEAN' && (
        <p className="mt-4 rounded-lg bg-amber-50 p-3 text-xs text-amber-900 ring-1 ring-amber-200">
          {result.violation_count} integrity signal
          {result.violation_count === 1 ? '' : 's'} were recorded during your exam and shared with
          your teacher. Recorded signals are not on their own a finding of misconduct.
        </p>
      )}

      <p className="mt-6 font-mono text-xs text-slate-400">{result.attempt_id}</p>
    </Shell>
  )
}

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-6">
      <div className="card w-full max-w-lg p-8 text-center">
        <div
          aria-hidden="true"
          className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-50 text-xl text-emerald-600"
        >
          ✓
        </div>
        <h1 className="mt-4 text-xl font-semibold text-slate-900">{title}</h1>
        {children}
      </div>
    </div>
  )
}
