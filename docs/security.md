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

| Authority | Where it is decided | Status |
|---|---|---|
| Attempt identity | `attempt_access.resolve_attempt` — lookup by token hash | built |
| Quiz identity | Looked up by `public_code`, never accepted from the body | built, tested |
| Quiz ownership | `services.ownership` — a WHERE clause, not a post-fetch check | built, tested |
| Publish eligibility | `services.publish_rules` — server decides, client only renders | built, tested |
| Variant approval | `services.variant_lifecycle` — only a human reaches APPROVED | built, tested |
| Which variants a student gets | Seeded draw, one assignable variant per family | built, tested |
| Question order / option order | Seeded shuffle, persisted in `attempt_questions` | built, tested |
| Correct answers | `question_variants.correct_option`, absent from `StudentQuestionOut` | built, tested |
| Score | `grading.grade_attempt`, at finalization only | built |
| `started_at` / `expires_at` | `core.clock.utcnow()`, written at creation | built |
| Attempt state transitions | `attempt_finalization.finalize_attempt` under a row lock | built |
| Integrity severity and totals | `services/integrity_policy.py` severity table | built, tested |
| Auto-submission | Server-side, inside the event request, under a row lock | built, tested |

"Status" is stated because a trust boundary that is documented but not yet
implemented is a plan, not a control. As of phase 3 every row above is built
and covered by adversarial tests.

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

Students are not required to hold a Supabase account; they are authorized by an
attempt token instead, described in full in section 4.

Identity for the one-attempt rule is resolved by `services/identity.py`. The MVP
mechanism is `SELF_DECLARED_EMAIL`: the student types an address and the
database enforces `UNIQUE (quiz_id, student_email)`. **This is a weak identity
claim** — anyone can type anyone's address, and nothing stops a student using a
second address on a second device. `StudentIdentity.is_verified` is `False` for
this mechanism precisely so that no code treats it as proof. A signed-in student
(`SUPABASE_ACCOUNT`) overrides whatever is typed in the form, and `dedupe_key`
is the single value the rule keys on, so a stronger mechanism is a resolver plus
a migration rather than a redesign of `Attempt`.

## 4. The attempt-token model

A student is authorized by a bearer token minted at `POST /attempts/start` and
sent thereafter as `X-Attempt-Token`.

**Generation.** `secrets.token_urlsafe(32)` — 32 bytes (256 bits) from the OS
CSPRNG. Not a UUID, not derived from the attempt id, not guessable from
anything the student can see.

**Storage.** Only `sha256(token)` is stored, in `attempts.attempt_token_hash`.
A leaked database backup does not hand out live attempts. A slow KDF (bcrypt,
argon2) is deliberately *not* used: those defend low-entropy human passwords
against offline guessing, and a 256-bit random token has nothing to guess. A
single SHA-256 is the right primitive and keeps the per-request cost negligible.

**Comparison.** `hmac.compare_digest` against the stored hash, so the comparison
is constant-time. The preceding database lookup is an indexed equality match on
a hash, which is not constant-time — but that leaks only timing about a hash of
a 256-bit secret, which is not a usable oracle.

**Scope.** The token authorizes exactly one attempt and nothing else. It grants
no access to any other attempt, any quiz, or any teacher route.

**Lookup order matters.** `resolve_attempt` queries by *token hash* and only
then checks that the row's id equals the id the caller asked for:

```python
attempt = db.scalar(select(Attempt).where(Attempt.attempt_token_hash == hash(token)))
if attempt is None or attempt.id != requested_id:
    raise NotFound("No such attempt.")
```

The victim's row is never queried, so nothing about it can leak — not even
whether it exists. The reverse order (fetch by id, then compare tokens) would
make the endpoint an existence oracle for attempt ids.

**Uniform failure.** A wrong token, a wrong id, a malformed id and a
nonexistent attempt all raise the same `ATTEMPT_NOT_FOUND` with the same
message. A test asserts the three cases are indistinguishable.

