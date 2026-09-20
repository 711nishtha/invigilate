# API contract

Base URL: `${API_URL}/api`. All payloads are JSON unless noted. Interactive
schema: `/docs` (disabled when `ENVIRONMENT=production`), machine-readable
`/openapi.json`.

## Authentication

Two independent credential types:

| Caller | Credential | Header |
|---|---|---|
| Teacher / admin | Supabase Auth JWT | `Authorization: Bearer <supabase_jwt>` |
| Student in an exam | Attempt token issued by `POST /attempts/start` | `X-Attempt-Token: <token>` |

The attempt token is a high-entropy opaque string; only its SHA-256 hash is
stored. It authorizes exactly one attempt and nothing else — it cannot read
other attempts, other quizzes, or any teacher route.

## Error shape

```json
{ "error": { "code": "ATTEMPT_EXPIRED", "message": "This attempt has expired.", "details": {} } }
```

| Status | When |
|---|---|
| 400 | Malformed input that passed schema validation but violates a rule |
| 401 | Missing/invalid JWT or attempt token |
| 403 | Authenticated but not permitted (wrong owner, wrong role) |
| 404 | Not found, or hidden from this caller by design |
| 409 | State conflict (already submitted, attempt already exists) |
| 410 | Attempt expired |
| 422 | Pydantic validation failure |
| 429 | Rate limited |

A resource the caller may not see returns **404, not 403**, so that IDs cannot be
probed for existence.

---

## Health

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | none | Liveness. Returns version + environment. |
| GET | `/api/health/ready` | none | Readiness: executes `SELECT 1` against PostgreSQL. |

## Auth

### `POST /api/auth/profile`
Upserts the local `users` row for the bearer of a valid Supabase JWT. Called once
after sign-in. `role` in the body is **ignored**; the server assigns `TEACHER` on
first upsert and never lets a request escalate its own role.

Request `{ "name": "Dr Rao" }` → 200 `UserOut`.

### `GET /api/auth/me`
Returns the caller's profile from the database.

```json
{ "id": "uuid", "email": "t@uni.edu", "name": "Dr Rao", "role": "TEACHER", "created_at": "..." }
```

## Quizzes (teacher, owner-scoped)

| Method | Path | Description |
|---|---|---|
| POST | `/api/quizzes` | Create a `DRAFT` quiz |
| GET | `/api/quizzes` | List quizzes owned by the caller (paginated) |
| GET | `/api/quizzes/{id}` | Quiz detail, including readiness counters |
| PATCH | `/api/quizzes/{id}` | Update settings. Rejected once `PUBLISHED` for fields that would change a running exam |
| GET | `/api/quizzes/{id}/readiness` | Why the quiz can or cannot be published |
| POST | `/api/quizzes/{id}/publish` | Assign `public_code`, set `PUBLISHED`, promote approved variants |
| POST | `/api/quizzes/{id}/unpublish` | Back to `DRAFT`; refused once any attempt exists |
| POST | `/api/quizzes/{id}/close` | Status `CLOSED`; no new attempts, in-flight attempts finish |
| DELETE | `/api/quizzes/{id}` | Delete; refused once any attempt exists |

While a quiz is `PUBLISHED`, settings that change the shape of a live exam
(`duration_seconds`, `question_count`, `security_level`,
`max_integrity_violations`, `randomization_enabled`, `allow_backtracking`,
`coverage_config_json`) are locked — `PATCH` returns 409
`QUIZ_PUBLISHED_IMMUTABLE_FIELDS` naming them. Title and description stay
editable. Question families cannot be added or removed at all while published
(409 `QUIZ_PUBLISHED_IMMUTABLE`).

`POST /api/quizzes` body:

```json
{
  "title": "Data Structures Midterm",
  "description": "Unit 1-3",
  "duration_seconds": 1200,
  "question_count": 10,
  "security_level": "SECURE",
  "max_integrity_violations": 2,
  "randomization_enabled": true,
  "allow_backtracking": true,
  "show_score_after_submission": false
}
```

`POST /publish` returns 409 listing every blocker when the quiz is not ready.
`GET /readiness` returns the same information without attempting to publish, so
the UI can explain the problem before the teacher presses the button:

```json
{
  "publishable": false,
  "blockers": [
    { "code": "FAMILY_WITHOUT_APPROVED_VARIANT",
      "message": "This question has no approved variant, so no student could be given it.",
      "family_id": "uuid", "variant_id": null }
  ],
  "family_count": 4, "ready_family_count": 3,
  "assignable_variant_count": 12, "question_count": 4,
  "distinct_paper_estimate": 64
}
```

