import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { CopyableCode, ErrorNote, StatusBadge, formatDuration } from '../../components/ui'
import { ApiError, api, type QuizDetail } from '../../lib/api'

export function QuizDetailPage() {
  const { id = '' } = useParams()
  const [quiz, setQuiz] = useState<QuizDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setQuiz(await api.getQuiz(id))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load the quiz.')
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  async function act(action: () => Promise<QuizDetail>) {
    setBusy(true)
    setError(null)
    try {
      setQuiz(await action())
    } catch (caught) {
      // The server explains exactly why a publish was refused; surface that
      // rather than a generic failure.
      if (caught instanceof ApiError && caught.code === 'QUIZ_NOT_READY') {
        setError('This quiz is not ready to publish. See the blockers below.')
        await load()
      } else {
        setError(caught instanceof Error ? caught.message : 'That action failed.')
      }
    } finally {
      setBusy(false)
    }
  }

  if (!quiz) {
    return error ? <ErrorNote error={error} /> : <Spinner label="Loading quiz" />
  }

  const { readiness } = quiz

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold text-slate-900">{quiz.title}</h1>
            <StatusBadge status={quiz.status} />
          </div>
          {quiz.description && <p className="mt-1 text-sm text-slate-600">{quiz.description}</p>}
        </div>
        <div className="flex gap-2">
          <Link to={`/teacher/quizzes/${quiz.id}/questions`} className="btn-secondary">
            Questions
          </Link>
          <Link to={`/teacher/quizzes/${quiz.id}/results`} className="btn-secondary">
            Results
          </Link>
          {quiz.status === 'DRAFT' && (
            <button
              type="button"
              className="btn-primary"
              disabled={busy || !readiness.publishable}
              title={readiness.publishable ? undefined : 'Resolve the blockers first'}
              onClick={() => act(() => api.publishQuiz(quiz.id))}
            >
              {busy ? 'Working…' : 'Publish'}
            </button>
          )}
          {quiz.status === 'PUBLISHED' && (
            <>
              <button
                type="button"
                className="btn-secondary"
                disabled={busy}
                onClick={() => act(() => api.unpublishQuiz(quiz.id))}
              >
                Unpublish
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={busy}
                onClick={() => act(() => api.closeQuiz(quiz.id))}
              >
                Close
              </button>
            </>
          )}
        </div>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />

      {quiz.status === 'PUBLISHED' && quiz.public_code && (
        <div className="card flex flex-wrap items-center justify-between gap-4 border-emerald-200 bg-emerald-50 p-5">
          <div>
            <p className="text-sm font-medium text-emerald-900">This quiz is live</p>
            <p className="mt-1 text-xs text-emerald-800">
              Share this one link with your whole class. Each student receives a different exam.
            </p>
          </div>
          <CopyableCode code={quiz.public_code} />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card p-6 lg:col-span-2">
          <h2 className="text-base font-semibold text-slate-900">
            {readiness.publishable ? 'Ready to publish' : 'Not ready to publish'}
          </h2>

          {readiness.publishable ? (
            <p className="mt-2 text-sm text-slate-600">
              {readiness.ready_family_count} question
              {readiness.ready_family_count === 1 ? '' : 's'} with approved variants.
              {readiness.distinct_paper_estimate > 1 && (
                <>
                  {' '}
                  This pool can produce about{' '}
                  <strong>{readiness.distinct_paper_estimate.toLocaleString()}</strong> distinct
                  papers before option order is shuffled.
                </>
              )}
            </p>
          ) : (
            <>
              <p className="mt-2 text-sm text-slate-600">
                Publishing is blocked until each of these is resolved:
              </p>
              <ul className="mt-4 space-y-3">
                {readiness.blockers.map((blocker, index) => (
                  <li
                    key={`${blocker.code}-${index}`}
                    className="flex gap-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 ring-1 ring-amber-200"
                  >
                    <span aria-hidden="true">!</span>
                    <span>
                      {blocker.message}
                      {blocker.family_id && (
                        <>
                          {' '}
                          <Link
                            to={`/teacher/quizzes/${quiz.id}/variants?family=${blocker.family_id}`}
                            className="font-medium underline"
                          >
                            Review that question
                          </Link>
                        </>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>

        <section className="card p-6">
          <h2 className="text-base font-semibold text-slate-900">Settings</h2>
          <dl className="mt-4 space-y-2.5 text-sm">
            {[
              ['Questions per student', String(quiz.question_count)],
              ['Duration', formatDuration(quiz.duration_seconds)],
              ['Security level', quiz.security_level.toLowerCase()],
              ['Violation budget', String(quiz.max_integrity_violations)],
              ['Backtracking', quiz.allow_backtracking ? 'allowed' : 'not allowed'],
              ['Randomization', quiz.randomization_enabled ? 'on' : 'off'],
              ['Score shown after submit', quiz.show_score_after_submission ? 'yes' : 'no'],
              ['Questions authored', String(readiness.family_count)],
              ['Approved variants', String(readiness.assignable_variant_count)],
            ].map(([term, value]) => (
              <div key={term} className="flex justify-between gap-4">
                <dt className="text-slate-600">{term}</dt>
                <dd className="text-right font-medium text-slate-900">{value}</dd>
              </div>
            ))}
          </dl>
          {quiz.status === 'PUBLISHED' && (
            <p className="mt-4 border-t border-slate-200 pt-3 text-xs text-slate-500">
              Exam settings are locked while the quiz is published, so a live exam cannot change
              underneath a student. Unpublish to edit them.
            </p>
          )}
        </section>
      </div>
    </div>
  )
}
