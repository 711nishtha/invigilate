import { PhasePlaceholder } from '../../components/PhasePlaceholder'

export function QuizResultsPage() {
  return (
    <PhasePlaceholder
      title="Results"
      phase="Phase 6"
      description="Attempts, scores and integrity outcomes for this quiz, with Excel export and email delivery. All figures are computed server-side from PostgreSQL; Excel is an export format, not the database."
      delivers={[
        'Per-student score, percentage, duration, status and integrity status',
        'Attempt detail: the exact paper served, answers, and the event timeline',
        'Four-sheet XLSX export: summary, answers, integrity, question statistics',
        'Email the workbook to yourself, with manual resend if delivery fails',
      ]}
    />
  )
}
