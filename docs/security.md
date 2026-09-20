# Security model

## 1. What this product claims

**Secure online assessment with integrity monitoring.**

It does not claim that cheating is impossible, and the UI must never say so. A
web page cannot see the operating system: it cannot enumerate running
applications, read a second monitor, see a phone on the desk, or detect a person
in the room. Browser events are *signals*, and signals plus a randomized paper
plus server-authoritative grading raise the cost of cheating. That is the honest
claim, and it is the one the interface makes.

Consequences, stated plainly:

- No fake DevTools detector. Timing-based DevTools "detection" is trivially
  defeated and gives the invigilator false confidence, so it is not implemented.
- Fullscreen is not a lockdown. It is a signal that the student left it.
- Clipboard blocking is a deterrent, not a boundary.
- Mobile is not claimed to offer the same guarantees; `SECURE` and `STRICT` mode
  block mobile browsers rather than pretend.
- OS-level lockdown is out of scope for the MVP and is listed as a future
  desktop-client integration.

## 2. The trust boundary

Everything in the browser is attacker-controlled: the DOM, JavaScript,
`localStorage`, the system clock, and every request body. The backend therefore
owns all of the following, and none of them can be influenced by a request:

| Authority | Where it is decided |
|---|---|
| Attempt identity | Attempt token hash compared server-side |
| Quiz identity | Looked up by `public_code`, never accepted from the body |
| Which variants a student gets | Seeded draw inside `attempt_service` |
| Question order / option order | Seeded shuffle, persisted in `attempt_questions` |
| Correct answers | `question_variants.correct_option`, never serialized to a student response |
| Score | `grading.py`, at finalization only |
| `started_at` / `expires_at` | Server clock (`now()`), written at creation |
| Attempt state transitions | `attempt_service` under a row lock |
| Integrity severity and totals | Server-side policy table |
| Auto-submission | Server-side, inside the event request |

Explicitly untrusted and either ignored or stored as forensic metadata only:
browser clocks, `client_reported_at`, `localStorage`, client-calculated scores,
client-chosen variant IDs, client-supplied severity, client-supplied role.

## 3. Authentication and authorization

Teachers authenticate with Supabase Auth. The backend verifies the JWT locally
(HS256 against `SUPABASE_JWT_SECRET`, checking `exp`, `aud` and `sub`) on every
request — there is no server-side session to desynchronize and no custom password
code to get wrong.

Role comes from the `users` table keyed by `sub`. A body containing
`"role": "ADMIN"` changes nothing. Teacher routes additionally verify
`quiz.owner_id == current_user.id` at the query level: ownership is a `WHERE`
clause, not a post-fetch `if`, so a mis-scoped query returns nothing rather than
leaking a row.

Students are not required to hold a Supabase account. `POST /attempts/start`
mints a high-entropy attempt token; only its SHA-256 hash is stored. The token
authorizes one attempt. Sending attempt A's token to attempt B's endpoint returns
404.

## 4. Answer-key confidentiality

The key never crosses the boundary:

1. Student-facing Pydantic schemas have no `correct_option`, `numeric_answer_json`,
   `explanation`, `variant_id` or `options_order_json` field, so the serializer
   cannot emit them even by accident.
2. `answers.is_correct` stays `NULL` until finalization, so correctness is not in
   the database to leak mid-exam.
3. Option permutations live server-side; the student submits a *displayed* index
   which the server maps back through `options_order_json`.
4. `GET /attempts/{id}/results` honours `show_score_after_submission`.
5. A test asserts that no student-facing response body contains any of the
   forbidden keys — the guarantee is enforced by CI, not by reviewer memory.

## 5. Integrity event severity policy

Severity is assigned by the server from this table. The teacher configures the
budget (`max_integrity_violations`); the cumulative severity is compared against
it.

