import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ProctoringPanel } from '../../components/ProctoringPanel'
import { Spinner } from '../../components/Spinner'
import { ErrorNote, formatDuration } from '../../components/ui'
import { api, type AttemptDetail } from '../../lib/api'

/**
 * One attempt, as the teacher who owns the quiz sees it.
 *
 * This is the only screen that shows a student's answers beside the correct
 * ones — that is what marking is. Access is scoped server-side: an attempt on
 * someone else's quiz returns 404, so changing the id in the URL achieves
 * nothing.
 */
export function AttemptDetailPage() {
  const { attemptId = '' } = useParams()
  const [detail, setDetail] = useState<AttemptDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api
      .getAttemptDetail(attemptId)
      .then((value) => active && setDetail(value))
      .catch(
        (caught) =>
          active && setError(caught instanceof Error ? caught.message : 'Could not load attempt.'),
      )
    return () => {
      active = false
    }
  }, [attemptId])

  if (error) return <ErrorNote error={error} />
  if (!detail) return <Spinner label="Loading attempt" />

  return (
    <div className="space-y-6">
      <div>
        <Link
          to={`/teacher/quizzes/${detail.quiz_id}/results`}
          className="text-sm text-brand-600 hover:underline"
        >
          ← {detail.quiz_title} results
        </Link>
        <h1 className="mt-1 text-2xl font-semibold text-slate-900">{detail.student_name}</h1>
        <p className="text-sm text-slate-600">{detail.student_email}</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Fact label="Outcome" value={detail.status.replace('_', ' ').toLowerCase()} />
        <Fact
          label="Score"
          value={
            detail.percentage !== null
              ? `${detail.score}/${detail.max_score} (${detail.percentage}%)`
              : 'not graded'
          }
        />
        <Fact
          label="Duration"
          value={
            detail.duration_seconds !== null ? formatDuration(detail.duration_seconds) : '—'
          }
        />
        <Fact
          label="Integrity"
          value={`${detail.integrity_status.toLowerCase()} (${detail.violation_count})`}
          tone={detail.integrity_status === 'FLAGGED' ? 'warn' : undefined}
        />
      </div>

      <section className="card p-6">
        <h2 className="text-base font-semibold text-slate-900">Timing</h2>
        <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
          <div className="flex justify-between gap-4">
            <dt className="text-slate-600">Started</dt>
            <dd className="text-slate-900">{new Date(detail.started_at).toLocaleString()}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-slate-600">Finalized</dt>
            <dd className="text-slate-900">
              {detail.submitted_at ? new Date(detail.submitted_at).toLocaleString() : '—'}
            </dd>
          </div>
        </dl>
      </section>

      <section className="space-y-4">
        <h2 className="text-base font-semibold text-slate-900">
          The exact paper this student received
        </h2>
        {detail.questions.map((question) => (
          <article key={question.position} className="card p-6">
            <div className="flex items-start justify-between gap-4">
              <p className="text-sm font-medium text-slate-900">
                {question.position + 1}. {question.question_text}
              </p>
              <span
                className={`shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium ${
                  question.is_correct === true
                    ? 'bg-emerald-50 text-emerald-700'
                    : question.is_correct === false
                      ? 'bg-red-50 text-red-700'
                      : 'bg-slate-100 text-slate-600'
                }`}
              >
                {question.is_correct === true
                  ? 'correct'
                  : question.is_correct === false
                    ? 'incorrect'
                    : question.selected_answer
                      ? 'not graded'
                      : 'unanswered'}
              </span>
            </div>

            <p className="mt-1 text-xs text-slate-500">
              {question.topic ?? 'no topic'} · {question.difficulty ?? 'no difficulty'} ·{' '}
              {question.question_type}
            </p>

            {question.options && (
              <ul className="mt-4 space-y-1.5">
                {question.options.map((option, index) => {
                  const chosen = option === question.selected_answer
                  const right = option === question.correct_answer
                  return (
                    <li
                      key={`${question.position}-${index}`}
                      className={`flex items-center gap-2 rounded px-2 py-1 text-sm ${
                        right
                          ? 'bg-emerald-50 font-medium text-emerald-900'
                          : chosen
                            ? 'bg-red-50 text-red-900'
                            : 'text-slate-700'
                      }`}
                    >
                      <span className="text-slate-400">{String.fromCharCode(65 + index)}.</span>
                      {option}
                      {chosen && <span className="ml-auto text-xs">student chose</span>}
                      {right && !chosen && <span className="ml-auto text-xs">correct</span>}
                    </li>
                  )
                })}
              </ul>
            )}

            {!question.options && (
              <dl className="mt-4 space-y-1 text-sm">
                <div className="flex gap-2">
                  <dt className="text-slate-600">Student answered:</dt>
                  <dd className="font-medium text-slate-900">
                    {question.selected_answer ?? 'nothing'}
                  </dd>
                </div>
                <div className="flex gap-2">
                  <dt className="text-slate-600">Correct answer:</dt>
                  <dd className="font-medium text-emerald-800">
                    {question.correct_answer ?? '—'}
                  </dd>
                </div>
              </dl>
            )}

            <p className="mt-3 border-t border-slate-200 pt-2 text-xs text-slate-400">
              Original question: {question.original_question}
            </p>
          </article>
        ))}
      </section>

      <section className="card overflow-hidden">
        <h2 className="border-b border-slate-200 px-6 py-4 text-base font-semibold text-slate-900">
          Integrity timeline
        </h2>
        {detail.integrity_events.length === 0 ? (
          <p className="px-6 py-6 text-sm text-slate-600">
            No integrity signals were recorded during this attempt.
          </p>
        ) : (
          <ul className="divide-y divide-slate-200">
            {detail.integrity_events.map((event, index) => (
              <li key={index} className="flex flex-wrap items-center gap-3 px-6 py-3 text-sm">
                <span className="font-mono text-xs text-slate-500">
                  {new Date(event.occurred_at).toLocaleTimeString()}
                </span>
                <span className="font-medium text-slate-900">{event.event_type}</span>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs ${
                    event.severity > 0 ? 'bg-amber-50 text-amber-800' : 'bg-slate-100 text-slate-600'
                  }`}
                >
                  severity {event.severity}
                </span>
                <span className="text-xs text-slate-500">{event.action_taken}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="border-t border-slate-200 px-6 py-3 text-xs leading-relaxed text-slate-500">
          These are browser-level signals recorded during the exam. They are not on their own a
          finding of misconduct, and they cannot detect activity outside this browser tab.
        </p>
      </section>
      {/* The proctoring record sits under the marking, because it is
          context for a decision about a person, not part of the mark. */}
      <ProctoringPanel attemptId={attemptId} />

    </div>
  )
}

function Fact({
  label,
  value,
  tone,
}: {
  label: string
  value: string | number
  tone?: 'warn'
}) {
  return (
    <div className="card p-5">
      <p className="text-sm font-medium text-slate-600">{label}</p>
      <p
        className={`mt-2 text-lg font-semibold ${
          tone === 'warn' ? 'text-danger' : 'text-slate-900'
        }`}
      >
        {value}
      </p>
    </div>
  )
}
