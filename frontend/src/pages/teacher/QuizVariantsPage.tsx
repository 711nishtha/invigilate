import { PhasePlaceholder } from '../../components/PhasePlaceholder'

export function QuizVariantsPage() {
  return (
    <PhasePlaceholder
      title="Variant review"
      phase="Phase 3"
      description="Generated variants awaiting your approval. Nothing is served to a student until you approve it, and there is no setting that auto-approves AI output."
      delivers={[
        'Original question beside each generated variant',
        'Approve, reject or regenerate, per variant',
        'Validation and duplicate-detection results, including why a variant was rejected',
        'The approved-variant count that gates publication',
      ]}
    />
  )
}
