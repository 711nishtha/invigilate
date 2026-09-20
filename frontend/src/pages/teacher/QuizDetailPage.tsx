import { PhasePlaceholder } from '../../components/PhasePlaceholder'

export function QuizDetailPage() {
  return (
    <PhasePlaceholder
      title="Quiz"
      phase="Phase 2"
      description="Settings, readiness and publication for a single quiz. Publishing assigns the non-sequential public code and is refused while any question family lacks an approved variant."
      delivers={[
        'Live readiness: families, approved variants, and what is blocking publish',
        "Publish and close actions with the server's reason when publication is refused",
        'The public link, once published',
      ]}
    />
  )
}
