import { useParams } from 'react-router-dom'

/**
 * Exam runner for /attempt/:attemptId.
 *
 * Phase 4 delivers the paper, the server-synchronized countdown and answer
 * submission; phase 5 adds the integrity monitor. The countdown here will be a
 * display only — the authoritative deadline lives in attempts.expires_at and is
 * re-checked on every request.
 */
export function AttemptPage() {
  const { attemptId } = useParams()

  return (
    <div className="exam-locked flex min-h-screen flex-col bg-white">
      <header className="border-b border-slate-200 px-6 py-3">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-4">
          <p className="text-sm font-medium text-slate-900">Exam in progress</p>
          <div className="flex items-center gap-3">
            <span className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700">
              Integrity: clean
            </span>
            <span className="font-mono text-lg font-semibold tabular-nums text-slate-900">--:--</span>
          </div>
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col justify-center px-6 py-12">
        <div className="card p-8">
          <h1 className="text-lg font-semibold text-slate-900">Exam engine arrives in phase 4</h1>
          <p className="mt-2 text-sm text-slate-600">
            Attempt <span className="font-mono text-xs">{attemptId}</span> will load its questions
            from GET /api/attempts/&#123;id&#125;/questions. The response carries the paper in this
            student&apos;s order with options already permuted, and never carries the answer key.
          </p>
        </div>
      </main>
    </div>
  )
}