**Lifecycle.** A token keeps working for *reads* after the attempt is terminal,
because a student is entitled to see their own submitted result. It stops
working for *writes* the moment the attempt becomes `SUBMITTED`,
`AUTO_SUBMITTED` or `TIME_EXPIRED` — `require_active_attempt` raises
`ATTEMPT_ALREADY_FINALIZED`. There is no code path that reopens a terminal
attempt.

**Logging.** `token` is in the redaction key list, so a token passed as a log
field is replaced with `[REDACTED]`. Redaction works on fields, not on message
text, so interpolating a token into a log message would defeat it; a test scans
`app/` for exactly that pattern and fails if one appears.

**Not yet covered.** Token rotation and explicit revocation are not implemented.
A student who leaks their token to a classmate has shared their attempt, and the
only remedy today is the teacher closing the quiz. `MULTIPLE_SESSION_DETECTED`
(phase 5) is the intended signal for this, and it is a signal, not a block.

## 5. Answer-key confidentiality

The key never crosses the boundary:

1. Student-facing Pydantic schemas have no `correct_option`, `numeric_answer_json`,
   `explanation`, `variant_id` or `options_order_json` field, so the serializer
   cannot emit them even by accident.
2. `answers.is_correct` stays `NULL` until finalization, so correctness is not in
   the database to leak mid-exam.
3. Option permutations live server-side; the student submits a *displayed* index
   which the server maps back through `options_order_json`.
4. `GET /attempts/{id}/results` honours `show_score_after_submission`.
5. A test drives every student endpoint and asserts no response body contains
   a forbidden key at any depth, and a second test asserts no student-facing
   schema even has a field capable of holding one. A third inspects the built
   frontend bundle. The guarantee is enforced by CI, not by reviewer memory.

## 6. Integrity event severity policy

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

## 7. Timing

`started_at` and `expires_at` are written from the server clock and are immutable.
Every protected request compares `now()` against `expires_at`; past it, the
attempt is graded and finalized as `TIME_EXPIRED`, and the answer being submitted
is rejected with 410. Responses carry `server_time` so the UI countdown can
resynchronize. A small `ATTEMPT_GRACE_SECONDS` window absorbs network latency on
an answer that was in flight at the buzzer; it is measured in seconds and is not
configurable by the client.

Disconnecting the network does not pause the clock. Answers queued while offline
are replayed on reconnect and are accepted only if the attempt is still live.

## 8. Concurrency

- **Double submit**: `SELECT ... FOR UPDATE` on the attempt row. The loser of the
  race sees a terminal state and returns the stored result. One finalization, one
  email, one score.
- **Double start**: `UNIQUE (quiz_id, student_email)`. The loser gets 409.
- **Answer races**: unique constraint on `attempt_question_id` makes the write an
  upsert.
- **Submit vs auto-submit vs expiry**: all three funnel through
  `finalize_attempt`, which returns the stored result untouched if the attempt
  is already terminal. A later manual submit cannot launder an
  `AUTO_SUBMITTED` flag into a clean `SUBMITTED`.

These are verified in `tests/integration/test_postgres_concurrency.py` against
real PostgreSQL with two concurrent connections. They are **skipped** when
`TEST_DATABASE_URL` is not a PostgreSQL URL; see docs/database.md,
"PostgreSQL-specific behaviour".

## 9. API hardening

CORS allowlist from `CORS_ORIGINS` (no `*` when credentials are involved);
per-route rate limits (see below); request body cap (`MAX_REQUEST_BYTES`, default 256 KiB)
rejecting oversized payloads with 413; Pydantic validation at every boundary with
`extra="forbid"` on student-facing input; SQLAlchemy parameterized queries
throughout (no string-built SQL); `metadata_json` size-capped before persistence;
security headers (`X-Content-Type-Options`, `Referrer-Policy`,
`X-Frame-Options: DENY`); `/docs` and `/openapi.json` disabled in production.

### Rate limiting cannot be switched off

Rate limiting is a control, not a preference:

- There is **no setting** that disables it. `limiter.enabled` is never assigned
  by application code, and a test asserts that.
- An empty or unparseable `RATE_LIMIT_*` value is **rejected at settings load**.
  An empty string would otherwise make slowapi's `default_limits` an empty list
  and silently remove the global limit — exactly what a blank Render variable
  produces.
