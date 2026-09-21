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
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = 'teacher', attemptToken, signal } = options
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
}
