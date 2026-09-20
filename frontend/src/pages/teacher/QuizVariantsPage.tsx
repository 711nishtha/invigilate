import { useCallback, useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { EmptyState, ErrorNote, VariantBadge } from '../../components/ui'
import { api, type GenerationResult, type QuestionFamily, type Variant } from '../../lib/api'

export function QuizVariantsPage() {
  const { id = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const selectedFamily = params.get('family')

  const [families, setFamilies] = useState<QuestionFamily[] | null>(null)
  const [variants, setVariants] = useState<Variant[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [lastRun, setLastRun] = useState<GenerationResult | null>(null)
  const [count, setCount] = useState(5)

  const loadFamilies = useCallback(async () => {
    try {
      const list = await api.listFamilies(id)
      setFamilies(list)
      if (!selectedFamily && list.length > 0) {
        setParams({ family: list[0].id }, { replace: true })
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load questions.')
    }
  }, [id, selectedFamily, setParams])

  const loadVariants = useCallback(async () => {
    if (!selectedFamily) return
    try {
      setVariants(await api.listVariants(selectedFamily))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load variants.')
    }
  }, [selectedFamily])

  useEffect(() => {
    void loadFamilies()
  }, [loadFamilies])

  useEffect(() => {
    void loadVariants()
  }, [loadVariants])

  async function generate() {
    if (!selectedFamily) return
    setBusy(true)
    setError(null)
    setNotice(null)
    setLastRun(null)
    try {
      const result = await api.generateVariants(selectedFamily, { count })
      setLastRun(result)
      // A provider outage is reported in the body, not as an exception: the
      // question is untouched and the teacher can retry.
      if (result.error_code) {
        setError(
          `Generation failed (${result.error_code}): ${result.error_message ?? 'unknown error'}. ` +
            'Your question is unchanged — you can try again.',
        )
      } else {
        setNotice(
          `${result.accepted} variant${result.accepted === 1 ? '' : 's'} accepted` +
            (result.rejected.length ? `, ${result.rejected.length} rejected by validation.` : '.'),
        )
      }
      await Promise.all([loadVariants(), loadFamilies()])
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Generation failed.')
    } finally {
      setBusy(false)
    }
  }

  async function act(action: () => Promise<unknown>, successMessage: string) {
    setError(null)
    setNotice(null)
    try {
      await action()
      setNotice(successMessage)
      await Promise.all([loadVariants(), loadFamilies()])
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'That action failed.')
    }
  }

  if (!families) {
    return error ? <ErrorNote error={error} /> : <Spinner label="Loading" />
  }

  if (families.length === 0) {
    return (
      <EmptyState
        title="No questions to review"
        description="Add an original question first, then generate variants from it."
        action={
          <Link to={`/teacher/quizzes/${id}/questions`} className="btn-primary">
            Add questions
          </Link>
        }
      />
    )
  }

  const current = families.find((family) => family.id === selectedFamily) ?? families[0]
  const pending = variants.filter((v) => v.validation_status === 'VALIDATED')

  return (
    <div className="space-y-6">
      <div>
        <Link to={`/teacher/quizzes/${id}/questions`} className="text-sm text-brand-600 hover:underline">
          ← Questions
        </Link>
        <h1 className="mt-1 text-2xl font-semibold text-slate-900">Variant review</h1>
        <p className="mt-1 text-sm text-slate-600">
          Nothing reaches a student until you approve it. There is no setting that auto-approves
          generated questions.
        </p>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />
      {notice && (
        <p className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800 ring-1 ring-emerald-200">
          {notice}
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-4">
        <aside className="lg:col-span-1">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Questions
          </p>
          <div className="card divide-y divide-slate-200">
            {families.map((family, index) => (
              <button
                key={family.id}
                type="button"
                onClick={() => setParams({ family: family.id })}
                className={`block w-full px-4 py-3 text-left transition-colors ${
                  family.id === current.id ? 'bg-brand-50' : 'hover:bg-slate-50'
                }`}
              >
                <span className="line-clamp-2 text-sm font-medium text-slate-900">
                  {index + 1}. {family.original_question}
                </span>
                <span
                  className={`mt-1 block text-xs ${
                    family.variant_counts.assignable > 0 ? 'text-emerald-700' : 'text-amber-700'
                  }`}
                >
                  {family.variant_counts.assignable} approved ·{' '}
                  {family.variant_counts.validated} to review
                </span>
              </button>
            ))}
          </div>
        </aside>

        <div className="space-y-4 lg:col-span-3">
          <div className="card p-5">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Original question
            </p>
            <p className="mt-2 text-sm text-slate-900">{current.original_question}</p>
            <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-slate-200 pt-4">
              <label className="text-sm text-slate-700" htmlFor="count">
                Generate
              </label>
              <input
                id="count"
                type="number"
                min={1}
                max={20}
                className="input w-20"
                value={count}
                onChange={(event) => setCount(Number(event.target.value))}
              />
              <span className="text-sm text-slate-700">variants</span>
              <button type="button" className="btn-primary" disabled={busy} onClick={generate}>
                {busy ? 'Generating…' : 'Generate'}
              </button>
              {current.question_type === 'NUMERICAL' && (
                <span className="text-xs text-slate-500">
                  Answers are computed by the backend, not by a model.
                </span>
              )}
            </div>
          </div>

          {lastRun && lastRun.rejected.length > 0 && (
            <div className="card p-5">
              <p className="text-sm font-medium text-slate-900">
                {lastRun.rejected.length} candidate
                {lastRun.rejected.length === 1 ? '' : 's'} rejected by validation
              </p>
              <ul className="mt-3 space-y-1.5 text-xs text-slate-600">
                {lastRun.rejected.map((rejection) => (
                  <li key={rejection.index}>
                    Candidate {rejection.index + 1}:{' '}
                    {rejection.reasons.map((r) => r.reason).join(', ')}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {variants.length === 0 ? (
            <EmptyState
              title="No variants yet"
              description="Generate variants from this question, then approve the ones you want students to see."
            />
          ) : (
            <>
              {pending.length > 0 && (
                <p className="text-sm text-slate-600">
                  {pending.length} variant{pending.length === 1 ? '' : 's'} awaiting your review.
                </p>
              )}
              <div className="space-y-4">
                {variants.map((variant) => (
                  <VariantCard
                    key={variant.id}
                    variant={variant}
                    onApprove={() =>
                      act(() => api.approveVariant(variant.id), 'Variant approved.')
                    }
                    onReject={() =>
                      act(() => api.rejectVariant(variant.id, 'Rejected by teacher'), 'Variant rejected.')
                    }
                    onReinstate={() =>
                      act(() => api.reinstateVariant(variant.id), 'Variant sent back for review.')
                    }
                    onDelete={() => act(() => api.deleteVariant(variant.id), 'Variant deleted.')}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function VariantCard({
  variant,
  onApprove,
  onReject,
  onReinstate,
  onDelete,
}: {
  variant: Variant
  onApprove: () => void
  onReject: () => void
  onReinstate: () => void
  onDelete: () => void
}) {
  const superseded = variant.superseded_by_id !== null

  return (
    <article className={`card p-5 ${superseded ? 'opacity-60' : ''}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-slate-900">{variant.question_text}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <VariantBadge status={variant.validation_status} />
          {variant.version > 1 && (
            <span className="text-xs text-slate-500">v{variant.version}</span>
          )}
        </div>
      </div>

      {variant.options_json && (
        <ul className="mt-3 space-y-1.5">
          {variant.options_json.map((option, index) => (
            <li
              key={`${variant.id}-${index}`}
              className={`flex gap-2 rounded px-2 py-1 text-sm ${
                index === variant.correct_option
                  ? 'bg-emerald-50 font-medium text-emerald-900'
                  : 'text-slate-700'
              }`}
            >
              <span className="text-slate-400">{String.fromCharCode(65 + index)}.</span>
              {option}
              {index === variant.correct_option && (
                <span className="ml-auto text-xs text-emerald-700">correct</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {variant.numeric_answer_json && (
        <p className="mt-3 rounded bg-emerald-50 px-2 py-1 text-sm text-emerald-900">
          Computed answer: <strong>{String(variant.numeric_answer_json.value)}</strong>
          {variant.numeric_answer_json.unit ? ` ${variant.numeric_answer_json.unit}` : ''}
          <span className="ml-2 text-xs opacity-75">
            tolerance ±{String(variant.numeric_answer_json.tolerance)}
          </span>
        </p>
      )}

      {variant.explanation && (
        <p className="mt-3 text-xs leading-relaxed text-slate-600">{variant.explanation}</p>
      )}

      {variant.rejection_reason && (
        <p className="mt-3 text-xs text-red-700">Rejected: {variant.rejection_reason}</p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-200 pt-4">
        <span className="mr-auto text-xs text-slate-400">
          {variant.generation_status === 'DETERMINISTIC'
            ? 'Backend-computed'
            : variant.generation_status === 'MANUAL'
              ? 'Written by hand'
              : `${variant.generation_model ?? 'AI'} · ${variant.generation_prompt_version ?? ''}`}
          {variant.in_use && ' · used in a student exam'}
          {superseded && ' · superseded by a newer version'}
        </span>

        {variant.validation_status === 'VALIDATED' && (
          <>
            <button type="button" className="btn-primary" onClick={onApprove}>
              Approve
            </button>
            <button type="button" className="btn-secondary" onClick={onReject}>
              Reject
            </button>
          </>
        )}
        {(variant.validation_status === 'APPROVED' ||
          variant.validation_status === 'PUBLISHED') && (
          <button type="button" className="btn-secondary" onClick={onReject}>
            Withdraw
          </button>
        )}
        {variant.validation_status === 'REJECTED' && (
          <button type="button" className="btn-secondary" onClick={onReinstate}>
            Send back for review
          </button>
        )}
        {!variant.in_use && variant.validation_status !== 'PUBLISHED' && (
          <button type="button" className="btn-secondary text-red-600" onClick={onDelete}>
            Delete
          </button>
        )}
      </div>
    </article>
  )
}
