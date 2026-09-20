import { useParams } from 'react-router-dom'

/**
 * Pre-exam screen for /q/:publicCode.
 *
 * Phase 4 wires this to GET /api/public/quizzes/{public_code} and
 * POST /api/attempts/start. That endpoint returns only quiz metadata — never
 * question text, never the variant pool — and the paper is assembled by the
 * server after the student presses start.
 *
 * The rules below are shown before the exam begins because students are
 * entitled to know what is monitored before they consent to it.
 */
export function PublicQuizPage() {
  const { publicCode } = useParams()

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-6">
      <div className="card w-full max-w-2xl p-8">
        <p className="font-mono text-xs uppercase tracking-widest text-slate-500">
          Quiz code {publicCode}
        </p>
        <h1 className="mt-2 text-2xl font-semibold text-slate-900">Exam entry</h1>
        <p className="mt-2 text-sm text-slate-600">
          Quiz details and the start button load here once the student exam engine ships in phase 4.
          Every student who opens this link receives a different randomized paper, assembled by the
          server after they press start.
        </p>

        <div className="mt-6 rounded-lg bg-slate-50 p-5 ring-1 ring-slate-200">
          <h2 className="text-sm font-semibold text-slate-900">Before you begin</h2>
          <ul className="mt-3 space-y-2 text-sm text-slate-700">
            {[
              'Your exam is timed by the server. Closing the tab or losing your connection does not pause the clock.',
              'Secure exams must be taken in fullscreen on a desktop browser.',
              'Leaving the tab, leaving fullscreen and clipboard use are recorded as integrity events.',
              'If recorded events exceed the limit your teacher set, the exam is submitted automatically.',
              'You may attempt this quiz once.',
            ].map((rule) => (
              <li key={rule} className="flex gap-2">
                <span aria-hidden="true" className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" />
                {rule}
              </li>
            ))}
          </ul>
        </div>

        <p className="mt-6 text-xs leading-relaxed text-slate-500">
          Monitoring covers activity inside this browser tab only. It cannot see other applications
          or devices, and it is not a claim that this exam cannot be cheated.
        </p>
      </div>
    </div>
  )
}
