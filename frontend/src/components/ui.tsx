import type { ReactNode } from 'react'

import type { QuizStatus, VariantStatus } from '../lib/api'

/** Small shared primitives for the teacher screens. */

export function StatusBadge({ status }: { status: QuizStatus }) {
  const tone: Record<QuizStatus, string> = {
    DRAFT: 'bg-slate-100 text-slate-700 ring-slate-200',
    GENERATING: 'bg-blue-50 text-blue-700 ring-blue-200',
    READY: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    PUBLISHED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    CLOSED: 'bg-amber-50 text-amber-700 ring-amber-200',
    ARCHIVED: 'bg-slate-100 text-slate-500 ring-slate-200',
  }
  return (
    <span
      className={`inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${tone[status]}`}
    >
      {status.toLowerCase()}
    </span>
  )
}

export function VariantBadge({ status }: { status: VariantStatus }) {
  const tone: Record<VariantStatus, string> = {
    GENERATED: 'bg-slate-100 text-slate-700 ring-slate-200',
    VALIDATED: 'bg-blue-50 text-blue-700 ring-blue-200',
    APPROVED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    PUBLISHED: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
    REJECTED: 'bg-red-50 text-red-700 ring-red-200',
  }
  const label: Record<VariantStatus, string> = {
    GENERATED: 'generated',
    VALIDATED: 'awaiting review',
    APPROVED: 'approved',
    PUBLISHED: 'published',
    REJECTED: 'rejected',
  }
  return (
    <span
      className={`inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ${tone[status]}`}
    >
      {label[status]}
    </span>
  )
}

export function ErrorNote({ error, onDismiss }: { error: string | null; onDismiss?: () => void }) {
  if (!error) return null
  return (
    <div role="alert" className="flex items-start justify-between gap-3 rounded-lg bg-red-50 p-3 text-sm text-red-800 ring-1 ring-red-200">
      <span>{error}</span>
      {onDismiss && (
        <button type="button" onClick={onDismiss} className="shrink-0 text-red-600 hover:underline">
          Dismiss
        </button>
      )}
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="card p-10 text-center">
      <h3 className="text-base font-semibold text-slate-900">{title}</h3>
      <p className="mx-auto mt-2 max-w-md text-sm text-slate-600">{description}</p>
      {action && <div className="mt-6 flex justify-center">{action}</div>}
    </div>
  )
}

export function formatDuration(seconds: number): string {
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

export function CopyableCode({ code }: { code: string }) {
  return (
    <button
      type="button"
      onClick={() => navigator.clipboard?.writeText(`${window.location.origin}/q/${code}`)}
      title="Copy the public quiz link"
      className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-1.5 font-mono text-sm font-semibold tracking-wider text-white hover:bg-slate-700"
    >
      {code}
      <span aria-hidden="true" className="text-xs font-normal opacity-70">
        copy link
      </span>
    </button>
  )
}
