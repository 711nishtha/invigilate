import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { ErrorNote, formatDuration } from '../../components/ui'
import { ApiError, api, type PublicQuiz } from '../../lib/api'
import { rememberAttemptToken } from '../../lib/attemptSession'
import { requestFullscreen } from '../../hooks/useIntegrityMonitor'

/**
 * Pre-exam screen for /q/:publicCode.
 *
 * The public code identifies a quiz; it authorizes nothing. This page shows
 * only what the server chooses to publish about the quiz — no question text,
 * no variant ids, no pool size.
 *
 * The rules are stated before the student starts, because being monitored is
 * something they are entitled to know about in advance.
 */
export function PublicQuizPage() {
  const { publicCode = '' } = useParams()
  const navigate = useNavigate()

  const [quiz, setQuiz] = useState<PublicQuiz | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [accepted, setAccepted] = useState(false)

  useEffect(() => {
    let active = true
    api
      .getPublicQuiz(publicCode)
      .then((result) => {
        if (!active) return
        setQuiz(result)
        // A monitored exam is started through the pre-exam check, where the
        // timer does not run while permissions are granted.
        if (result.requires_precheck) navigate(`/q/${publicCode}/check`, { replace: true })
      })
      .catch((caught) =>
        active &&
        setLoadError(
          caught instanceof ApiError && caught.status === 404
            ? 'No quiz found for this link. Check the code with your teacher.'
            : 'Could not load this quiz.',
        ),
      )
    return () => {
      active = false
    }
  }, [publicCode, navigate])

  async function handleStart(event: React.FormEvent) {
    event.preventDefault()
    if (!quiz) return
    if (quiz.requires_precheck) {
      // Belt and braces: the page normally redirects on load. The server
      // refuses this route for a proctored quiz regardless of what the client
      // does, so this only saves the student a pointless error.
      navigate(`/q/${publicCode}/check`, { replace: true })
      return
    }
    setBusy(true)
    setError(null)
    try {
      // Fullscreen is requested from inside the click handler because browsers
      // only grant it during a user gesture. Refusal is not fatal: leaving
      // fullscreen is recorded as a signal, not enforced as a lock.
      if (quiz.requires_fullscreen) await requestFullscreen()

      const started = await api.startAttempt(publicCode, {
        student_name: name.trim(),
        student_email: email.trim(),
      })
      // Shown once by the server; kept per-tab so a refresh can resume.
      rememberAttemptToken(started.attempt_id, started.attempt_token)
      navigate(`/attempt/${started.attempt_id}`, { replace: true })
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'ATTEMPT_ALREADY_EXISTS') {
        setError(
          'You have already started this quiz. Each student may attempt it once. ' +
            'If you were disconnected, reopen the tab where you started it.',
        )
      } else {
        setError(caught instanceof Error ? caught.message : 'Could not start the exam.')
      }
    } finally {
      setBusy(false)
    }
  }

  if (loadError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-100 p-6">
        <div className="card max-w-md p-8 text-center">
          <h1 className="text-lg font-semibold text-slate-900">Quiz unavailable</h1>
          <p className="mt-2 text-sm text-slate-600">{loadError}</p>
        </div>
      </div>
    )
  }

  if (!quiz) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Loading quiz" />
      </div>
    )
  }

  const rules = [
    `You have ${formatDuration(quiz.duration_seconds)} once you start. The exam is timed by the server — closing the tab or losing your connection does not pause the clock.`,
    `You will be given ${quiz.question_count} question${quiz.question_count === 1 ? '' : 's'}. Every student receives a different randomized paper.`,
    quiz.requires_fullscreen
      ? 'This exam runs in fullscreen on a desktop browser. Leaving fullscreen is recorded.'
      : 'Leaving the exam tab is recorded.',
    'Switching tabs, leaving fullscreen and clipboard use are recorded as integrity signals.',
    `If recorded signals exceed the limit your teacher set (${quiz.max_integrity_violations}), the exam is submitted automatically by the server.`,
    'You may attempt this quiz once.',
    quiz.shows_score_after_submission
      ? 'Your score is shown as soon as you submit.'
      : 'Your score is not shown after submitting; your teacher will release results.',
  ]

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-6">
      <div className="card w-full max-w-2xl p-8">
        <p className="font-mono text-xs uppercase tracking-widest text-slate-500">
          Quiz code {quiz.public_code}
        </p>
        <h1 className="mt-2 text-2xl font-semibold text-slate-900">{quiz.title}</h1>
        {quiz.owner_name && (
          <p className="mt-1 text-sm text-slate-600">Set by {quiz.owner_name}</p>
        )}
        {quiz.description && <p className="mt-3 text-sm text-slate-700">{quiz.description}</p>}

        <dl className="mt-6 grid grid-cols-3 gap-4 border-y border-slate-200 py-4 text-center">
          <div>
            <dt className="text-xs text-slate-500">Questions</dt>
            <dd className="mt-1 text-xl font-semibold text-slate-900">{quiz.question_count}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Time limit</dt>
            <dd className="mt-1 text-xl font-semibold text-slate-900">
              {formatDuration(quiz.duration_seconds)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Attempts</dt>
            <dd className="mt-1 text-xl font-semibold text-slate-900">1</dd>
          </div>
        </dl>

        <div className="mt-6 rounded-lg bg-slate-50 p-5 ring-1 ring-slate-200">
          <h2 className="text-sm font-semibold text-slate-900">Before you begin</h2>
          <ul className="mt-3 space-y-2 text-sm text-slate-700">
            {rules.map((rule) => (
              <li key={rule} className="flex gap-2">
                <span aria-hidden="true" className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" />
                {rule}
              </li>
            ))}
          </ul>
        </div>

        <ErrorNote error={error} onDismiss={() => setError(null)} />

        <form onSubmit={handleStart} className="mt-6 space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="name">
                Your full name
              </label>
              <input
                id="name"
                className="input"
                required
                maxLength={200}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="email">
                Your email
              </label>
              <input
                id="email"
                type="email"
                className="input"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
          </div>

          <label className="flex items-start gap-3 text-sm text-slate-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={accepted}
              onChange={(event) => setAccepted(event.target.checked)}
            />
            I understand the rules above and that activity in this browser tab is monitored
            during the exam.
          </label>

          <button
            type="submit"
            className="btn-primary w-full text-base"
            disabled={busy || !accepted || !name.trim() || !email.trim()}
          >
            {busy ? 'Starting…' : 'Start exam'}
          </button>
        </form>

        <p className="mt-6 text-xs leading-relaxed text-slate-500">
          Monitoring covers activity inside this browser tab only. It cannot see other
          applications, other devices, or the room you are in, and it does not make cheating
          impossible.
        </p>
      </div>
    </div>
  )
}
