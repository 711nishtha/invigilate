import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { ErrorNote } from '../../components/ui'
import { useIntegrityMonitor } from '../../hooks/useIntegrityMonitor'
import {
  ApiError,
  api,
  type AttemptState,
  type StudentQuestion,
} from '../../lib/api'
import { recallAttemptToken } from '../../lib/attemptSession'

/**
 * The exam runner.
 *
 * Everything that matters is the server's. This component renders the paper it
 * is given, reports signals, and displays a countdown — but the countdown is a
 * *display* derived from the server's `seconds_remaining`, the paper comes
 * frozen from the server, and submission is the server's decision to make.
 *
 * Resilience: a refresh reloads the same attempt and the same paper. A
 * temporary network failure leaves answers queued and retried; it does not
 * pause the clock, because the clock is not here.
 */

/** How often the client resynchronizes against the server's clock. */
const RESYNC_MS = 20_000

export function AttemptPage() {
  const { attemptId = '' } = useParams()
  const navigate = useNavigate()
  const token = recallAttemptToken(attemptId)

  const [state, setState] = useState<AttemptState | null>(null)
  const [questions, setQuestions] = useState<StudentQuestion[] | null>(null)
  const [current, setCurrent] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [fatal, setFatal] = useState<string | null>(null)
  const [offline, setOffline] = useState(false)
  const [saving, setSaving] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [warning, setWarning] = useState<string | null>(null)

  // Local countdown seconds. Seeded and repeatedly corrected from the server;
  // never used to decide anything, only to render.
  const [remaining, setRemaining] = useState<number | null>(null)
  // Answers that failed to reach the server, retried on reconnect.
  const pending = useRef<Map<string, Record<string, unknown>>>(new Map())

  const finish = useCallback(() => navigate(`/attempt/${attemptId}/submitted`, { replace: true }), [
    attemptId,
    navigate,
  ])

  const load = useCallback(async () => {
    if (!token) {
      setFatal('This exam session is not available in this tab. Reopen the tab where you started it.')
      return
    }
    try {
      const [nextState, paper] = await Promise.all([
        api.getAttempt(attemptId, token),
        api.getAttemptQuestions(attemptId, token),
      ])
      setState(nextState)
      setQuestions(paper.questions)
      setRemaining(nextState.seconds_remaining)
      setOffline(false)
      if (nextState.is_terminal) finish()
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'NETWORK_ERROR') {
        setOffline(true)
      } else if (caught instanceof ApiError && caught.status === 404) {
        setFatal('This exam session could not be found.')
      } else {
        setError(caught instanceof Error ? caught.message : 'Could not load the exam.')
      }
    }
  }, [attemptId, token, finish])

  useEffect(() => {
    void load()
  }, [load])

  // Display countdown. Decrements locally between resyncs purely so the number
  // moves; the server's value overwrites it on every resync.
  useEffect(() => {
    if (remaining === null || state?.is_terminal) return
    const timer = window.setInterval(() => {
      setRemaining((value) => (value === null ? null : Math.max(0, value - 1)))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [remaining === null, state?.is_terminal])

  // Resync against the server. This is also what finalizes an expired attempt:
  // the server closes it and reports the terminal state back.
  useEffect(() => {
    if (!token || state?.is_terminal) return
    const timer = window.setInterval(async () => {
      try {
        const next = await api.getAttempt(attemptId, token)
        setState(next)
        setRemaining(next.seconds_remaining)
        setOffline(false)
        await flushPending()
        if (next.is_terminal) finish()
      } catch (caught) {
        if (caught instanceof ApiError && caught.code === 'NETWORK_ERROR') setOffline(true)
      }
    }, RESYNC_MS)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attemptId, token, state?.is_terminal, finish])

  const flushPending = useCallback(async () => {
    if (!token || pending.current.size === 0) return
    for (const [questionId, answer] of Array.from(pending.current.entries())) {
      try {
        await api.saveAnswer(attemptId, token, {
          attempt_question_id: questionId,
          selected_answer: answer,
        })
        pending.current.delete(questionId)
      } catch {
        // Still unreachable, or the attempt is finished. Either way there is
        // nothing useful to do here; the next resync will discover which.
        break
      }
    }
  }, [attemptId, token])

  // Called for its side effects: it attaches the browser listeners.
  useIntegrityMonitor({
    attemptId,
    attemptToken: token ?? '',
    active: Boolean(token) && Boolean(state) && !state?.is_terminal,
    onAutoSubmit: () => {
      // The server has already finalized. We are being told, not asked.
      finish()
    },
    onViolation: (total, max) => {
      setState((previous) => (previous ? { ...previous, violation_count: total } : previous))
      setWarning(
        total > max
          ? 'Your exam has been submitted automatically.'
          : `Activity outside the exam was recorded (${total} of ${max} allowed).`,
      )
    },
  })

  async function answer(question: StudentQuestion, selected: Record<string, unknown>) {
    setQuestions((previous) =>
      previous?.map((item) =>
        item.attempt_question_id === question.attempt_question_id
          ? { ...item, selected_answer: selected }
          : item,
      ) ?? previous,
    )
    if (!token) return

    setSaving(true)
    try {
      await api.saveAnswer(attemptId, token, {
        attempt_question_id: question.attempt_question_id,
        selected_answer: selected,
      })
      pending.current.delete(question.attempt_question_id)
      setOffline(false)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'NETWORK_ERROR') {
        // Queued, not lost. Retried on the next resync.
        pending.current.set(question.attempt_question_id, selected)
        setOffline(true)
      } else if (caught instanceof ApiError && caught.status === 409) {
        void load()
      } else {
        setError(caught instanceof Error ? caught.message : 'Could not save that answer.')
      }
    } finally {
      setSaving(false)
    }
  }

  async function submit() {
    if (!token) return
    setSubmitting(true)
    setError(null)
    try {
      await flushPending()
      await api.submitAttempt(attemptId, token)
      finish()
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'NETWORK_ERROR') {
        setOffline(true)
        setError('Could not reach the server. Check your connection and try again.')
      } else {
        setError(caught instanceof Error ? caught.message : 'Could not submit.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (fatal) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-100 p-6">
        <div className="card max-w-md p-8 text-center">
          <h1 className="text-lg font-semibold text-slate-900">Exam unavailable</h1>
          <p className="mt-2 text-sm text-slate-600">{fatal}</p>
        </div>
      </div>
    )
  }

  if (!state || !questions) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Loading your exam" />
      </div>
    )
  }

  const question = questions[current]
  const answeredCount = questions.filter((item) => item.selected_answer).length
  const low = remaining !== null && remaining <= 60

  return (
    <div className="exam-locked flex min-h-screen flex-col bg-white">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 px-6 py-3">
          <div>
            <p className="text-sm font-medium text-slate-900">
              Question {current + 1} of {questions.length}
            </p>
            <p className="text-xs text-slate-500">
              {answeredCount} answered
              {saving && ' · saving…'}
              {pending.current.size > 0 && ' · queued'}
            </p>
          </div>

          <div className="flex items-center gap-3">
            <span
              className={`rounded-full px-3 py-1 text-xs font-medium ${
                state.integrity_status === 'CLEAN'
                  ? 'bg-emerald-50 text-emerald-700'
                  : state.integrity_status === 'WARNING'
                    ? 'bg-amber-50 text-amber-800'
                    : 'bg-red-50 text-red-700'
              }`}
            >
              {state.violation_count} / {state.max_integrity_violations} recorded
            </span>
            <span
              aria-live="polite"
              className={`font-mono text-lg font-semibold tabular-nums ${
                low ? 'text-danger' : 'text-slate-900'
              }`}
            >
              {remaining === null
                ? '--:--'
                : `${String(Math.floor(remaining / 60)).padStart(2, '0')}:${String(remaining % 60).padStart(2, '0')}`}
            </span>
          </div>
        </div>

        {offline && (
          <p className="bg-amber-50 px-6 py-2 text-center text-xs text-amber-900">
            You appear to be offline. Your answers are saved locally and will be sent when the
            connection returns — but the exam clock keeps running on the server.
          </p>
        )}
        {warning && (
          <p className="bg-amber-100 px-6 py-2 text-center text-xs font-medium text-amber-900">
            {warning}
          </p>
        )}
      </header>

      <main className="mx-auto w-full max-w-4xl flex-1 px-6 py-8">
        <ErrorNote error={error} onDismiss={() => setError(null)} />

        {question && (
          <article className="card p-8">
            <p className="text-base font-medium leading-relaxed text-slate-900">
              {question.question_text}
            </p>

            {question.options ? (
              <ul className="mt-6 space-y-3">
                {question.options.map((option, index) => {
                  const selected =
                    (question.selected_answer as { displayed_index?: number } | null)
                      ?.displayed_index === index
                  return (
                    <li key={`${question.attempt_question_id}-${index}`}>
                      <button
                        type="button"
                        onClick={() => void answer(question, { displayed_index: index })}
                        className={`flex w-full items-center gap-3 rounded-lg border p-4 text-left text-sm transition-colors ${
                          selected
                            ? 'border-brand-500 bg-brand-50 text-brand-900'
                            : 'border-slate-200 hover:bg-slate-50'
                        }`}
                      >
                        <span
                          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                            selected ? 'bg-brand-600 text-white' : 'bg-slate-100 text-slate-600'
                          }`}
                        >
                          {String.fromCharCode(65 + index)}
                        </span>
                        {option}
                      </button>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <div className="mt-6">
                <label className="label" htmlFor="numeric">
                  Your answer
                </label>
                <input
                  id="numeric"
                  className="input max-w-xs"
                  inputMode="decimal"
                  defaultValue={
                    ((question.selected_answer as { value?: string } | null)?.value as string) ?? ''
                  }
                  onBlur={(event) =>
                    event.target.value.trim() &&
                    void answer(question, { value: event.target.value.trim() })
                  }
                />
                <p className="mt-1 text-xs text-slate-500">
                  Your answer is saved when you move away from the box.
                </p>
              </div>
            )}
          </article>
        )}

        <div className="mt-6 flex items-center justify-between gap-3">
          <button
            type="button"
            className="btn-secondary"
            disabled={!state.allow_backtracking || current === 0}
            onClick={() => setCurrent((index) => Math.max(0, index - 1))}
          >
            Previous
          </button>

          <div className="flex flex-wrap justify-center gap-1.5">
            {questions.map((item, index) => (
              <button
                key={item.attempt_question_id}
                type="button"
                aria-label={`Question ${index + 1}`}
                disabled={!state.allow_backtracking && index < current}
                onClick={() => setCurrent(index)}
                className={`h-8 w-8 rounded text-xs font-medium transition-colors ${
                  index === current
                    ? 'bg-brand-600 text-white'
                    : item.selected_answer
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                }`}
              >
                {index + 1}
              </button>
            ))}
          </div>

          {current < questions.length - 1 ? (
            <button
              type="button"
              className="btn-primary"
              onClick={() => setCurrent((index) => Math.min(questions.length - 1, index + 1))}
            >
              Next
            </button>
          ) : (
            <button
              type="button"
              className="btn-primary"
              disabled={submitting}
              onClick={() => void submit()}
            >
              {submitting ? 'Submitting…' : 'Submit exam'}
            </button>
          )}
        </div>

        {current === questions.length - 1 && answeredCount < questions.length && (
          <p className="mt-4 text-center text-sm text-amber-800">
            {questions.length - answeredCount} question
            {questions.length - answeredCount === 1 ? '' : 's'} unanswered. Unanswered questions
            score zero.
          </p>
        )}
      </main>

      <footer className="border-t border-slate-200 px-6 py-3">
        <p className="mx-auto max-w-4xl text-center text-xs text-slate-400">
          Timing, question assignment and marking are handled by the server. Activity in this
          tab is monitored, as described in the rules you accepted before starting.
        </p>
      </footer>
    </div>
  )
}
