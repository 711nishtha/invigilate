import { useEffect, useState } from 'react'

import {
  api,
  type ProctoringDetail,
  type ProctoringReview,
  type ProctoringSummary,
} from '../lib/api'
import { ErrorNote } from './ui'
import { Spinner } from './Spinner'

/**
 * Teacher-facing proctoring review for one attempt.
 *
 * The layout is an argument about what this data is worth, so it is worth
 * saying out loud:
 *
 * - **The caveats are above the signals, not below them.** A teacher who reads
 *   "4 face-missing events" and only later learns that a browser cannot see the
 *   room has been given the numbers in the wrong order.
 * - **There is no score, ranking or risk percentage.** Any single number here
 *   would be acted on as if it meant something, and it would not.
 * - **The decision buttons describe what the reviewer will do**, not what the
 *   student did. There is no "mark as cheating" control, because the platform
 *   is not in a position to support that finding and a dropdown would imply it
 *   is.
 * - **A monitoring gap is displayed as prominently as a signal.** "We could not
 *   see" is information a teacher needs; hiding it behind a green tick is the
 *   failure mode this whole phase exists to avoid.
 */

const OUTCOME_TONE: Record<string, string> = {
  NO_SIGNALS: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  INFORMATIONAL_ONLY: 'bg-slate-100 text-slate-700 ring-slate-200',
  REVIEW_RECOMMENDED: 'bg-amber-50 text-amber-900 ring-amber-200',
  MONITORING_INCOMPLETE: 'bg-amber-50 text-amber-900 ring-amber-200',
}

const DECISIONS: Array<{ value: ProctoringReview; label: string; hint: string }> = [
  {
    value: 'REVIEWED_NO_ACTION',
    label: 'Reviewed — no action',
    hint: 'I looked at the timeline and there is nothing to follow up.',
  },
  {
    value: 'REVIEWED_CONCERN',
    label: 'Reviewed — noted a concern',
    hint: 'Worth remembering, but not being taken further right now.',
  },
  {
    value: 'REVIEWED_ESCALATED',
    label: 'Reviewed — escalated',
    hint: 'Referred to whatever process your institution uses. No mark is changed here.',
  },
]

function severityTone(severity: number): string {
  if (severity === 0) return 'text-slate-400'
  if (severity >= 3) return 'text-red-600'
  return 'text-amber-600'
}

