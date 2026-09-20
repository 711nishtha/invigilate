import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ErrorNote } from '../../components/ui'
import { api, type SecurityLevel } from '../../lib/api'

const SECURITY_LEVELS: { value: SecurityLevel; label: string; detail: string }[] = [
  {
    value: 'STANDARD',
    label: 'Standard',
    detail: 'Events are logged. No fullscreen requirement.',
  },
  {
    value: 'SECURE',
    label: 'Secure',
    detail: 'Fullscreen required, desktop browsers only.',
  },
  {
    value: 'STRICT',
    label: 'Strict',
    detail: 'Secure, plus optional camera and screen-share monitoring.',
  },
]

export function QuizNewPage() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [form, setForm] = useState({
    title: '',
    description: '',
    durationMinutes: 20,
    questionCount: 10,
    securityLevel: 'SECURE' as SecurityLevel,
    maxIntegrityViolations: 2,
    allowBacktracking: true,
    randomizationEnabled: true,
    showScoreAfterSubmission: false,
  })

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((current) => ({ ...current, [key]: value }))
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const quiz = await api.createQuiz({
        title: form.title.trim(),
        description: form.description.trim() || null,
        duration_seconds: form.durationMinutes * 60,
        question_count: form.questionCount,
        security_level: form.securityLevel,
        max_integrity_violations: form.maxIntegrityViolations,
        randomization_enabled: form.randomizationEnabled,
        allow_backtracking: form.allowBacktracking,
        show_score_after_submission: form.showScoreAfterSubmission,
      })
      navigate(`/teacher/quizzes/${quiz.id}/questions`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not create the quiz.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Create quiz</h1>
        <p className="mt-1 text-sm text-slate-600">
          You can change any of this until the quiz is published.
        </p>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />

      <form onSubmit={handleSubmit} className="card space-y-6 p-6">
        <div>
          <label className="label" htmlFor="title">
            Title
          </label>
          <input
            id="title"
            className="input"
            required
            maxLength={300}
            value={form.title}
            onChange={(event) => update('title', event.target.value)}
            placeholder="Data Structures Midterm"
          />
        </div>

        <div>
          <label className="label" htmlFor="description">
            Description <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <textarea
            id="description"
            className="input min-h-20"
            maxLength={5000}
            value={form.description}
            onChange={(event) => update('description', event.target.value)}
            placeholder="Covers units 1 to 3."
          />
        </div>

        <div className="grid gap-5 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="duration">
              Duration (minutes)
            </label>
            <input
              id="duration"
              type="number"
              className="input"
              min={1}
              max={480}
              required
              value={form.durationMinutes}
              onChange={(event) => update('durationMinutes', Number(event.target.value))}
            />
            <p className="mt-1 text-xs text-slate-500">
              Timed by the server. Losing connection does not pause the clock.
            </p>
          </div>

          <div>
            <label className="label" htmlFor="questionCount">
              Questions per student
            </label>
            <input
              id="questionCount"
              type="number"
              className="input"
              min={1}
              max={200}
              required
              value={form.questionCount}
              onChange={(event) => update('questionCount', Number(event.target.value))}
            />
            <p className="mt-1 text-xs text-slate-500">
              You may author more questions than this; each student gets a sample.
            </p>
          </div>
        </div>

        <fieldset>
          <legend className="label">Security level</legend>
          <div className="space-y-2">
            {SECURITY_LEVELS.map((level) => (
              <label
                key={level.value}
                className={`flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors ${
                  form.securityLevel === level.value
                    ? 'border-brand-400 bg-brand-50'
                    : 'border-slate-200 hover:bg-slate-50'
                }`}
              >
                <input
                  type="radio"
                  name="security"
                  className="mt-1"
                  checked={form.securityLevel === level.value}
                  onChange={() => update('securityLevel', level.value)}
                />
                <span>
                  <span className="block text-sm font-medium text-slate-900">{level.label}</span>
                  <span className="block text-xs text-slate-600">{level.detail}</span>
                </span>
              </label>
            ))}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            Monitoring covers activity inside the exam tab only. It cannot see other applications or
            devices, and it does not make cheating impossible.
          </p>
        </fieldset>

        <div>
          <label className="label" htmlFor="violations">
            Integrity violation budget
          </label>
          <input
            id="violations"
            type="number"
            className="input max-w-32"
            min={0}
            max={100}
            value={form.maxIntegrityViolations}
            onChange={(event) => update('maxIntegrityViolations', Number(event.target.value))}
          />
          <p className="mt-1 text-xs text-slate-500">
            Cumulative severity, not a count of events. The exam is submitted automatically by the
            server once this is exceeded.
          </p>
        </div>

        <div className="space-y-3 border-t border-slate-200 pt-5">
          {(
            [
              ['randomizationEnabled', 'Randomize question and option order'],
              ['allowBacktracking', 'Allow students to go back to earlier questions'],
              ['showScoreAfterSubmission', 'Show the score to students after they submit'],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="flex items-center gap-3 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={form[key]}
                onChange={(event) => update(key, event.target.checked)}
              />
              {label}
            </label>
          ))}
        </div>

        <div className="flex justify-end gap-3 border-t border-slate-200 pt-5">
          <button type="button" className="btn-secondary" onClick={() => navigate('/teacher/quizzes')}>
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={busy || !form.title.trim()}>
            {busy ? 'Creating…' : 'Create and add questions'}
          </button>
        </div>
      </form>
    </div>
  )
}
