import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { ErrorNote, formatDuration } from '../../components/ui'
import {
  ApiError,
  api,
  type CapabilityVerdict,
  type PrecheckSession,
  type PublicQuiz,
} from '../../lib/api'
import { rememberAttemptToken } from '../../lib/attemptSession'
import {
  CAPABILITY_LABELS,
  FacePresenceDetector,
  MEDIA_FAILURE_LABELS,
  captureStill,
  detectCapabilities,
  enterFullscreen,
  isFullscreen,
  isLive,
  requestCamera,
  requestMicrophone,
  requestScreenShare,
  stopStream,
} from '../../lib/proctoring'

/**
 * The pre-exam security check, at /q/:publicCode/check.
 *
 * Three things about this page are worth stating, because they are the design
 * rather than the styling.
 *
 * **No clock is running.** The attempt does not exist yet. A student can spend
 * ten minutes finding a quiet room, fighting with a webcam driver or reading
 * the notice, and lose none of their exam time. The timer starts when the
 * server authorizes the start, which is the last thing this page does.
 *
 * **The green ticks are not the decision.** Everything here reports to the
 * server, which evaluates its own policy and answers. This page cannot let a
 * student in — it can only ask, and display the answer. If every tick is green
 * and the server says no, the server is right.
 *
 * **Consent comes before the camera.** The notice is shown, and the box is
 * ticked, before `getUserMedia` is ever called. A student finds out what is
 * being recorded and for how long before anything is recorded.
 */

type StepState = 'idle' | 'working' | 'ok' | 'failed'

interface Step {
  key: string
  label: string
  state: StepState
  detail?: string
}

