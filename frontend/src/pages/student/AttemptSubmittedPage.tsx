import { useParams } from 'react-router-dom'

/**
 * Post-submission screen.
 *
 * The score appears only when the teacher enabled show_score_after_submission;
 * the backend decides that, so the client cannot reveal a hidden score.
 */
export function AttemptSubmittedPage() {
  const { attemptId } = useParams()

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-6">
      <div className="card w-full max-w-lg p-8 text-center">
        <div
          aria-hidden="true"
          className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-50 text-xl text-emerald-600"
        >
          ✓
        </div>
        <h1 className="mt-4 text-xl font-semibold text-slate-900">Exam submitted</h1>
        <p className="mt-2 text-sm text-slate-600">
          Your answers are recorded. You may close this window.
        </p>
        <p className="mt-6 border-t border-slate-200 pt-4 font-mono text-xs text-slate-400">
          {attemptId}
        </p>
      </div>
    </div>
  )
}
