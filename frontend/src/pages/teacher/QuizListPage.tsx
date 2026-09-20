import { PhasePlaceholder } from '../../components/PhasePlaceholder'

export function QuizListPage() {
  return (
    <PhasePlaceholder
      title="My quizzes"
      phase="Phase 2"
      description="Every quiz you own, with its status, public link and attempt count. Quiz CRUD lands in phase 2, on top of the quizzes table that phase 1 migrated."
      delivers={[
        'Draft, published and closed quizzes with their current status',
        'The public /q/CODE link for published quizzes, ready to copy',
        'Variant readiness per quiz, so you can see what is blocking publication',
        'Shortcuts into questions, variant review and results',
      ]}
    />
  )
}
