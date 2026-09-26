/**
 * Typed API client.
 *
 * Every call goes to the FastAPI backend, which is authoritative. The client
 * attaches whichever credential the route needs and normalises the error shape
 * the API documents.
 *
 * Note what this file does NOT contain: any notion of correctness, scoring,
 * publish eligibility or ownership. Those are server decisions; the client only
 * renders what it is told.
 */

import { env } from './env'
import { getAccessToken } from './supabase'

export interface ApiErrorBody {
  error: { code: string; message: string; details?: Record<string, unknown> }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

type Auth = 'teacher' | 'attempt' | 'none'

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  auth?: Auth
  attemptToken?: string
  /** Scoped to one pre-exam check, and to nothing else. */
  precheckToken?: string
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = 'teacher', attemptToken, precheckToken, signal } =
    options
  const headers: Record<string, string> = { Accept: 'application/json' }

  if (body !== undefined) headers['Content-Type'] = 'application/json'

  if (auth === 'teacher') {
    const token = await getAccessToken()
    if (!token) throw new ApiError(401, 'NOT_AUTHENTICATED', 'Your session has expired.')
    headers.Authorization = `Bearer ${token}`
  } else if (auth === 'attempt') {
    if (!attemptToken) throw new ApiError(401, 'MISSING_ATTEMPT_TOKEN', 'No attempt token.')
    headers['X-Attempt-Token'] = attemptToken
  }

  // Carried independently of `auth`, because a precheck request is otherwise
  // unauthenticated: the token names one session and grants nothing else.
  if (precheckToken) headers['X-Precheck-Token'] = precheckToken

  let response: Response
  try {
    response = await fetch(`${env.apiUrl}/api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (cause) {
    // A network failure during an exam must be distinguishable from a rejection,
    // so the exam runner can queue and retry rather than give up.
    throw new ApiError(0, 'NETWORK_ERROR', 'Could not reach the server.', { cause: String(cause) })
  }

  if (response.status === 204) return undefined as T

  const payload = await response.json().catch(() => null)

  if (!response.ok) {
    const error = (payload as ApiErrorBody | null)?.error
    throw new ApiError(
      response.status,
      error?.code ?? 'HTTP_ERROR',
      error?.message ?? `Request failed with status ${response.status}.`,
      error?.details ?? {},
    )
  }

  return payload as T
}

// --- Types mirroring the backend schemas -----------------------------------

export type UserRole = 'TEACHER' | 'STUDENT' | 'ADMIN'
export type QuizStatus = 'DRAFT' | 'GENERATING' | 'READY' | 'PUBLISHED' | 'CLOSED' | 'ARCHIVED'
export type SecurityLevel = 'STANDARD' | 'SECURE' | 'STRICT'
export type ProctoringMode = 'STANDARD' | 'PROCTORED' | 'LOCKDOWN'
/** Note the absence of a 'PASSED' state: monitoring is observed, impaired,
 *  or not verifiable. It never silently becomes a clean exam. */
export type ProctoringStatus = 'NOT_REQUIRED' | 'ACTIVE' | 'DEGRADED' | 'UNVERIFIABLE'
/** There is deliberately no 'CHEATING' member. */
export type ProctoringReview =
  | 'PENDING'
  | 'REVIEWED_NO_ACTION'
  | 'REVIEWED_CONCERN'
  | 'REVIEWED_ESCALATED'
export type QuestionType = 'MCQ' | 'NUMERICAL' | 'TRUE_FALSE' | 'SHORT_ANSWER'
export type VariantStatus = 'GENERATED' | 'VALIDATED' | 'APPROVED' | 'PUBLISHED' | 'REJECTED'
export type GenerationStatus = 'AI_GENERATED' | 'DETERMINISTIC' | 'MANUAL' | 'FAILED'

export interface UserProfile {
  id: string
  name: string
  email: string
  role: UserRole
  created_at: string
}

export interface HealthStatus {
  status: string
  version: string
  environment: string
  server_time: string
}

export interface Blocker {
  code: string
  message: string
  family_id: string | null
  variant_id: string | null
}

export interface PublishReadiness {
  publishable: boolean
  blockers: Blocker[]
  family_count: number
  ready_family_count: number
  assignable_variant_count: number
  question_count: number
  distinct_paper_estimate: number
}

export interface Quiz {
  id: string
  title: string
  description: string | null
  duration_seconds: number
  question_count: number
  security_level: SecurityLevel
  max_integrity_violations: number
  randomization_enabled: boolean
  allow_backtracking: boolean
  show_score_after_submission: boolean
  coverage_config_json: Record<string, string[]>
  proctoring_mode: ProctoringMode
  proctoring_config_json: Record<string, unknown>
  status: QuizStatus
  public_code: string | null
  created_at: string
  published_at: string | null
}

export interface QuizSummary extends Quiz {
  family_count: number
  attempt_count: number
  assignable_variant_count: number
}

export interface QuizDetail extends Quiz {
  readiness: PublishReadiness
  stats: Record<string, number>
}

export interface VariantCounts {
  generated: number
  validated: number
  approved: number
  published: number
  rejected: number
  assignable: number
}

export interface QuestionFamily {
  id: string
  quiz_id: string
  original_question: string
  question_type: QuestionType
  topic: string | null
  difficulty: string | null
  learning_objective: string | null
  generation_config_json: Record<string, unknown>
  position: number
  created_at: string
  variant_counts: VariantCounts
}

/** Teacher-facing variant. Carries the answer key; never rendered to a student. */
export interface Variant {
  id: string
  family_id: string
  question_text: string
  options_json: string[] | null
  correct_option: number | null
  numeric_answer_json: Record<string, unknown> | null
  explanation: string | null
  generation_model: string | null
  generation_prompt_version: string | null
  generation_status: GenerationStatus
  validation_status: VariantStatus
  validation_errors_json: Record<string, unknown>[] | null
  rejection_reason: string | null
  content_hash: string
  version: number
  superseded_by_id: string | null
  approved_at: string | null
  created_at: string
  is_assignable: boolean
  in_use: boolean
}

export interface GenerationResult {
  family_id: string
  requested: number
  accepted: number
  rejected: { index: number; reasons: { reason: string; detail: string }[] }[]
  provider: string
  model: string | null
  prompt_version: string | null
  variants: Variant[]
  error_code: string | null
  error_message: string | null
}

export interface QuizCreatePayload {
  title: string
  description?: string | null
  duration_seconds: number
  question_count: number
  security_level?: SecurityLevel
  max_integrity_violations?: number
  randomization_enabled?: boolean
  allow_backtracking?: boolean
  show_score_after_submission?: boolean
  coverage_config_json?: Record<string, string[]>
}

export interface FamilyCreatePayload {
  original_question: string
  question_type: QuestionType
  topic?: string | null
  difficulty?: string | null
  learning_objective?: string | null
  generation_config_json?: Record<string, unknown>
}

// --- Student engine types --------------------------------------------------

export interface PublicQuiz {
  public_code: string
  title: string
  description: string | null
  duration_seconds: number
  question_count: number
  security_level: SecurityLevel
  max_integrity_violations: number
  allow_backtracking: boolean
  requires_fullscreen: boolean
  proctoring_mode: ProctoringMode
  /** When true, /q/:code routes to the pre-exam check instead of starting. */
  requires_precheck: boolean
  shows_score_after_submission: boolean
  owner_name: string | null
}

export interface AttemptStart {
  attempt_id: string
  /** Shown once. Persisted in sessionStorage so a refresh can resume. */
  attempt_token: string
  server_time: string
  started_at: string
  expires_at: string
  seconds_remaining: number
  question_count: number
  allow_backtracking: boolean
  security_level: SecurityLevel
  proctoring_mode: ProctoringMode
  /** The cadence the server requires. The client does not choose its own. */
  heartbeat_interval_seconds: number
}

export type AttemptStatus =
  | 'IN_PROGRESS'
  | 'SUBMITTED'
  | 'AUTO_SUBMITTED'
  | 'TIME_EXPIRED'
  | 'ABANDONED'

export type IntegrityStatus = 'CLEAN' | 'WARNING' | 'FLAGGED'

export interface AttemptState {
  attempt_id: string
  status: AttemptStatus
  server_time: string
  started_at: string
  expires_at: string
  /** Authoritative. The UI counts down from this, never from its own clock. */
  seconds_remaining: number
  question_count: number
  answered_count: number
  allow_backtracking: boolean
  security_level: SecurityLevel
  proctoring_mode: ProctoringMode
  proctoring_status: ProctoringStatus
  heartbeat_interval_seconds: number
  integrity_status: IntegrityStatus
  violation_count: number
  max_integrity_violations: number
  is_terminal: boolean
}

/** A question as served. There is no correct-answer field, by construction. */
export interface StudentQuestion {
  attempt_question_id: string
  position: number
  question_type: QuestionType
  question_text: string
  options: string[] | null
  selected_answer: Record<string, unknown> | null
}

export interface AttemptQuestions {
  attempt_id: string
  server_time: string
  seconds_remaining: number
  questions: StudentQuestion[]
}

export interface AnswerSaved {
  saved: true
  attempt_question_id: string
  answered_at: string
  server_time: string
  seconds_remaining: number
}

export type IntegrityEventType =
  | 'TAB_HIDDEN'
  | 'TAB_VISIBLE'
  | 'WINDOW_BLUR'
  | 'WINDOW_FOCUS'
  | 'FULLSCREEN_ENTER'
  | 'FULLSCREEN_EXIT'
  | 'COPY_ATTEMPT'
  | 'CUT_ATTEMPT'
  | 'PASTE_ATTEMPT'
  | 'RIGHT_CLICK'
  | 'KEYBOARD_SHORTCUT_ATTEMPT'
  | 'NETWORK_DISCONNECTED'
  | 'NETWORK_RECONNECTED'
  // Phase 5 proctoring signals. Only the client-reportable ones are listed:
  // PROCTORING_HEARTBEAT_LOST, PRECHECK_PASSED and EVIDENCE_CAPTURED are
  // server-generated and the API refuses them from a student.
  | 'CAMERA_STARTED'
  | 'CAMERA_STOPPED'
  | 'MICROPHONE_STARTED'
  | 'MICROPHONE_STOPPED'
  | 'SCREEN_SHARE_STARTED'
  | 'SCREEN_SHARE_STOPPED'
  | 'CAMERA_PERMISSION_DENIED'
  | 'MICROPHONE_PERMISSION_DENIED'
  | 'SCREEN_SHARE_PERMISSION_DENIED'
  | 'FACE_PRESENT'
  | 'FACE_MISSING'
  | 'MULTIPLE_FACES_DETECTED'
  | 'GAZE_AWAY_SUSTAINED'
  | 'SCREEN_RECORDING_STARTED'
  | 'SCREEN_RECORDING_STOPPED'
  | 'DEVICE_CHANGED'
  | 'PROCTORING_UNAVAILABLE'

export interface IntegrityEventResult {
  recorded: boolean
  violation_total: number
  max_integrity_violations: number
  integrity_status: IntegrityStatus
  action_taken: string
  /** The server already finalized the attempt; the browser is being told. */
  auto_submitted: boolean
  attempt_status: AttemptStatus
}

export interface AttemptResult {
  attempt_id: string
  status: AttemptStatus
  submitted_at: string | null
  server_time: string
  question_count: number
  answered_count: number
  integrity_status: IntegrityStatus
  violation_count: number
  score_visible: boolean
  score: number | null
  max_score: number | null
  percentage: number | null
}

// --- Results, export and delivery ------------------------------------------

export interface ScoreBand {
  band: string
  count: number
}

/** Dashboard counters. Averages are null when there is nothing to average. */
export interface QuizStats {
  attempt_count: number
  in_progress_count: number
  submitted_count: number
  auto_submitted_count: number
  expired_count: number
  finalized_count: number
  completion_rate: number | null
  average_percentage: number | null
  highest_percentage: number | null
  lowest_percentage: number | null
  average_score: number | null
  max_score: number | null
  clean_count: number
  warning_count: number
  flagged_count: number
  integrity_event_count: number
  score_distribution: ScoreBand[]
}

export interface AttemptRow {
  attempt_id: string
  student_name: string
  student_email: string
  status: AttemptStatus
  started_at: string
  submitted_at: string | null
  duration_seconds: number | null
  score: number | null
  max_score: number | null
  percentage: number | null
  integrity_status: IntegrityStatus
  violation_count: number
  answered_count: number
  question_count: number
}

export interface QuizResults {
  quiz_id: string
  title: string
  public_code: string | null
  question_count: number
  stats: QuizStats
  attempts: AttemptRow[]
}

export interface AttemptQuestionDetail {
  position: number
  question_type: QuestionType
  topic: string | null
  difficulty: string | null
  original_question: string
  question_text: string
  options: string[] | null
  selected_answer: string | null
  correct_answer: string | null
  is_correct: boolean | null
  answered_at: string | null
}

export interface TeacherIntegrityEvent {
  event_type: string
  severity: number
  occurred_at: string
  client_reported_at: string | null
  action_taken: string
  metadata: Record<string, unknown>
}

export interface AttemptDetail {
  attempt_id: string
  quiz_id: string
  quiz_title: string
  student_name: string
  student_email: string
  status: AttemptStatus
  started_at: string
  submitted_at: string | null
  duration_seconds: number | null
  score: number | null
  max_score: number | null
  percentage: number | null
  integrity_status: IntegrityStatus
  violation_count: number
  questions: AttemptQuestionDetail[]
  integrity_events: TeacherIntegrityEvent[]
}

export type DeliveryStatus = 'PENDING' | 'SENT' | 'FAILED'

export interface Delivery {
  id: string
  status: DeliveryStatus
  recipient: string
  provider: string | null
  provider_message_id: string | null
  attempt_count: number
  attempts_made: number
  error_code: string | null
  error_detail: string | null
  retained: boolean
  created_at: string
  sent_at: string | null
  failed_at: string | null
}

export interface SendResultsResult {
  sent: boolean
  delivery: Delivery
  error_code: string | null
  error_message: string | null
}

// --- Proctoring -------------------------------------------------------------

export type PrecheckStatus =
  | 'PENDING'
  | 'BLOCKED'
  | 'FAILED'
  | 'READY'
  | 'CONSUMED'
  | 'EXPIRED'

/** What the server requires. Derived from a policy the browser never sees whole. */
export interface PrecheckRequirements {
  mode: ProctoringMode
  camera: boolean
  microphone: boolean
  screen_share: boolean
  fullscreen: boolean
  face_presence: boolean
  multiple_face_detection: boolean
  gaze_signals: boolean
  object_detection: boolean
  screen_recording: boolean
  consent: boolean
  evidence_stored: boolean
  evidence_retention_days: number
  face_missing_grace_seconds: number
  gaze_away_grace_seconds: number
  heartbeat_interval_seconds: number
  required_capabilities: string[]
}

export interface PrecheckSession {
  precheck_id: string
  /** Shown once. Only its hash is stored, as with an attempt token. */
  precheck_token: string
  status: PrecheckStatus
  mode: ProctoringMode
  server_time: string
  expires_at: string
  requirements: PrecheckRequirements
  /** The provider's own words about what monitoring cannot do. Rendered as-is. */
  monitoring_notice: string[]
}

export interface CapabilityVerdict {
  precheck_id: string
  status: PrecheckStatus
  satisfiable: boolean
  missing: string[]
  notes: string[]
}

export interface ReadinessVerdict {
  precheck_id: string
  status: PrecheckStatus
  /** Computed by the server. There is no request field that can set it. */
  ready: boolean
  checks: Record<string, boolean>
  failures: string[]
  server_time: string
  expires_at: string
}

export interface HeartbeatResult {
  acknowledged: boolean
  server_time: string
  proctoring_status: ProctoringStatus
  integrity_status: IntegrityStatus
  violation_total: number
  max_integrity_violations: number
  next_heartbeat_seconds: number
  signals_recorded: string[]
  warnings: string[]
  auto_submitted: boolean
  attempt_status: AttemptStatus
}

export interface ProctoringSummary {
  mode: ProctoringMode
  proctoring_status: ProctoringStatus
  review_state: ProctoringReview
  outcome: string
  headline: string
  /** Shown next to the numbers, never tucked into a help page. */
  caveats: string[]
  signal_counts: Record<string, number>
  violation_total: number
  max_violations: number
  evidence_count: number
  auto_submitted: boolean
}

export interface ProctoringEvidenceItem {
  id: string
  kind: string
  captured_at: string
  content_type: string
  size_bytes: number
  retention_expires_at: string
  /** Short-lived and minted per request. Null once retention has purged it. */
  download_url: string | null
  metadata: Record<string, unknown>
}

export interface ProctoringEventItem {
  id: string
  event_type: string
  severity: number
  occurred_at: string
  action_taken: string
  metadata: Record<string, unknown>
}

export interface ProctoringDetail {
  attempt_id: string
  student_name: string
  student_email: string
  summary: ProctoringSummary
  monitoring_capabilities: Record<string, unknown>
  precheck: Record<string, unknown> | null
  events: ProctoringEventItem[]
  evidence: ProctoringEvidenceItem[]
  last_heartbeat_at: string | null
  server_time: string
}

export const api = {
  health: () => request<HealthStatus>('/health', { auth: 'none' }),

  /** Creates the local profile row after a Supabase sign-in. Idempotent. */
  upsertProfile: (name: string) =>
    request<UserProfile>('/auth/profile', { method: 'POST', body: { name } }),

  me: () => request<UserProfile>('/auth/me'),

  // --- Quizzes -------------------------------------------------------------
  listQuizzes: () => request<QuizSummary[]>('/quizzes'),
  getQuiz: (id: string) => request<QuizDetail>(`/quizzes/${id}`),
  createQuiz: (body: QuizCreatePayload) =>
    request<QuizDetail>('/quizzes', { method: 'POST', body }),
  updateQuiz: (id: string, body: Partial<QuizCreatePayload>) =>
    request<QuizDetail>(`/quizzes/${id}`, { method: 'PATCH', body }),
  deleteQuiz: (id: string) => request<void>(`/quizzes/${id}`, { method: 'DELETE' }),
  getReadiness: (id: string) => request<PublishReadiness>(`/quizzes/${id}/readiness`),
  publishQuiz: (id: string) => request<QuizDetail>(`/quizzes/${id}/publish`, { method: 'POST' }),
  unpublishQuiz: (id: string) =>
    request<QuizDetail>(`/quizzes/${id}/unpublish`, { method: 'POST' }),
  closeQuiz: (id: string) => request<QuizDetail>(`/quizzes/${id}/close`, { method: 'POST' }),

  // --- Question families ---------------------------------------------------
  listFamilies: (quizId: string) => request<QuestionFamily[]>(`/quizzes/${quizId}/questions`),
  createFamily: (quizId: string, body: FamilyCreatePayload) =>
    request<QuestionFamily>(`/quizzes/${quizId}/questions`, { method: 'POST', body }),
  updateFamily: (familyId: string, body: Partial<FamilyCreatePayload>) =>
    request<QuestionFamily>(`/questions/${familyId}`, { method: 'PATCH', body }),
  deleteFamily: (familyId: string) =>
    request<void>(`/questions/${familyId}`, { method: 'DELETE' }),

  // --- Variants ------------------------------------------------------------
  listVariants: (familyId: string) => request<Variant[]>(`/questions/${familyId}/variants`),
  generateVariants: (familyId: string, body: { count?: number; difficulty?: string }) =>
    request<GenerationResult>(`/questions/${familyId}/generate-variants`, {
      method: 'POST',
      body,
    }),
  approveVariant: (variantId: string) =>
    request<Variant>(`/variants/${variantId}/approve`, { method: 'POST' }),
  rejectVariant: (variantId: string, reason?: string) =>
    request<Variant>(`/variants/${variantId}/reject`, { method: 'POST', body: { reason } }),
  reinstateVariant: (variantId: string) =>
    request<Variant>(`/variants/${variantId}/reinstate`, { method: 'POST' }),
  deleteVariant: (variantId: string) =>
    request<void>(`/variants/${variantId}`, { method: 'DELETE' }),

  // --- Public (no auth) ----------------------------------------------------
  getPublicQuiz: (code: string) =>
    request<PublicQuiz>(`/public/quizzes/${encodeURIComponent(code)}`, { auth: 'none' }),

  startAttempt: (code: string, body: { student_name: string; student_email: string }) =>
    request<AttemptStart>(`/public/quizzes/${encodeURIComponent(code)}/attempts`, {
      method: 'POST',
      body,
      auth: 'none',
    }),

  // --- Pre-exam security check (precheck token) ----------------------------
  //
  // A proctored quiz cannot be started through startAttempt: the server
  // refuses. This is the only path in, and the server authorizes each step.

  startPrecheck: (code: string, body: { student_name: string; student_email: string }) =>
    request<PrecheckSession>(`/public/quizzes/${encodeURIComponent(code)}/precheck`, {
      method: 'POST',
      body,
      auth: 'none',
    }),

  submitCapabilities: (precheckId: string, token: string, body: Record<string, unknown>) =>
    request<CapabilityVerdict>(`/public/precheck/${precheckId}/capabilities`, {
      method: 'POST',
      body,
      auth: 'none',
      precheckToken: token,
    }),

  submitReadiness: (precheckId: string, token: string, body: Record<string, unknown>) =>
    request<ReadinessVerdict>(`/public/precheck/${precheckId}/readiness`, {
      method: 'POST',
      body,
      auth: 'none',
      precheckToken: token,
    }),

  startProctoredExam: (precheckId: string, token: string) =>
    request<AttemptStart>(`/public/precheck/${precheckId}/start`, {
      method: 'POST',
      body: {},
      auth: 'none',
      precheckToken: token,
    }),

  // --- Student (attempt token) ---------------------------------------------
  getAttempt: (attemptId: string, attemptToken: string) =>
    request<AttemptState>(`/student/attempts/${attemptId}`, { auth: 'attempt', attemptToken }),

  getAttemptQuestions: (attemptId: string, attemptToken: string) =>
    request<AttemptQuestions>(`/student/attempts/${attemptId}/questions`, {
      auth: 'attempt',
      attemptToken,
    }),

  saveAnswer: (
    attemptId: string,
    attemptToken: string,
    body: { attempt_question_id: string; selected_answer: Record<string, unknown> },
  ) =>
    request<AnswerSaved>(`/student/attempts/${attemptId}/answers`, {
      method: 'POST',
      body,
      auth: 'attempt',
      attemptToken,
    }),

  reportIntegrityEvent: (
    attemptId: string,
    attemptToken: string,
    body: { event_type: IntegrityEventType; client_reported_at?: string; metadata?: Record<string, unknown> },
  ) =>
    request<IntegrityEventResult>(`/student/attempts/${attemptId}/integrity-events`, {
      method: 'POST',
      body,
      auth: 'attempt',
      attemptToken,
    }),

  submitAttempt: (attemptId: string, attemptToken: string) =>
    request<AttemptResult>(`/student/attempts/${attemptId}/submit`, {
      method: 'POST',
      body: {},
      auth: 'attempt',
      attemptToken,
    }),

  getAttemptResult: (attemptId: string, attemptToken: string) =>
    request<AttemptResult>(`/student/attempts/${attemptId}/result`, {
      auth: 'attempt',
      attemptToken,
    }),

  // --- Teacher results -----------------------------------------------------
  getQuizResults: (quizId: string) => request<QuizResults>(`/quizzes/${quizId}/results`),

  getAttemptDetail: (attemptId: string) =>
    request<AttemptDetail>(`/attempts/${attemptId}/detail`),

  listDeliveries: (quizId: string) => request<Delivery[]>(`/quizzes/${quizId}/deliveries`),

  // --- Proctoring (attempt token) ------------------------------------------

  /**
   * Report observed monitoring state.
   *
   * The body carries observations only. There is no severity, violation count
   * or verdict field, and the backend rejects unknown keys, so there is nothing
   * the client could add that would change the server's conclusion.
   */
  proctoringHeartbeat: (
    attemptId: string,
    attemptToken: string,
    body: {
      camera_active: boolean
      microphone_active: boolean
      screen_share_active: boolean
      fullscreen_active: boolean
      monitoring_impaired: boolean
      impaired_reason?: string | null
    },
  ) =>
    request<HeartbeatResult>(`/student/attempts/${attemptId}/proctoring/heartbeat`, {
      method: 'POST',
      body,
      auth: 'attempt',
      attemptToken,
    }),

  /**
   * Upload one evidence still.
   *
   * multipart, so it bypasses the JSON helper. Write-only: the response carries
   * no URL and no path, because a student uploads evidence about themselves and
   * does not get to read the store back.
   */
  uploadEvidence: async (
    attemptId: string,
    attemptToken: string,
    kind: string,
    blob: Blob,
  ): Promise<{ stored: boolean; reason?: string | null }> => {
    const form = new FormData()
    form.append('kind', kind)
    form.append('file', blob, 'frame.jpg')
    const response = await fetch(
      `${env.apiUrl}/api/student/attempts/${attemptId}/proctoring/evidence`,
      { method: 'POST', headers: { 'X-Attempt-Token': attemptToken }, body: form },
    )
    if (!response.ok) {
      // Evidence is best effort. A failed upload must never interrupt an exam,
      // so this resolves rather than throwing into the monitoring loop.
      return { stored: false, reason: `HTTP_${response.status}` }
    }
    return (await response.json()) as { stored: boolean; reason?: string | null }
  },

  // --- Proctoring review (teacher) -----------------------------------------
  getProctoringDetail: (attemptId: string) =>
    request<ProctoringDetail>(`/attempts/${attemptId}/proctoring`),

  recordProctoringReview: (
    attemptId: string,
    body: { decision: ProctoringReview; note?: string | null },
  ) =>
    request<ProctoringSummary>(`/attempts/${attemptId}/proctoring/review`, {
      method: 'POST',
      body,
    }),

  emailResults: (quizId: string) =>
    request<SendResultsResult>(`/quizzes/${quizId}/email-results`, {
      method: 'POST',
      body: {},
    }),

  /**
   * Downloads the workbook.
   *
   * Uses fetch directly rather than the JSON helper because the response is a
   * binary stream. The report is generated server-side from database truth;
   * nothing authoritative is assembled in the browser.
   */
  downloadExport: async (quizId: string, filename: string): Promise<void> => {
    const token = await getAccessToken()
    if (!token) throw new ApiError(401, 'NOT_AUTHENTICATED', 'Your session has expired.')

    const response = await fetch(`${env.apiUrl}/api/quizzes/${quizId}/export`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!response.ok) {
      const payload = (await response.json().catch(() => null)) as ApiErrorBody | null
      throw new ApiError(
        response.status,
        payload?.error?.code ?? 'EXPORT_FAILED',
        payload?.error?.message ?? 'Could not generate the export.',
      )
    }

    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.click()
    URL.revokeObjectURL(url)
  },
}
