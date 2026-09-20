import { PhasePlaceholder } from '../../components/PhasePlaceholder'

export function QuizNewPage() {
  return (
    <PhasePlaceholder
      title="Create quiz"
      phase="Phase 2"
      description="Title, description, duration, question count, security level, violation budget, backtracking and score visibility. The form posts to POST /api/quizzes, which creates the quiz as a DRAFT with no public code."
      delivers={[
        'Quiz settings validated on the server as well as in the form',
        'Security level: STANDARD, SECURE (fullscreen, desktop only) or STRICT',
        'Maximum integrity violations — a cumulative severity budget, not an event count',
        'Backtracking and post-submission score visibility',
      ]}
    />
  )
}