Blocker codes: `NO_QUESTION_FAMILIES`, `NOT_ENOUGH_FAMILIES`,
`FAMILY_WITHOUT_APPROVED_VARIANT`, `VARIANT_MISSING_OPTIONS`,
`VARIANT_MISSING_CORRECT_ANSWER`, `VARIANT_CORRECT_OPTION_OUT_OF_RANGE`,
`VARIANT_DUPLICATE_OPTIONS`, `VARIANT_MISSING_NUMERIC_ANSWER`,
`DUPLICATE_VARIANTS_IN_FAMILY`, `FAMILY_MISSING_METADATA`,
`UNSUPPORTED_QUESTION_TYPE`, `TOPIC_COVERAGE_UNMET`,
`DIFFICULTY_COVERAGE_UNMET`.

## Questions and variants (teacher)

All implemented and tested. Ownership is resolved through `services.ownership`
for every route: a family is reachable only through its quiz's owner, a variant
only through its family's quiz's owner. A resource the caller does not own
returns **404**, never 403.

| Method | Path | Description |
|---|---|---|
| POST | `/api/quizzes/{id}/questions` | Create a question family |
| GET | `/api/quizzes/{id}/questions` | List families with lifecycle counts |
| POST | `/api/quizzes/{id}/questions/reorder` | Set family positions |
| GET | `/api/questions/{id}` | One family |
| PATCH | `/api/questions/{id}` | Edit a family |
| DELETE | `/api/questions/{id}` | Delete a family (refused if any variant was served) |
| GET | `/api/questions/{id}/variants` | List variants, including superseded ones |
| POST | `/api/questions/{id}/variants` | Add a variant by hand |
| POST | `/api/questions/{id}/generate-variants` | Run the generation pipeline |
| GET | `/api/variants/{id}` | One variant |
| PATCH | `/api/variants/{id}` | Edit — **may return a new variant**, see below |
| POST | `/api/variants/{id}/approve` | `VALIDATED` -> `APPROVED` |
| POST | `/api/variants/{id}/reject` | -> `REJECTED`, with an optional reason |
| POST | `/api/variants/{id}/reinstate` | `REJECTED` -> `VALIDATED` |
| DELETE | `/api/variants/{id}` | Delete (refused if published or served) |

### Variant lifecycle

`GENERATED -> VALIDATED -> APPROVED -> PUBLISHED`, with `REJECTED` reachable
from any state and returning to `VALIDATED`. Illegal transitions return 409
`ILLEGAL_VARIANT_TRANSITION` with the allowed targets in `details`. Only a
human performs `VALIDATED -> APPROVED`; there is no configuration that
auto-approves generated content.

### `POST /api/questions/{id}/generate-variants`

Body `{ "count": 5, "difficulty": "medium" }`. Returns **200 even when the
provider fails**, because a Gemini outage is a reportable event rather than a
server error, and the family is left untouched:

```json
{
  "family_id": "uuid", "requested": 5, "accepted": 4,
  "rejected": [{ "index": 3, "reasons": [{ "reason": "DUPLICATE_OF_EXISTING", "detail": "similarity 0.97" }] }],
  "provider": "gemini", "model": "gemini-2.5-flash", "prompt_version": "mcq-v1",
  "variants": [ /* VariantOut, status VALIDATED */ ],
  "error_code": null, "error_message": null
}
```

On provider failure: `error_code` is one of `AITimeout`, `AIRateLimited`,
`AITransportError`, `AIRequestRejected`, `AIMalformedResponse`; `accepted` is 0
and `variants` is empty. Rate limited at 20/minute per caller.

Accepted variants land as `VALIDATED`. **Nothing is auto-approved.**

### `PATCH /api/variants/{id}`