- In `production` and `staging`, startup calls `assert_rate_limiting_active()`
  and the process **refuses to boot** if the limiter is disabled or has no
  limits.
- The test suite disables the limiter through an autouse fixture, because
  slowapi's limiter is process-global and keys on one IP for the whole suite.
  Tests assert that this state does not leak between cases, and an opt-in
  `rate_limited` fixture exercises the real limits.
- Both 429 paths — the middleware's default limit and a per-route decorator —
  return the same documented envelope. The handler is deliberately synchronous:
  slowapi's middleware discards a coroutine handler and falls back to its own
  response shape.

The stub AI and email providers are guarded the same way: `AI_PROVIDER=stub` or
`EMAIL_PROVIDER=stub` in production or staging refuses to start, because they
would otherwise hand teachers fabricated questions and silently drop result
email with no error anywhere.

## 10. Secrets

`GEMINI_API_KEY`, `RESEND_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_JWT_SECRET` and `DATABASE_URL` exist only in the backend environment —
Render's secret store locally mirrored by `backend/.env`, which is gitignored.
`.env.example` carries placeholders only. The frontend bundle contains only
`VITE_API_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`; anything
prefixed `VITE_` is public by definition and is treated that way.

Structured logs redact tokens, JWTs and API keys, and never record answer keys.

## 11. Teacher-tenant isolation

Teachers are tenants of one another. Every teacher-facing route resolves its
resource through `services/ownership.py`, which applies the ownership predicate
**inside the query**:

```python
select(QuestionVariant)
    .join(QuestionFamily, ...).join(Quiz, ...)
    .where(QuestionVariant.id == requested_id, Quiz.owner_id == caller.id)
```

A variant belonging to another teacher does not match, so there is no row to
forget to check. Three consequences, each tested in `tests/test_ownership.py`:

- **404, never 403.** Another teacher's quiz and a nonexistent quiz return byte-
  identical responses, so ids cannot be probed for existence.
- **Malformed ids are authorization failures.** `parse_uuid` raises the same 404
  rather than a 422, which would otherwise reveal that an id was well-formed but
  unknown.
- **Nothing is mutated on a failed attempt.** A test asserts teacher B's variant
  is byte-identical after teacher A attempts approve, edit and delete on it.

`ADMIN` bypasses the owner predicate by design. That is the only role that does,
and the role comes from the database row, never from a request.

## 12. Answer-key confidentiality in the teacher API

The teacher-facing `VariantOut` schema **does** carry `correct_option`,
`numeric_answer_json` and `explanation`. That is deliberate: a review screen is
useless without the answer. What confines it is ownership (section 11).

The student-facing `StudentQuestionOut` schema has no field capable of holding a
key. `tests/test_answer_key_disclosure.py` asserts:

- the schema's field set is exactly what a student needs, and its intersection
  with the forbidden-field list is empty;
- handing it a full ORM-shaped dict still emits none of the forbidden fields —
  the serializer has nowhere to put them;
- no unauthenticated response, and no error response, contains a forbidden key
  at any depth;
- prompt text never appears in any response (the prompt *version* does, as
  provenance).

## 13. What the server guarantees, and what the browser cannot

The split below is the heart of the security model. Everything in the left
column holds no matter what the student's browser does, including running
modified JavaScript. Nothing in the right column can be guaranteed by any web
application, and the product does not claim otherwise.

### The server guarantees

| Guarantee | Mechanism |
|---|---|
| An attempt belongs to exactly one student | Token-hash lookup; id mismatch is a 404 |
| A student cannot read, answer or submit another student's attempt | `resolve_attempt` |
| A submitted attempt can never be reopened | Terminal status checked on every write |
| Two submits produce one score and one email | `FOR UPDATE` + idempotent `finalize_attempt` |
| The clock cannot be extended from the browser | `expires_at` written server-side, re-checked per request |
| The score cannot be set or influenced from the browser | Grading runs once, server-side, from the stored key |
| The answer key is never sent to a student | Absent from student schemas; `is_correct` NULL during the exam |
| A student cannot choose their own questions | Seeded draw server-side, frozen in `attempt_questions` |
| One attempt per student per quiz | Database unique constraint, not an application check |
| Only teacher-approved variants reach a paper | `is_assignable`: APPROVED/PUBLISHED and not superseded |
| A teacher cannot touch another teacher's content | Ownership predicate inside every query |
| A served question never changes | Supersession, plus `ON DELETE RESTRICT` |
| An unready quiz cannot be published | `publish_rules` blockers, checked server-side |
| Integrity severity cannot be understated by the client | Severity assigned from the server-side policy table |

