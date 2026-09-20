import { PhasePlaceholder } from '../../components/PhasePlaceholder'

export function QuizQuestionsPage() {
  return (
    <PhasePlaceholder
      title="Questions"
      phase="Phase 2"
      description="The original questions for this quiz. Each one becomes a question family that variants are generated from; a student's paper draws one variant per family."
      delivers={[
        'Add, edit and reorder question families',
        'Question type: MCQ, numerical, true/false or short answer',
        'Topic, difficulty and learning objective used to steer generation',
        'Numeric templates with parameter ranges, where the backend computes the answer',
      ]}
    />
  )
}