export function ProctoringPanel({ attemptId }: { attemptId: string }) {
  const [detail, setDetail] = useState<ProctoringDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [summary, setSummary] = useState<ProctoringSummary | null>(null)

  useEffect(() => {
    let active = true
    api
      .getProctoringDetail(attemptId)
      .then((value) => {
        if (!active) return
        setDetail(value)
        setSummary(value.summary)
      })
      .catch(
        (caught) =>
          active &&
          setError(caught instanceof Error ? caught.message : 'Could not load the record.'),
      )
    return () => {
      active = false
    }
  }, [attemptId])

  async function decide(decision: ProctoringReview) {
    setBusy(true)
    setError(null)
    try {
      setSummary(await api.recordProctoringReview(attemptId, { decision, note: note || null }))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not record the decision.')
    } finally {
      setBusy(false)
    }
  }

  if (error && !detail) return <ErrorNote error={error} />
  if (!detail || !summary) return <Spinner label="Loading proctoring record" />

  const capabilities = detail.monitoring_capabilities as {
    provides_os_lockdown?: boolean
    notes?: string[]
  }

  return (
    <section className="space-y-4">
      <div className="card p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">Proctoring record</h2>
            <p className="mt-1 text-sm text-slate-600">
              Mode {summary.mode.toLowerCase()} · monitoring{' '}
              {summary.proctoring_status.toLowerCase().replace('_', ' ')}
            </p>
          </div>
          <span
            className={`rounded-full px-3 py-1 text-xs font-medium ring-1 ${
              OUTCOME_TONE[summary.outcome] ?? 'bg-slate-100 text-slate-700 ring-slate-200'
            }`}
          >
            {summary.outcome.toLowerCase().replace(/_/g, ' ')}
          </span>
        </div>

        <p className="mt-4 text-sm text-slate-800">{summary.headline}</p>

        {/* Above the numbers, deliberately. */}
        <ul className="mt-4 space-y-1.5 rounded-lg bg-slate-50 p-4 text-xs text-slate-600 ring-1 ring-slate-200">
          {summary.caveats.map((caveat) => (
            <li key={caveat}>• {caveat}</li>
          ))}
          {capabilities.provides_os_lockdown === false && (
            <li>
              • This exam did not lock the operating system. No software in a browser can.
            </li>
          )}
        </ul>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="card p-5">
          <p className="text-sm font-medium text-slate-600">Counted signals</p>
          <p className="mt-2 text-lg font-semibold text-slate-900">
            {summary.violation_total} of {summary.max_violations} allowed
          </p>
        </div>
        <div className="card p-5">
          <p className="text-sm font-medium text-slate-600">Evidence stored</p>
          <p className="mt-2 text-lg font-semibold text-slate-900">{summary.evidence_count}</p>
        </div>
        <div className="card p-5">
          <p className="text-sm font-medium text-slate-600">Last heartbeat</p>
          <p className="mt-2 text-lg font-semibold text-slate-900">
            {detail.last_heartbeat_at
              ? new Date(detail.last_heartbeat_at).toLocaleTimeString()
              : '—'}
          </p>
        </div>
      </div>

      {detail.precheck && (
        <div className="card p-6">
          <h3 className="text-sm font-semibold text-slate-900">Pre-exam check</h3>
          <p className="mt-2 text-sm text-slate-600">
            Authorized by the server at{' '}
            {typeof detail.precheck.authorized_at === 'string'
              ? new Date(detail.precheck.authorized_at).toLocaleString()
              : 'unknown'}
            . Consent recorded{' '}
            {typeof detail.precheck.consent_accepted_at === 'string' ? 'yes' : 'no'}.
          </p>
          <p className="mt-2 text-xs text-slate-500">
            The reported capabilities and observations below were supplied by the student’s
            browser. They are recorded because the timeline is useful, not because they are
            proof.
          </p>
          <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-50 p-3 text-xs text-slate-700">
            {JSON.stringify(detail.precheck.reported_observations ?? {}, null, 2)}
          </pre>
        </div>
      )}

      <div className="card p-6">
        <h3 className="text-sm font-semibold text-slate-900">Timeline</h3>
        {detail.events.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600">No signals were recorded.</p>
        ) : (
          <ul className="mt-3 divide-y divide-slate-100">
            {detail.events.map((event) => (
              <li key={event.id} className="flex items-baseline gap-3 py-2 text-sm">
                <span className="w-20 shrink-0 font-mono text-xs text-slate-500">
                  {new Date(event.occurred_at).toLocaleTimeString()}
                </span>
                <span className="flex-1 text-slate-800">
                  {event.event_type.toLowerCase().replace(/_/g, ' ')}
                  {event.metadata.repeat_suppressed === true && (
                    <span className="ml-2 text-xs text-slate-400">
                      (repeat within the debounce window — not counted)
                    </span>
                  )}
                </span>
                <span className={`shrink-0 text-xs ${severityTone(event.severity)}`}>
                  {event.severity === 0 ? 'informational' : `weight ${event.severity}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {detail.evidence.length > 0 && (
        <div className="card p-6">
          <h3 className="text-sm font-semibold text-slate-900">Evidence</h3>
          <p className="mt-1 text-xs text-slate-500">
            Stored privately. These links are signed and expire in a few minutes; they are not
            shareable URLs.
          </p>
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            {detail.evidence.map((item) => (
              <figure key={item.id} className="text-xs text-slate-600">
                {item.download_url ? (
                  <img
                    src={item.download_url}
                    alt={`${item.kind} captured at ${new Date(item.captured_at).toLocaleTimeString()}`}
                    className="w-full rounded-lg border border-slate-200"
                  />
                ) : (
                  <div className="flex h-24 items-center justify-center rounded-lg bg-slate-100 text-slate-500">
                    No longer available
                  </div>
                )}
                <figcaption className="mt-1">
                  {new Date(item.captured_at).toLocaleTimeString()} · deleted{' '}
                  {new Date(item.retention_expires_at).toLocaleDateString()}
                </figcaption>
              </figure>
            ))}
          </div>
        </div>
      )}

      <div className="card p-6">
        <h3 className="text-sm font-semibold text-slate-900">Your decision</h3>
        <p className="mt-1 text-sm text-slate-600">
          This records what <em>you</em> concluded, attributed to your account. It does not
          change the student’s mark.
        </p>
        <ErrorNote error={error} onDismiss={() => setError(null)} />
        <textarea
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={3}
          maxLength={2000}
          placeholder="Optional note — what you saw, and why you reached this conclusion."
          className="mt-3 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
        />
        <div className="mt-3 flex flex-wrap gap-2">
          {DECISIONS.map((decision) => (
            <button
              key={decision.value}
              type="button"
              title={decision.hint}
              disabled={busy}
              onClick={() => decide(decision.value)}
              className={`rounded-lg px-3 py-2 text-sm font-medium ring-1 disabled:opacity-50 ${
                summary.review_state === decision.value
                  ? 'bg-slate-900 text-white ring-slate-900'
                  : 'bg-white text-slate-800 ring-slate-300 hover:bg-slate-50'
              }`}
            >
              {decision.label}
            </button>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">
          There is no “cheating” option here on purpose. Academic misconduct is a process your
          institution runs, with evidence this platform can contribute to but cannot establish.
        </p>
      </div>
    </section>
  )
}