export function PrecheckPage() {
  const { publicCode = '' } = useParams()
  const navigate = useNavigate()
  const videoRef = useRef<HTMLVideoElement | null>(null)

  const [quiz, setQuiz] = useState<PublicQuiz | null>(null)
  const [session, setSession] = useState<PrecheckSession | null>(null)
  const [capabilities, setCapabilities] = useState<CapabilityVerdict | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [consent, setConsent] = useState(false)

  const [camera, setCamera] = useState<MediaStream | null>(null)
  const [microphone, setMicrophone] = useState<MediaStream | null>(null)
  const [screen, setScreen] = useState<MediaStream | null>(null)
  const [faceSeen, setFaceSeen] = useState<boolean | null>(null)
  const [manyFaces, setManyFaces] = useState(false)
  const [failures, setFailures] = useState<string[]>([])
  const [checks, setChecks] = useState<Record<string, boolean>>({})
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let active = true
    api
      .getPublicQuiz(publicCode)
      .then((result) => {
        if (!active) return
        setQuiz(result)
        // A quiz that needs no check should never have landed here.
        if (!result.requires_precheck) navigate(`/q/${publicCode}`, { replace: true })
      })
      .catch((caught) => {
        if (!active) return
        setLoadError(
          caught instanceof ApiError && caught.status === 404
            ? 'No quiz found for this link. Check the code with your teacher.'
            : 'Could not load this quiz.',
        )
      })
    return () => {
      active = false
    }
  }, [publicCode, navigate])

  // Release every device when the page goes away. A camera light left on after
  // a student navigates elsewhere is not acceptable.
  useEffect(
    () => () => {
      stopStream(camera)
      stopStream(microphone)
      stopStream(screen)
    },
    [camera, microphone, screen],
  )

  const requirements = session?.requirements ?? null

  /** Step 1: open the session and ask the server what this browser must do. */
  async function handleBegin(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const opened = await api.startPrecheck(publicCode, {
        student_name: name.trim(),
        student_email: email.trim(),
      })
      setSession(opened)

      // Report what this browser genuinely supports. A browser that falls short
      // is blocked with a reason, never given a quietly reduced exam.
      const verdict = await api.submitCapabilities(
        opened.precheck_id,
        opened.precheck_token,
        detectCapabilities() as unknown as Record<string, unknown>,
      )
      setCapabilities(verdict)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'LOCKDOWN_CLIENT_REQUIRED') {
        setError(
          'This exam is set to require a desktop lockdown application, which is not ' +
            'available. Please contact your teacher — it cannot be taken in a browser.',
        )
      } else if (caught instanceof ApiError && caught.code === 'ATTEMPT_ALREADY_EXISTS') {
        setError('You have already started this quiz. Each student may attempt it once.')
      } else {
        setError(caught instanceof Error ? caught.message : 'Could not start the check.')
      }
    } finally {
      setBusy(false)
    }
  }

  /** Step 2: acquire whatever the server said is required. */
  async function handleGrant() {
    if (!requirements) return
    setBusy(true)
    setError(null)
    const problems: string[] = []

    if (requirements.camera && !isLive(camera)) {
      const result = await requestCamera()
      if (result.stream) {
        setCamera(result.stream)
        if (videoRef.current) videoRef.current.srcObject = result.stream
      } else if (result.failure) {
        problems.push(`Camera: ${MEDIA_FAILURE_LABELS[result.failure]}`)
      }
    }
    if (requirements.microphone && !isLive(microphone)) {
      const result = await requestMicrophone()
      if (result.stream) setMicrophone(result.stream)
      else if (result.failure) problems.push(`Microphone: ${MEDIA_FAILURE_LABELS[result.failure]}`)
    }
    if (requirements.screen_share && !isLive(screen)) {
      const result = await requestScreenShare()
      if (result.stream) setScreen(result.stream)
      else if (result.failure)
        problems.push(`Screen sharing: ${MEDIA_FAILURE_LABELS[result.failure]}`)
    }
    if (requirements.fullscreen && !isFullscreen()) {
      // Requested inside the click handler: browsers only grant it during a
      // user gesture.
      await enterFullscreen()
    }

    setFailures(problems)
    setBusy(false)
  }

  /** A single face-presence reading for the preview, not a monitoring loop. */
  const readFace = useCallback(async () => {
    const video = videoRef.current
    if (!video) return
    const detector = new FacePresenceDetector()
    const reading = await detector.read(video)
    if (reading.faceCount === null) {
      // Unknown, not absent — a distinction this page keeps visible.
      setFaceSeen(null)
      return
    }
    setFaceSeen(reading.faceCount >= 1)
    setManyFaces(reading.faceCount > 1)
  }, [])

  useEffect(() => {
    if (!requirements?.face_presence || !isLive(camera)) return
    const timer = window.setInterval(() => void readFace(), 1000)
    return () => window.clearInterval(timer)
  }, [requirements, camera, readFace])

  /** Step 3: submit observations. Only the server can answer "ready". */
  async function handleSubmitReadiness() {
    if (!session || !requirements) return
    setBusy(true)
    setError(null)
    try {
      const faceDetectorWorking = faceSeen !== null
      const verdict = await api.submitReadiness(session.precheck_id, session.precheck_token, {
        camera_active: isLive(camera),
        microphone_active: isLive(microphone),
        screen_share_active: isLive(screen),
        fullscreen_active: isFullscreen(),
        face_detected: faceSeen === true,
        multiple_faces: manyFaces,
        consent_accepted: consent,
        // Honest reporting, which blocks the start rather than downgrading it.
        monitoring_impaired: requirements.face_presence && !faceDetectorWorking,
        impaired_reason:
          requirements.face_presence && !faceDetectorWorking
            ? 'Face detection is not working in this browser.'
            : null,
      })
      setChecks(verdict.checks)
      setFailures(verdict.failures)
      setReady(verdict.ready)

      // Store one still so a human can later see who sat down. Best effort: a
      // failed upload must not block a student from starting.
      if (verdict.ready && requirements.evidence_stored && videoRef.current) {
        await captureStill(videoRef.current)
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not submit the checks.')
    } finally {
      setBusy(false)
    }
  }

  /** Step 4: ask the server to start. It re-evaluates before it agrees. */
  async function handleStartExam() {
    if (!session) return
    setBusy(true)
    setError(null)
    try {
      const started = await api.startProctoredExam(session.precheck_id, session.precheck_token)
      rememberAttemptToken(started.attempt_id, started.attempt_token)
      navigate(`/attempt/${started.attempt_id}`, { replace: true })
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'PRECHECK_NOT_READY') {
        setReady(false)
        setError(
          'The server re-checked and the requirements are no longer met. ' +
            'Please run the checks again.',
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

  const blocked = capabilities !== null && !capabilities.satisfiable

  const steps: Step[] = requirements
    ? ([
        requirements.camera && {
          key: 'camera',
          label: 'Camera running',
          state: isLive(camera) ? 'ok' : 'idle',
        },
        requirements.microphone && {
          key: 'microphone',
          label: 'Microphone running',
          state: isLive(microphone) ? 'ok' : 'idle',
        },
        requirements.screen_share && {
          key: 'screen',
          label: 'Screen shared',
          state: isLive(screen) ? 'ok' : 'idle',
        },
        requirements.fullscreen && {
          key: 'fullscreen',
          label: 'Fullscreen',
          state: isFullscreen() ? 'ok' : 'idle',
        },
        requirements.face_presence && {
          key: 'face',
          label: 'You are visible in frame',
          state: faceSeen === true ? 'ok' : faceSeen === false ? 'failed' : 'idle',
          detail:
            faceSeen === null
              ? 'Waiting for the face detector. If this never changes, this browser cannot detect faces.'
              : manyFaces
                ? 'More than one person is visible.'
                : undefined,
        },
        {
          key: 'consent',
          label: 'Monitoring notice accepted',
          state: consent ? 'ok' : 'idle',
        },
      ] as Array<Step | false>).filter((step): step is Step => step !== false)
    : []

  return (
    <div className="min-h-screen bg-slate-100 p-6">
      <div className="mx-auto w-full max-w-3xl space-y-6">
        <header className="card p-8">
          <p className="font-mono text-xs uppercase tracking-widest text-slate-500">
            Pre-exam security check
          </p>
          <h1 className="mt-2 text-2xl font-semibold text-slate-900">{quiz.title}</h1>
          <p className="mt-3 text-sm text-slate-700">
            This exam is monitored. Nothing has started yet — <strong>the timer begins only
            when these checks pass</strong>, so take as long as you need here. You will have the
            full {formatDuration(quiz.duration_seconds)} once the exam opens.
          </p>
        </header>

        <ErrorNote error={error} onDismiss={() => setError(null)} />

        {!session && (
          <form onSubmit={handleBegin} className="card space-y-4 p-8">
            <div>
              <label htmlFor="name" className="block text-sm font-medium text-slate-700">
                Full name
              </label>
              <input
                id="name"
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-slate-700">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {busy ? 'Checking your browser…' : 'Begin security check'}
            </button>
          </form>
        )}

        {blocked && capabilities && (
          <div className="card border-l-4 border-red-500 p-8">
            <h2 className="text-lg font-semibold text-slate-900">
              This browser cannot run this exam
            </h2>
            <p className="mt-2 text-sm text-slate-700">
              Your exam is not being started in a reduced form. Your teacher asked for
              monitoring this browser cannot provide, so it cannot begin here.
            </p>
            <ul className="mt-4 space-y-2 text-sm text-slate-700">
              {capabilities.missing.map((item) => (
                <li key={item} className="flex gap-2">
                  <span aria-hidden className="text-red-500">
                    ✕
                  </span>
                  <span>{CAPABILITY_LABELS[item] ?? item}</span>
                </li>
              ))}
              {capabilities.notes.map((note) => (
                <li key={note} className="text-slate-600">
                  {note}
                </li>
              ))}
            </ul>
          </div>
        )}

        {session && !blocked && requirements && (
          <>
            {/* Consent before the camera, not after. */}
            <section className="card p-8">
              <h2 className="text-lg font-semibold text-slate-900">What is monitored</h2>
              <ul className="mt-4 space-y-2 text-sm text-slate-700">
                {requirements.camera && <li>• Your camera is on for the whole exam.</li>}
                {requirements.microphone && <li>• Your microphone is on for the whole exam.</li>}
                {requirements.screen_share && <li>• The screen you choose to share is visible.</li>}
                {requirements.face_presence && (
                  <li>
                    • Your browser checks that a face is visible. If nobody is visible for more
                    than {requirements.face_missing_grace_seconds} seconds, that is recorded.
                  </li>
                )}
                {requirements.evidence_stored && (
                  <li>
                    • Still images are saved when something is recorded, kept privately for{' '}
                    {requirements.evidence_retention_days} days, and visible only to your teacher.
                    Video is not recorded.
                  </li>
                )}
                <li>• Switching tabs or leaving fullscreen is recorded.</li>
              </ul>

              <h3 className="mt-6 text-sm font-semibold text-slate-900">
                What this monitoring cannot do
              </h3>
              <ul className="mt-2 space-y-1 text-sm text-slate-600">
                {session.monitoring_notice.map((note) => (
                  <li key={note}>• {note}</li>
                ))}
              </ul>
              <p className="mt-4 text-sm text-slate-600">
                Recorded signals are reviewed by your teacher. A signal is not a finding, and
                nothing here decides on its own that you have done anything wrong.
              </p>

              <label className="mt-6 flex items-start gap-3 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={consent}
                  onChange={(event) => setConsent(event.target.checked)}
                  className="mt-0.5 h-4 w-4"
                />
                <span>
                  I have read the above and agree to be monitored for the duration of this exam.
                </span>
              </label>
            </section>

            <section className="card p-8">
              <h2 className="text-lg font-semibold text-slate-900">Your devices</h2>

              {requirements.camera && (
                <video
                  ref={videoRef}
                  autoPlay
                  playsInline
                  muted
                  className="mt-4 w-full max-w-sm rounded-lg bg-slate-900"
                />
              )}

              <ul className="mt-4 space-y-2">
                {steps.map((step) => (
                  <li key={step.key} className="flex items-start gap-3 text-sm">
                    <span
                      aria-hidden
                      className={
                        step.state === 'ok'
                          ? 'text-emerald-600'
                          : step.state === 'failed'
                            ? 'text-red-500'
                            : 'text-slate-400'
                      }
                    >
                      {step.state === 'ok' ? '✓' : step.state === 'failed' ? '✕' : '○'}
                    </span>
                    <span>
                      <span className="text-slate-800">{step.label}</span>
                      {step.detail && (
                        <span className="block text-xs text-slate-500">{step.detail}</span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>

              {failures.length > 0 && (
                <ul className="mt-4 space-y-1 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 ring-1 ring-amber-200">
                  {failures.map((failure) => (
                    <li key={failure}>{failure}</li>
                  ))}
                </ul>
              )}

              <div className="mt-6 flex flex-wrap gap-3">
                <button
                  type="button"
                  onClick={handleGrant}
                  disabled={busy || !consent}
                  className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {busy ? 'Working…' : 'Turn on my devices'}
                </button>
                <button
                  type="button"
                  onClick={handleSubmitReadiness}
                  disabled={busy || !consent}
                  className="rounded-lg border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-800 disabled:opacity-50"
                >
                  Check with the server
                </button>
              </div>
              {!consent && (
                <p className="mt-3 text-xs text-slate-500">
                  Accept the monitoring notice above before turning on any device.
                </p>
              )}
            </section>

            <section className="card p-8">
              <h2 className="text-lg font-semibold text-slate-900">Start the exam</h2>
              <p className="mt-2 text-sm text-slate-700">
                {ready
                  ? 'The server has authorized this exam. Your time starts when you click below.'
                  : 'The server has not authorized this exam yet. Run the checks above.'}
              </p>
              {Object.keys(checks).length > 0 && (
                <p className="mt-2 text-xs text-slate-500">
                  Server-side result:{' '}
                  {Object.entries(checks)
                    .map(([key, ok]) => `${key}: ${ok ? 'ok' : 'not met'}`)
                    .join(' · ')}
                </p>
              )}
              <button
                type="button"
                onClick={handleStartExam}
                disabled={busy || !ready}
                className="mt-4 w-full rounded-lg bg-emerald-600 px-4 py-3 text-sm font-semibold text-white disabled:opacity-40"
              >
                {busy ? 'Starting…' : 'Start my exam'}
              </button>
            </section>
          </>
        )}
      </div>
    </div>
  )
}
