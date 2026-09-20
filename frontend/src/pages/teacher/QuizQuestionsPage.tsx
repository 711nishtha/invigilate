import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { EmptyState, ErrorNote } from '../../components/ui'
import { api, type QuestionFamily, type QuestionType, type QuizDetail } from '../../lib/api'

const QUESTION_TYPES: { value: QuestionType; label: string; detail: string }[] = [
  { value: 'MCQ', label: 'Multiple choice', detail: 'Four options, one correct. AI-generated variants.' },
  { value: 'TRUE_FALSE', label: 'True / false', detail: 'A statement to judge. AI-generated variants.' },
  {
    value: 'NUMERICAL',
    label: 'Numerical',
    detail: 'The backend computes the answer from your template. No AI arithmetic.',
  },
  {
    value: 'SHORT_ANSWER',
    label: 'Short answer',
    detail: 'Stored for review. Cannot be auto-graded, so it cannot be published yet.',
  },
]

export function QuizQuestionsPage() {
  const { id = '' } = useParams()
  const [quiz, setQuiz] = useState<QuizDetail | null>(null)
  const [families, setFamilies] = useState<QuestionFamily[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)

  const [draft, setDraft] = useState({
    original_question: '',
    question_type: 'MCQ' as QuestionType,
    topic: '',
    difficulty: 'medium',
    learning_objective: '',
    template: '',
    expression: '',
    parameters: '',
  })

  const load = useCallback(async () => {
    try {
      const [detail, list] = await Promise.all([api.getQuiz(id), api.listFamilies(id)])
      setQuiz(detail)
      setFamilies(list)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load the questions.')
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  async function addFamily(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      let config: Record<string, unknown> = {}
      if (draft.question_type === 'NUMERICAL') {
        // The template is executed by the backend, so a malformed parameter
        // block must fail here rather than at generation time.
        config = {
          template: draft.template,
          expression: draft.expression,
          parameters: JSON.parse(draft.parameters || '{}'),
        }
      }
      await api.createFamily(id, {
        original_question: draft.original_question.trim(),
        question_type: draft.question_type,
        topic: draft.topic.trim() || null,
        difficulty: draft.difficulty || null,
        learning_objective: draft.learning_objective.trim() || null,
        generation_config_json: config,
      })
      setDraft({ ...draft, original_question: '', template: '', expression: '', parameters: '' })
      setAdding(false)
      await load()
    } catch (caught) {
      if (caught instanceof SyntaxError) {
        setError('The parameters box must contain valid JSON.')
      } else {
        setError(caught instanceof Error ? caught.message : 'Could not add the question.')
      }
    } finally {
      setBusy(false)
    }
  }

  async function removeFamily(familyId: string) {
    setError(null)
    try {
      await api.deleteFamily(familyId)
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not delete the question.')
    }
  }

  if (!families || !quiz) {
    return error ? <ErrorNote error={error} /> : <Spinner label="Loading questions" />
  }

  const locked = quiz.status === 'PUBLISHED' || quiz.status === 'CLOSED'

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <Link to={`/teacher/quizzes/${id}`} className="text-sm text-brand-600 hover:underline">
            ← {quiz.title}
          </Link>
          <h1 className="mt-1 text-2xl font-semibold text-slate-900">Questions</h1>
          <p className="mt-1 text-sm text-slate-600">
            Each question becomes a family. A student&apos;s paper draws one approved variant from
            each.
          </p>
        </div>
        <div className="flex gap-2">
          <Link to={`/teacher/quizzes/${id}/variants`} className="btn-secondary">
            Review variants
          </Link>
          {!locked && (
            <button type="button" className="btn-primary" onClick={() => setAdding((v) => !v)}>
              {adding ? 'Cancel' : 'Add question'}
            </button>
          )}
        </div>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />

      {locked && (
        <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900 ring-1 ring-amber-200">
          This quiz is {quiz.status.toLowerCase()}. Questions cannot be changed while students may
          be sitting it — unpublish first.
        </p>
      )}

      {adding && !locked && (
        <form onSubmit={addFamily} className="card space-y-5 p-6">
          <div>
            <label className="label" htmlFor="question">
              Original question
            </label>
            <textarea
              id="question"
              className="input min-h-24"
              required
              value={draft.original_question}
              onChange={(event) => setDraft({ ...draft, original_question: event.target.value })}
              placeholder="What is the time complexity of binary search on a sorted array?"
            />
          </div>

          <fieldset>
            <legend className="label">Question type</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {QUESTION_TYPES.map((type) => (
                <label
                  key={type.value}
                  className={`flex cursor-pointer gap-3 rounded-lg border p-3 ${
                    draft.question_type === type.value
                      ? 'border-brand-400 bg-brand-50'
                      : 'border-slate-200 hover:bg-slate-50'
                  }`}
                >
                  <input
                    type="radio"
                    name="qtype"
                    className="mt-1"
                    checked={draft.question_type === type.value}
                    onChange={() => setDraft({ ...draft, question_type: type.value })}
                  />
                  <span>
                    <span className="block text-sm font-medium text-slate-900">{type.label}</span>
                    <span className="block text-xs text-slate-600">{type.detail}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="topic">
                Topic
              </label>
              <input
                id="topic"
                className="input"
                value={draft.topic}
                onChange={(event) => setDraft({ ...draft, topic: event.target.value })}
                placeholder="Algorithms"
              />
            </div>
            <div>
              <label className="label" htmlFor="difficulty">
                Difficulty
              </label>
              <select
                id="difficulty"
                className="input"
                value={draft.difficulty}
                onChange={(event) => setDraft({ ...draft, difficulty: event.target.value })}
              >
                <option value="easy">easy</option>
                <option value="medium">medium</option>
                <option value="hard">hard</option>
              </select>
            </div>
          </div>

          <div>
            <label className="label" htmlFor="objective">
              Learning objective <span className="font-normal text-slate-400">(optional)</span>
            </label>
            <input
              id="objective"
              className="input"
              value={draft.learning_objective}
              onChange={(event) => setDraft({ ...draft, learning_objective: event.target.value })}
              placeholder="Analyse the complexity of divide-and-conquer search"
            />
          </div>

          {draft.question_type === 'NUMERICAL' && (
            <div className="space-y-4 rounded-lg bg-slate-50 p-4 ring-1 ring-slate-200">
              <p className="text-xs leading-relaxed text-slate-600">
                For numerical questions the backend computes the answer. Write a template with
                placeholders, declare the parameter ranges, and give the expression to evaluate. No
                AI-produced number is ever stored.
              </p>
              <div>
                <label className="label" htmlFor="template">
                  Template
                </label>
                <input
                  id="template"
                  className="input font-mono text-xs"
                  required
                  value={draft.template}
                  onChange={(event) => setDraft({ ...draft, template: event.target.value })}
                  placeholder="A classifier predicts {correct} of {total} samples. Accuracy?"
                />
              </div>
              <div>
                <label className="label" htmlFor="expression">
                  Expression
                </label>
                <input
                  id="expression"
                  className="input font-mono text-xs"
                  required
                  value={draft.expression}
                  onChange={(event) => setDraft({ ...draft, expression: event.target.value })}
                  placeholder="correct / total * 100"
                />
              </div>
              <div>
                <label className="label" htmlFor="parameters">
                  Parameters (JSON)
                </label>
                <textarea
                  id="parameters"
                  className="input min-h-28 font-mono text-xs"
                  required
                  value={draft.parameters}
                  onChange={(event) => setDraft({ ...draft, parameters: event.target.value })}
                  placeholder={
                    '{\n  "total": {"type": "int", "min": 50, "max": 500, "step": 10},\n  "correct": {"type": "int", "min": 10, "max": 500, "depends_on": "total", "rule": "lte"}\n}'
                  }
                />
              </div>
            </div>
          )}

          <div className="flex justify-end gap-3 border-t border-slate-200 pt-4">
            <button type="button" className="btn-secondary" onClick={() => setAdding(false)}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={busy}>
              {busy ? 'Adding…' : 'Add question'}
            </button>
          </div>
        </form>
      )}

      {families.length === 0 ? (
        <EmptyState
          title="No questions yet"
          description="Add your original questions here. Variants are generated from them, and a student's paper draws one approved variant per question."
          action={
            !locked ? (
              <button type="button" className="btn-primary" onClick={() => setAdding(true)}>
                Add your first question
              </button>
            ) : undefined
          }
        />
      ) : (
        <div className="card divide-y divide-slate-200">
          {families.map((family, index) => (
            <div key={family.id} className="flex flex-wrap items-start gap-4 p-5">
              <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold text-slate-600">
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-slate-900">{family.original_question}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {family.question_type} · {family.topic ?? 'no topic'} ·{' '}
                  {family.difficulty ?? 'no difficulty'}
                </p>
                <p className="mt-2 text-xs">
                  <span
                    className={
                      family.variant_counts.assignable > 0
                        ? 'font-medium text-emerald-700'
                        : 'font-medium text-amber-700'
                    }
                  >
                    {family.variant_counts.assignable} approved
                  </span>
                  <span className="text-slate-500">
                    {' '}
                    · {family.variant_counts.validated} awaiting review ·{' '}
                    {family.variant_counts.rejected} rejected
                  </span>
                </p>
              </div>
              <div className="flex gap-2">
                <Link
                  to={`/teacher/quizzes/${id}/variants?family=${family.id}`}
                  className="btn-secondary"
                >
                  Variants
                </Link>
                {!locked && (
                  <button
                    type="button"
                    className="btn-secondary text-red-600"
                    onClick={() => removeFamily(family.id)}
                  >
                    Delete
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
