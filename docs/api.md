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

**Implemented and tested.** Public routes need no credential; every
`/api/student/*` route needs the attempt token in `X-Attempt-Token`. Every
authorization failure — wrong token, wrong id, malformed id, unknown attempt —
returns an identical 404.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/public/quizzes/{public_code}` | none | Pre-exam summary |
| POST | `/api/public/quizzes/{public_code}/attempts` | none | Start an attempt (rate limited) |
| GET | `/api/student/attempts/{id}` | token | State + authoritative clock |
| GET | `/api/student/attempts/{id}/questions` | token | The frozen paper, sanitized |
| POST | `/api/student/attempts/{id}/answers` | token | Save one answer (upsert) |
| POST | `/api/student/attempts/{id}/integrity-events` | token | Report a browser signal |
| POST | `/api/student/attempts/{id}/submit` | token | Finalize (idempotent) |
| GET | `/api/student/attempts/{id}/result` | token | The student's own result |

Every student request resolves the attempt and then finalizes it if the server
clock has passed `expires_at`, before doing anything else.


### `GET /api/public/quizzes/{public_code}`
No auth. Returns only what the pre-exam screen needs:

```json
{ "public_code": "8F7K2P", "title": "Data Structures Midterm", "description": "Unit 1-3",
  "duration_seconds": 1200, "question_count": 10, "security_level": "SECURE",
  "max_integrity_violations": 2, "allow_backtracking": true, "requires_fullscreen": true,
  "owner_name": "Dr Rao", "status": "PUBLISHED" }
```

No question text, no variant IDs, no pool size, and no internal quiz id. Only a
`PUBLISHED` quiz resolves: a draft or closed quiz returns a 404 byte-identical
to that of a code that never existed, so a guessed code reveals nothing.

`max_integrity_violations` is included deliberately. A student is entitled to
know the rules before consenting to be monitored, and knowing the budget does
not help them defeat it, because the count is kept server-side.

### `POST /api/public/quizzes/{public_code}/attempts`
Rate limited (`RATE_LIMIT_ATTEMPT_START`, default 10/minute). Body
`{ "student_name": "...", "student_email": "..." }` and nothing else: the schema
forbids extra fields, so supplying a seed, an attempt id, a start time or a
question list is a 422 rather than something silently ignored.

Server: validates publication, enforces the one-attempt rule, generates the seed,
stamps `started_at`/`expires_at` from the server clock, draws one approved variant
per family, shuffles order and options, persists `attempt_questions`, returns:

```json
{ "attempt_id": "uuid", "attempt_token": "opaque-token-shown-once",
  "server_time": "...", "started_at": "...", "expires_at": "...",
  "seconds_remaining": 1200, "question_count": 10,
  "allow_backtracking": true, "security_level": "SECURE" }
```

201 on success. `attempt_token` is returned once and never again; only its
SHA-256 hash is stored. 409 `ATTEMPT_ALREADY_EXISTS` if this student already
attempted this quiz, enforced by `UNIQUE (quiz_id, student_email)` so that two
simultaneous starts cannot both create a paper.

### `GET /api/student/attempts/{id}`
Attempt state plus `server_time` and `seconds_remaining`. The exam runner
resynchronizes its countdown against this and never against `Date.now()`. Also
carries `violation_count`, `max_integrity_violations` and `integrity_status`, so
a student can see where they stand; all three are read from the attempt row.

### `GET /api/student/attempts/{id}/questions`
The frozen paper, in this student's order, options already permuted. Read from
`attempt_questions` and never re-planned, so a refresh returns the identical
paper with options in the identical order:

```json
{ "questions": [
  { "attempt_question_id": "uuid", "position": 0, "question_type": "MCQ",
    "question_text": "...", "options": ["...", "...", "...", "..."],
    "selected_answer": { "displayed_index": 2 } } ] }
```

Never present: `correct_option`, `explanation`, `variant_id`,
`options_order_json`, `numeric_answer_json`. The schema has no field capable of
holding them, so a handler passing the wrong object still cannot leak one.

### `POST /api/student/attempts/{id}/answers`
Body `{ "attempt_question_id": "uuid", "selected_answer": { "displayed_index": 2 } }`.
Validates ownership, attempt active, question belongs to *this* attempt, not
expired, format valid. Upserts, so answering twice leaves one row.

A `selected_answer` containing `is_correct`, `score`, `percentage`,
`correct_option` or `grade` is rejected with 400 `GRADING_FIELD_REJECTED`
rather than ignored, so a client probing the boundary gets a clear answer.

Response: `{ "saved": true, "attempt_question_id": "...", "answered_at": "...",
"server_time": "...", "seconds_remaining": N }`. Correctness is **not**
returned, which would turn the exam into an oracle; `answers.is_correct` stays
NULL until finalization.

### `POST /api/student/attempts/{id}/integrity-events`
Body `{ "event_type": "TAB_HIDDEN", "client_reported_at": "...", "metadata": {} }`.
There is no `severity` and no `violation_count` field, so a client cannot
understate its own signals. `TIME_EXPIRED`, `AUTO_SUBMISSION_TRIGGERED` and
`ATTEMPT_RESUMED` are server-generated: a client reporting one gets 400
`SERVER_ONLY_EVENT`. Metadata is bounded (20 keys, 500 chars per value) before
storage. Severity comes from the table in
[security.md](./security.md#integrity-signals). Response:

```json
{ "recorded": true, "violation_total": 2, "max_integrity_violations": 2,
  "integrity_status": "WARNING", "action_taken": "WARNING_ISSUED",
  "auto_submitted": false, "attempt_status": "IN_PROGRESS" }
