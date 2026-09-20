/**
 * Placeholder for a screen whose backend does not exist yet.
 *
 * It states plainly which phase delivers the feature rather than rendering
 * invented data. Fake rows here would be indistinguishable from real ones in a
 * demo, which is exactly the confusion this project cannot afford.
 */
export function PhasePlaceholder({
  title,
  phase,
  description,
  delivers,
}: {
  title: string
  phase: string
  description: string
  delivers: string[]
}) {
  return (
    <div className="card p-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">{description}</p>
        </div>
        <span className="shrink-0 rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 ring-1 ring-amber-200">
          {phase}
        </span>
      </div>

      <div className="mt-6 border-t border-slate-200 pt-6">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          This screen will provide
        </p>
        <ul className="mt-3 space-y-2">
          {delivers.map((item) => (
            <li key={item} className="flex gap-2 text-sm text-slate-700">
              <span aria-hidden="true" className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-300" />
              {item}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