Editing a `GENERATED` or `VALIDATED` variant updates it in place and returns the
same id. Editing an `APPROVED`, `PUBLISHED` or already-served variant **creates
a new version**: the response is a different variant with `version` incremented,
and the original keeps its text with `superseded_by_id` set. See
[database.md](./database.md#variant-immutability-and-versioning).

Errors: 409 `NO_CHANGE` if the edit changes no content, 409 `DUPLICATE_VARIANT`
if it would duplicate a sibling.

## Public / student

> **Status: not implemented.** Everything in this section ships in phase 4. The
> student-facing response schema (`StudentQuestionOut`) exists and is tested for
> answer-key non-disclosure, but no student endpoint is registered yet.


### `GET /api/public/quizzes/{public_code}`
No auth. Returns only what the pre-exam screen needs:

```json
{ "public_code": "8F7K2P", "title": "Data Structures Midterm", "description": "Unit 1-3",
  "duration_seconds": 1200, "question_count": 10, "security_level": "SECURE",
  "max_integrity_violations": 2, "allow_backtracking": true, "requires_fullscreen": true,
  "owner_name": "Dr Rao", "status": "PUBLISHED" }
```

No question text, no variant IDs, no pool size.

### `POST /api/attempts/start`
Rate limited. Body `{ "public_code": "8F7K2P", "student_name": "...", "student_email": "..." }`.

Server: validates publication, enforces the one-attempt rule, generates the seed,
stamps `started_at`/`expires_at` from the server clock, draws one approved variant
per family, shuffles order and options, persists `attempt_questions`, returns:

```json
{ "attempt_id": "uuid", "attempt_token": "opaque-token-shown-once",
  "server_time": "2026-09-20T14:02:11Z", "expires_at": "2026-09-20T14:22:11Z",
  "quiz": { "title": "...", "allow_backtracking": true, "question_count": 10 } }
```

409 `ATTEMPT_ALREADY_EXISTS` if this student already attempted this quiz.

### `GET /api/attempts/{id}`
Attempt state plus `server_time` and `seconds_remaining` — the frontend
resynchronizes its countdown against this and never against `Date.now()`.

### `GET /api/attempts/{id}/questions`
The frozen paper, in this student's order, options already permuted:

```json
{ "questions": [
  { "attempt_question_id": "uuid", "position": 0, "question_type": "MCQ",
    "question_text": "...", "options": ["...", "...", "...", "..."],
    "selected_answer": { "displayed_index": 2 } } ] }
```

Never present: `correct_option`, `explanation`, `variant_id`, `options_order_json`,
`numeric_answer_json`.

### `POST /api/attempts/{id}/answer`
Body `{ "attempt_question_id": "uuid", "selected_answer": { "displayed_index": 2 } }`.
Validates ownership, attempt active, question belongs to this attempt, not
expired, format valid. Upserts. Response `{ "saved": true, "answered_at": "...", "server_time": "..." }`
— correctness is **not** returned.

### `POST /api/attempts/{id}/event`
Body `{ "event_type": "TAB_HIDDEN", "client_reported_at": "...", "metadata": {} }`.
Severity is assigned server-side from the policy table; a client-supplied severity
is discarded. Response:

```json
{ "recorded": true, "violation_total": 2, "integrity_status": "WARNING",
  "action_taken": "WARNING_ISSUED", "auto_submitted": false }
```

When the cumulative severity exceeds `max_integrity_violations`, the server marks
the attempt `FLAGGED`, writes an `AUTO_SUBMISSION_TRIGGERED` event, grades and
finalizes — inside the same request. `auto_submitted: true` tells the browser to
navigate to the submitted screen; it does not ask the browser to do the
submitting.

### `POST /api/attempts/{id}/submit`
Idempotent under a row lock. Returns `AttemptResult`; `score` and `percentage` are
`null` unless the quiz sets `show_score_after_submission`.

### `GET /api/attempts/{id}/results`
Student-facing summary with the same score-visibility rule. The teacher variant of
this view (full answer key, timeline, integrity events) is served under the quiz
results route and requires quiz ownership.

## Results (teacher)

| Method | Path | Description |
|---|---|---|
| GET | `/api/quizzes/{id}/attempts` | Attempt rows for the results table |
| GET | `/api/quizzes/{id}/stats` | Dashboard counters, average score, flagged count |
| GET | `/api/attempts/{id}/detail` | Full detail: questions, answers, key, timeline, events |
| POST | `/api/quizzes/{id}/export` | Build the four-sheet workbook |
| POST | `/api/quizzes/{id}/email-results` | Email the workbook to the quiz owner |

`POST /export` accepts `{ "delivery": "download" | "storage" }`. `download`
streams `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`;
`storage` uploads to the Supabase `exports` bucket and returns a signed URL.

`POST /email-results` returns 200 `{ "sent": true, "message_id": "..." }` or 502
`{ "sent": false, "error": "..." }` — and in the failure case the export is still
persisted and the endpoint stays safe to retry.

## Rate limits

| Route | Default |
|---|---|
| `POST /api/attempts/start` | 10/minute per IP |
| `POST /api/attempts/{id}/answer` | 240/minute per attempt |
| `POST /api/attempts/{id}/event` | 300/minute per attempt |
| `POST /api/questions/{id}/generate-variants` | 20/minute per user |
| everything else | 120/minute |

Exceeding a limit returns 429 with `Retry-After`. Event-stream throttling drops
excess events rather than failing the exam — an integrity signal is never a reason
to break a student's paper.