### The browser cannot guarantee

| Not guaranteed | Why |
|---|---|
| That no second device is in use | A web page cannot see a phone, tablet or second laptop |
| That no other application is running | No web API enumerates OS processes |
| That nobody else is in the room | No web API sees the room; camera capture is opt-in and can be denied |
| That the person typing is the enrolled student | The MVP does no identity proofing |
| That integrity events are reported at all | A student can disable the reporting script |
| That fullscreen means a locked desktop | Fullscreen is a window state, not a kiosk mode |
| That clipboard blocking prevents copying | Screenshots and a phone camera are outside the browser |
| That DevTools is closed | Reliable detection does not exist; a fake detector is not implemented |

The honest reading: a student who suppresses the reporting script produces a
*silent* timeline, which the teacher can see as anomalous, but silence is not
proof of misconduct. The defensible outcomes come from the left column —
different papers, a server clock, server grading, and an auditable record —
not from the monitoring.

## 14. Database integrity guarantees

Several security properties are enforced by PostgreSQL rather than by
application code, because a constraint holds even when two requests race and
even if a future code path forgets to check.

| Constraint | Guarantees |
|---|---|
| `UNIQUE (quiz_id, student_email)` on `attempts` | One attempt per student per quiz, including under a concurrent double-start |
| `UNIQUE (attempt_id, position)` on `attempt_questions` | A paper cannot gain two questions at one position |
| `UNIQUE (attempt_id, variant_id)` on `attempt_questions` | The same variant cannot appear twice on one paper |
| `UNIQUE (attempt_question_id)` on `answers` | An answer is an upsert; grading cannot double-count |
| `UNIQUE (family_id, content_hash)` on `question_variants` | Exact-duplicate variants are refused even under a race |
| `ON DELETE RESTRICT` on `attempt_questions.variant_id` | A served variant cannot be deleted; history cannot be rewritten |
| `ON DELETE RESTRICT` on `attempt_questions.variant_id` | A variant that was served to a student cannot be deleted out from under the record of that exam |
| `ON DELETE CASCADE` on attempt children | Deleting an attempt leaves no orphan answers or events |
| Native enum types | An invalid status cannot be written, even by raw SQL |
| `CHECK` on quiz duration, question count, violation budget | Nonsense exam settings are rejected at the storage layer |
| `answers.is_correct` nullable | Correctness is genuinely absent during an exam, not merely unqueried |
| `timestamptz` throughout | No timezone-naive comparison can shift a deadline |

