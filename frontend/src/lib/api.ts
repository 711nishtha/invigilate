/**
 * Typed API client.
 *
 * Every call goes to the FastAPI backend, which is authoritative. The client
 * attaches whichever credential the route needs and normalises the error shape
 * the API documents.
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

export const api = {
  health: () => request<HealthStatus>('/health', { auth: 'none' }),

  /** Creates the local profile row after a Supabase sign-in. Idempotent. */
  upsertProfile: (name: string) =>
    request<UserProfile>('/auth/profile', { method: 'POST', body: { name } }),

  me: () => request<UserProfile>('/auth/me'),
}