```

When the cumulative severity exceeds `max_integrity_violations`, the server marks
the attempt `FLAGGED`, writes an `AUTO_SUBMISSION_TRIGGERED` event, grades and
finalizes — inside the same request. `auto_submitted: true` tells the browser to
navigate to the submitted screen; it does not ask the browser to do the
submitting.

### `POST /api/student/attempts/{id}/submit`
Empty body. Idempotent under `SELECT ... FOR UPDATE`: a second submit blocks on
the lock, observes the terminal state and returns the same stored result.
Returns `AttemptResultOut`; `score`, `max_score` and `percentage` are `null`
unless the quiz sets `show_score_after_submission`.

Submitting an attempt whose clock has already run out returns status
`TIME_EXPIRED`, not `SUBMITTED`.

### `GET /api/student/attempts/{id}/result`
The student's own result, with the same score-visibility rule. 409
`ATTEMPT_NOT_FINALIZED` before submission. A token may read its own finished
attempt, because a student is entitled to their result page, but may not write
to it.

The teacher view of an attempt (answer key, timeline, integrity events) ships in
a later phase under the quiz results route and requires quiz ownership.

## Results (teacher)

**Implemented and tested.** Ownership-scoped like every other teacher route: a
quiz or attempt the caller does not own returns 404.

| Method | Path | Description |
|---|---|---|
| GET | `/api/quizzes/{id}/results` | Statistics plus the attempt table |
| GET | `/api/attempts/{id}/detail` | Full detail: paper, answers, key, timeline |
| POST | `/api/quizzes/{id}/export` | Stream the four-sheet workbook |
| GET | `/api/quizzes/{id}/deliveries` | Email delivery history |
| POST | `/api/quizzes/{id}/email-results` | Email the workbook to the quiz owner |
| GET | `/api/deliveries/{id}/export` | Signed URL for a retained export |

### `GET /api/quizzes/{id}/results`

Counters, score distribution, integrity summary and one row per attempt. With
no attempts every counter is zero and every average is `null`, so a client can
render a real empty state rather than a fabricated zero.

### `GET /api/attempts/{id}/detail`

The exact paper served, the student's answers, the correct answers, per-question
correctness and the integrity timeline. Carries the answer key **by design** —
this is the marking view — and is confined by ownership, not by omission.

### `POST /api/quizzes/{id}/export`

Builds the workbook server-side from database truth and streams it as
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`. Nothing
touches the container filesystem, which Render destroys on every deploy. Rate
limited (`RATE_LIMIT_EXPORT`, default 10/minute).

Sheets: **Summary** (quiz info, attempt counts, score statistics, integrity
summary), **Answers** (one row per student per question, with the selected and
correct answers), **Integrity** (every recorded signal), **Question statistics**
(presented / answered / correct / incorrect / unanswered / accuracy).

Never present in any sheet: attempt tokens, token hashes, seeds, API keys, or
any data from another quiz. A test asserts this.

### `POST /api/quizzes/{id}/email-results`

Empty body — the recipient is the quiz owner, read from the database. Accepting
an address would turn the endpoint into an open relay for student marks, so the
schema forbids extra fields.

Returns **200 even when delivery fails**, because the results are already
durable and a provider outage is a reportable event rather than a server error:

```json
{ "sent": false, "error_code": "EmailTimeout",
  "error_message": "Resend did not respond within 20s.",
  "delivery": { "status": "FAILED", "attempts_made": 1, "error_code": "EmailTimeout" } }
```

Rate limited (`RATE_LIMIT_EMAIL`, default 5/minute) so a double-click cannot
become a mail storm. Retry is safe: each attempt writes a new delivery row and
nothing in `attempts`, `answers` or `integrity_events` is touched.

### `GET /api/deliveries/{id}/export`

Returns a short-lived signed URL (default 300s) for an export retained in
Supabase Storage. Authorization happens server-side before any URL is minted;
the bucket is private. 404 `EXPORT_NOT_RETAINED` when `RETAIN_EXPORTS` was off.

## Rate limits

| Route | Default |
|---|---|
| `POST /api/public/quizzes/{code}/attempts` | 10/minute per IP |
| `POST /api/student/attempts/{id}/answers` | 240/minute per attempt |
| `POST /api/student/attempts/{id}/integrity-events` | 300/minute per attempt |
| `POST /api/questions/{id}/generate-variants` | 20/minute per user |
| `POST /api/quizzes/{id}/export` | 10/minute per user |
| `POST /api/quizzes/{id}/email-results` | 5/minute per user |
| everything else | 120/minute |

Exceeding a limit returns 429 with `Retry-After`. Event-stream throttling drops
excess events rather than failing the exam — an integrity signal is never a reason
to break a student's paper.