The full inventory of PostgreSQL-specific reliance, including transaction
boundaries and what SQLite cannot reproduce, is in
[database.md](./database.md#postgresql-specific-behaviour).

## 15. The student exam engine

### Attempt lifecycle

```
(no attempt) --POST /public/quizzes/{code}/attempts--> IN_PROGRESS
IN_PROGRESS --student submits--------------------------> SUBMITTED
IN_PROGRESS --integrity budget exceeded----------------> AUTO_SUBMITTED
IN_PROGRESS --server clock passes expires_at-----------> TIME_EXPIRED
```

All three terminal states are final. Every student request resolves the attempt
and then finalizes it if the clock has run out, *before* doing anything else —
so a student who leaves the tab open past the deadline has the attempt closed
by whatever request they make next, including the one trying to save an answer.

All three finalization paths call one function, `finalize_attempt`, under
`SELECT ... FOR UPDATE`. There is no second grading path to drift out of sync.

### Assignment

`build_pools` -> `filter_pools` -> `plan_paper(seed, question_count)`.

Families are sampled first, then one variant per family, so a student never
receives two variants of the same question. The seed is generated by
`secrets.randbits(63)` server-side; there is no request field that could supply
one. Question order and option order are drawn from the same seed, so the whole
paper is reproducible from the value stored on the attempt row.

`SELECT ... ORDER BY random() LIMIT n` is deliberately **not** used: it would
give one student two variants of one question and another none.

### Historical immutability

A served paper is historical assessment data. `attempt_questions` references
the exact `question_variants` row that was served, and that row is never
rewritten: an approved variant is superseded by a new version rather than
edited, rejection changes only status, and `ON DELETE RESTRICT` stops deletion.

**No snapshot table was added**, because the immutable-variant model already
guarantees the property. Tests prove a running attempt is unchanged after the
teacher supersedes, rejects, or tries to delete the variant it was served.

### Integrity signals

| Severity | Events | Effect |
|---|---|---|
| 0 (informational) | `TAB_VISIBLE`, `WINDOW_FOCUS`, `FULLSCREEN_ENTER`, `NETWORK_DISCONNECTED`, `NETWORK_RECONNECTED`, `RIGHT_CLICK`, `SCREEN_SHARE_STARTED`, `CAMERA_STARTED`, `ATTEMPT_RESUMED` | Recorded for the timeline. Never counts, never auto-submits. |
| 1 | `TAB_HIDDEN`, `WINDOW_BLUR`, `FULLSCREEN_EXIT`, `COPY_ATTEMPT`, `CUT_ATTEMPT`, `PASTE_ATTEMPT`, `KEYBOARD_SHORTCUT_ATTEMPT` | Counts toward the budget. |
| 3 | `SCREEN_SHARE_STOPPED`, `CAMERA_STOPPED`, `MULTIPLE_SESSION_DETECTED` | Counts toward the budget. |
| server-only | `TIME_EXPIRED`, `AUTO_SUBMISSION_TRIGGERED`, `ATTEMPT_RESUMED` | Refused if a client reports them. |

`NETWORK_DISCONNECTED` is deliberately weightless: punishing it would penalise
bad wifi, and the server clock runs regardless.

Severity is assigned server-side from this table. The request schema has no
`severity` and no `violation_count` field, so a client cannot understate its own
signals. Once cumulative severity exceeds `max_integrity_violations`, the server
— inside the same request, holding the row lock — records an
`AUTO_SUBMISSION_TRIGGERED` marker, flags the attempt, grades it and finalizes
it. The browser is told afterwards; killing JavaScript at that moment does not
avoid it.

### What a student may see

`AttemptResultOut` carries status, timestamps, answered count, integrity status
and violation count. Score, max score and percentage are present **only** when
the quiz sets `show_score_after_submission`; otherwise they are `null` in the
payload rather than hidden by the UI. Correct answers are never returned to a
student under any configuration in this phase.

### What the anti-cheat does not do

It detects configured browser integrity signals and enforces server-side
assessment rules. It does not detect a second device, another application, a
person in the room, or a phone. A student who disables JavaScript produces a
silent timeline, which a teacher can see is anomalous but which is not on its
own a finding of misconduct. No webcam, gaze, screen-recording or AI-based
classification exists in this system.

## 16. Known limitations

1. A second device (phone, laptop) is invisible to the browser.
2. A virtual machine or screen-sharing tool outside the browser is invisible.
3. A determined student can suppress client-side event reporting; the server then
   sees a silent exam, which the timeline makes visible to the teacher but which
   is not, on its own, proof of anything.
4. Identity is asserted by email unless students sign in through Supabase Auth;
   the MVP does not perform identity proofing.
5. **Attempt tokens can be shared.** A student who passes their token to
   somebody else has shared their attempt. There is no rotation or revocation;
   the remedy today is that the quiz owner closes the quiz.
5. Screen and camera capture are opt-in browser streams, not proctoring, and stop
   the moment the student denies permission.

These are properties of the web platform, not defects in this implementation. The
mitigation is layering — randomized papers, a server clock, server grading, and a
reviewable timeline — plus a future desktop lockdown client for exams that need a
stronger boundary.
