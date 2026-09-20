import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Spinner } from '../../components/Spinner'
import { CopyableCode, EmptyState, ErrorNote, StatusBadge, formatDuration } from '../../components/ui'
import { api, type QuizSummary } from '../../lib/api'

export function QuizListPage() {
  const [quizzes, setQuizzes] = useState<QuizSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setQuizzes(await api.listQuizzes())
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load your quizzes.')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (!quizzes && !error) return <Spinner label="Loading your quizzes" />

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">My quizzes</h1>
          <p className="mt-1 text-sm text-slate-600">
            Every student opens the same link and receives a different randomized exam.
          </p>
        </div>
        <Link to="/teacher/quizzes/new" className="btn-primary">
          Create quiz
        </Link>
      </div>

      <ErrorNote error={error} onDismiss={() => setError(null)} />

      {quizzes && quizzes.length === 0 ? (
        <EmptyState
          title="No quizzes yet"
          description="Create a quiz, add your original questions, generate and approve variants, then publish one link for your class."
          action={
            <Link to="/teacher/quizzes/new" className="btn-primary">
              Create your first quiz
            </Link>
          }
        />
      ) : (
        <div className="card divide-y divide-slate-200">
          {quizzes?.map((quiz) => (
            <div key={quiz.id} className="flex flex-wrap items-center gap-4 p-5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-3">
                  <Link
                    to={`/teacher/quizzes/${quiz.id}`}
                    className="truncate text-sm font-semibold text-slate-900 hover:text-brand-700"
                  >
                    {quiz.title}
                  </Link>
                  <StatusBadge status={quiz.status} />
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {quiz.question_count} question{quiz.question_count === 1 ? '' : 's'} ·{' '}
                  {formatDuration(quiz.duration_seconds)} · {quiz.family_count} authored ·{' '}
                  {quiz.assignable_variant_count} approved variant
                  {quiz.assignable_variant_count === 1 ? '' : 's'} · {quiz.attempt_count} attempt
                  {quiz.attempt_count === 1 ? '' : 's'}
                </p>
              </div>

              {quiz.status === 'PUBLISHED' && quiz.public_code && (
                <CopyableCode code={quiz.public_code} />
              )}

              <div className="flex gap-2">
                <Link to={`/teacher/quizzes/${quiz.id}/questions`} className="btn-secondary">
                  Questions
                </Link>
                <Link to={`/teacher/quizzes/${quiz.id}`} className="btn-secondary">
                  Settings
                </Link>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