| Event | Severity | Meaning |
|---|---|---|
| `TAB_HIDDEN` | 1 | Student switched tab or minimized |
| `WINDOW_BLUR` | 1 | Exam window lost focus |
| `FULLSCREEN_EXIT` | 1 | Left fullscreen in a secure exam |
| `COPY_ATTEMPT` | 1 | |
| `CUT_ATTEMPT` | 1 | |
| `PASTE_ATTEMPT` | 1 | |
| `RIGHT_CLICK` | 0 | Logged only |
| `KEYBOARD_SHORTCUT_ATTEMPT` | 1 | Configured shortcuts |
| `SCREEN_SHARE_STOPPED` | 3 | Monitoring stream ended |
| `CAMERA_STOPPED` | 3 | Monitoring stream ended |
| `MULTIPLE_SESSION_DETECTED` | 3 | Same attempt used from two sessions |
| `NETWORK_DISCONNECTED` | 0 | Recorded, never punished |
| `TAB_VISIBLE`, `WINDOW_FOCUS`, `FULLSCREEN_ENTER`, `NETWORK_RECONNECTED`, `SCREEN_SHARE_STARTED`, `CAMERA_STARTED`, `ATTEMPT_RESUMED` | 0 | Timeline context |
| `TIME_EXPIRED`, `AUTO_SUBMISSION_TRIGGERED` | 0 | System-generated markers |

`integrity_status` transitions: `CLEAN` → `WARNING` once any severity accrues →
`FLAGGED` when the budget is exceeded.

### Auto-submission

When cumulative severity exceeds the budget, the **server**, inside the same
`POST /event` request: stores the event, sets `FLAGGED`, writes
`AUTO_SUBMISSION_TRIGGERED`, grades, and finalizes as `AUTO_SUBMITTED`. The
browser is told what happened; it is never the thing that performs it. A student
who kills JavaScript at the moment of the second violation does not escape — the
event that crossed the threshold has already been processed server-side.

## 6. Timing

`started_at` and `expires_at` are written from the server clock and are immutable.
Every protected request compares `now()` against `expires_at`; past it, the
attempt is graded and finalized as `TIME_EXPIRED`, and the answer being submitted
is rejected with 410. Responses carry `server_time` so the UI countdown can
resynchronize. A small `ATTEMPT_GRACE_SECONDS` window absorbs network latency on
an answer that was in flight at the buzzer; it is measured in seconds and is not
configurable by the client.

Disconnecting the network does not pause the clock. Answers queued while offline
are replayed on reconnect and are accepted only if the attempt is still live.

## 7. Concurrency

- **Double submit**: `SELECT ... FOR UPDATE` on the attempt row. The loser of the
  race sees a terminal state and returns the stored result. One finalization, one
  email, one score.
- **Double start**: `UNIQUE (quiz_id, student_email)`. The loser gets 409.
- **Answer races**: unique constraint on `attempt_question_id` makes the write an
  upsert.

## 8. API hardening

CORS allowlist from `CORS_ORIGINS` (no `*` when credentials are involved);
per-route rate limits; request body cap (`MAX_REQUEST_BYTES`, default 256 KiB)
rejecting oversized payloads with 413; Pydantic validation at every boundary with
`extra="forbid"` on student-facing input; SQLAlchemy parameterized queries
throughout (no string-built SQL); `metadata_json` size-capped before persistence;
security headers (`X-Content-Type-Options`, `Referrer-Policy`,
`X-Frame-Options: DENY`); `/docs` and `/openapi.json` disabled in production.

## 9. Secrets

`GEMINI_API_KEY`, `RESEND_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_JWT_SECRET` and `DATABASE_URL` exist only in the backend environment —
Render's secret store locally mirrored by `backend/.env`, which is gitignored.
`.env.example` carries placeholders only. The frontend bundle contains only
`VITE_API_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`; anything
prefixed `VITE_` is public by definition and is treated that way.

Structured logs redact tokens, JWTs and API keys, and never record answer keys.

## 10. Known limitations

1. A second device (phone, laptop) is invisible to the browser.
2. A virtual machine or screen-sharing tool outside the browser is invisible.
3. A determined student can suppress client-side event reporting; the server then
   sees a silent exam, which the timeline makes visible to the teacher but which
   is not, on its own, proof of anything.
4. Identity is asserted by email unless students sign in through Supabase Auth;
   the MVP does not perform identity proofing.
5. Screen and camera capture are opt-in browser streams, not proctoring, and stop
   the moment the student denies permission.

These are properties of the web platform, not defects in this implementation. The
mitigation is layering — randomized papers, a server clock, server grading, and a
reviewable timeline — plus a future desktop lockdown client for exams that need a
stronger boundary.
