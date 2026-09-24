# Deployment runbook

Release candidate. This is the exact sequence to take the repository to a
running deployment, and the checks that prove each step worked.

Placeholders are written `<like-this>`. Nothing below invents a URL that does
not exist yet.

---

## 0. Before you start

| Prerequisite | Why |
|---|---|
| GitHub repository | Vercel and Render both deploy from it |
| Supabase project | PostgreSQL, Auth and Storage. **Use a new project**, not one holding other data — the first migration creates every table. |
| Gemini API key | Teacher variant generation. Verified working (see §6). |
| Resend account with a **verified sending domain** | Result email. Until a domain is verified Resend delivers only to the account owner's address. |

> **Rotate the Gemini key that was shared in chat before going live.** A key
> pasted into a transcript should be treated as disclosed.

---

## 1. Architecture

```
GitHub ──────────────┬──────────────────┐
                     │                  │
                  Vercel             Render
              (static React)      (Docker FastAPI)
                     │                  │
                 browser ── HTTPS ──────┤
                                        ├──► Supabase PostgreSQL  (system of record)
                                        ├──► Supabase Storage     (exports, optional)
                                        ├──► Gemini               (variant generation, teacher-time only)
                                        └──► Resend               (result email)
```

Two properties this shape is built around:

- **Render's filesystem is ephemeral.** Nothing is persisted there. Exports are
  generated in memory and streamed, or written to Supabase Storage.
- **No student request ever calls Gemini or Resend.** Generation happens while
  a teacher prepares a quiz; email happens after results exist. An outage at
  either provider cannot affect a running exam.

---

## 2. Supabase

1. Create the project. Record the project ref and database password.
2. **Settings → API**: copy `Project URL`, the `anon` key and the `service_role`
   key.
3. **Settings → API → JWT Settings**: copy the JWT secret.
4. **Settings → Database → Connection string → URI**: take both
   - the **transaction pooler** URI (port `6543`) — for the application, and
   - the **direct** URI (port `5432`) — for migrations.

   Rewrite the scheme to `postgresql+psycopg://`.
5. **Storage**: create two **private** buckets, `evidence` and `exports`. Public
   buckets would expose student names, answers and marks.
6. **Authentication → Providers**: enable Email. Add `<vercel-url>` and
   `http://localhost:5173` to the redirect allowlist.

### Connection modes

| Use | Port | Setting |
|---|---|---|
| Application (`DATABASE_URL`) | 6543 transaction pooler | `prepare_threshold=None`, already set in `app/db/session.py` |
| Migrations (`ALEMBIC_DATABASE_URL`) | 5432 direct | DDL must not run through the pooler |

`prepare_threshold=None` is not optional. pgBouncer in transaction mode cannot
hold server-side prepared statements; without it psycopg creates them silently
and a later execution fails with *prepared statement does not exist*.

Session-level constructs (advisory locks, `LISTEN`/`NOTIFY`, temp tables) are
unavailable through the pooler and are used nowhere in the codebase. Row locks
are transaction-scoped and work normally.

### Migrations

```bash
cd backend
ALEMBIC_DATABASE_URL="postgresql+psycopg://postgres.<ref>:<password>@<host>:5432/postgres" \
  alembic upgrade head
alembic current          # expect: 0003_result_deliveries (head)
```

Run migrations as a one-off job or pre-deploy step, **not** at container start:
a rolling deploy would otherwise run two migrations concurrently.

No manual SQL is required at any point.

### Rollback limitations

`alembic downgrade` works, with one deliberate caveat:

- **`0002_variant_lifecycle` is lossy in reverse.** It collapses five variant
  states into three: `VALIDATED → PENDING` and `PUBLISHED → APPROVED`. Rolling
  back and forward again leaves those variants as `GENERATED` and `APPROVED`
  respectively — a teacher would have to re-approve. Verified by direct test.
- `0003_result_deliveries` drops the delivery history table. Results are
  untouched; only the record of which emails were sent is lost.
- `0001_initial` downgrade drops every table. Never run it against a database
  holding real attempts.

Nothing destructive runs as part of a normal forward deployment.

---

## 3. Render (backend)

New → Web Service → connect the repository.

| Setting | Value |
|---|---|
| Runtime | Docker |
| Dockerfile path | `backend/Dockerfile` |
| Docker build context | `backend` |
| Health check path | `/api/health` |
| Instance type | Free to evaluate; **paid for a real exam window** |

The image already: uses `python:3.12-slim`, runs as non-root (`appuser`, uid
10001), binds `0.0.0.0`, honours Render's injected `$PORT`, and passes
`--proxy-headers`. Do not set `PORT` by hand.

Startup command is baked into the image; leave Render's override empty.

### Behaviour to expect

- **Free tier sleeps.** The first request after a sleep is slow. Acceptable for
  a teacher dashboard, **not** acceptable mid-exam. Use a paid instance for an
  exam window, or keep the service warm.
- **Restarts are safe.** No state lives in the process; an attempt's clock and
  paper are in the database.
- **Logs are structured JSON** on stdout, with tokens, keys and answer keys
  redacted. Render captures stdout automatically.

### Startup will refuse to boot if

- any required secret is missing,
- `AI_PROVIDER=stub` or `EMAIL_PROVIDER=stub`,
- `DATABASE_URL`, `FRONTEND_URL` or `CORS_ORIGINS` still point at localhost,
- rate limiting is somehow inactive.

That is intentional: each of these would otherwise start cleanly and fail
confusingly later.

---

## 4. Vercel (frontend)

New Project → import the repository.

| Setting | Value |
|---|---|
| Root directory | `frontend` |
| Framework preset | Vite |
| Build command | `npm run build` |
| Output directory | `dist` |

`frontend/vercel.json` already rewrites unknown paths to `index.html`, so
`/q/<code>` and `/attempt/<id>` survive a hard refresh, and sets
`X-Content-Type-Options`, `Referrer-Policy` and a `Permissions-Policy`.

A production build **fails at load** with a clear message if `VITE_API_URL` is
missing, rather than silently calling `localhost:8000`.

---

## 5. Environment variables

Nothing below contains a value. Copy from `.env.example`.

### Vercel (frontend) — all three are PUBLIC by construction

Anything prefixed `VITE_` is compiled into a file any visitor can download.

| Variable | Required | Safe in frontend | Notes |
|---|---|---|---|
| `VITE_API_URL` | **yes** | yes | The Render backend URL. Build fails loudly without it. |
| `VITE_SUPABASE_URL` | **yes** | yes | Project URL |
| `VITE_SUPABASE_ANON_KEY` | **yes** | **yes — this is the one key that may reach the browser** | Anon key only |

**Never** add `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`,
`GEMINI_API_KEY`, `RESEND_API_KEY` or `DATABASE_URL` here. A Playwright test
inspects the built bundle for exactly these names.

### Render (backend) — all backend-only, mark every one as secret

| Variable | Required | Backend-only | Notes |
|---|---|---|---|
| `ENVIRONMENT` | yes | — | `production` |
| `DATABASE_URL` | yes | yes | Supabase **pooler** URI, port 6543 |
| `ALEMBIC_DATABASE_URL` | for migrations | yes | Supabase **direct** URI, port 5432 |
| `SUPABASE_URL` | yes | — | Project URL (also public, but the backend needs it) |
| `SUPABASE_ANON_KEY` | optional | — | Not used by the backend today |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | **yes — never expose** | Storage writes and signed URLs |
| `SUPABASE_JWT_SECRET` | yes | **yes — never expose** | Verifies every teacher JWT. Disclosure means forged sessions. |
| `GEMINI_API_KEY` | yes | **yes — never expose** | |
| `GEMINI_MODEL` | optional | — | Defaults to `gemini-3.1-flash-lite`. See §6. |
| `AI_REQUEST_TIMEOUT_SECONDS` | optional | — | Defaults to 120. **Do not lower** — see §6. |
| `RESEND_API_KEY` | yes | **yes — never expose** | |
| `EMAIL_FROM` | yes | — | Must be on a Resend-verified domain |
| `FRONTEND_URL` | yes | — | Vercel URL. Used in result email links. Rejected if localhost. |
| `CORS_ORIGINS` | yes | — | Vercel URL. Comma-separated. Rejected if localhost. |
| `AI_PROVIDER` | optional | — | `gemini`. `stub` refuses to boot in production. |
| `EMAIL_PROVIDER` | optional | — | `resend`. `stub` refuses to boot in production. |
| `STORAGE_PROVIDER` | optional | — | `supabase` |
| `RETAIN_EXPORTS` | optional | — | `false` by default; workbooks are regenerable |
| `RATE_LIMIT_*` | optional | — | Sensible defaults; empty values are rejected at load |
| `LOG_LEVEL` | optional | — | `INFO` |

**The Supabase key split, stated plainly:** the **anon** key is designed to be
public and belongs in the browser. The **service_role** key bypasses row-level
security entirely and must never leave the backend.

---

## 6. Gemini

**Verified** against the live API on 2026-09-24.

| Property | Value |
|---|---|
| Endpoint | `https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent` |
| Auth | `x-goog-api-key` header (not a query parameter, not Bearer) |
| Configured model | `gemini-3.1-flash-lite` |
| Verified working | `gemini-flash-lite-latest`, `gemini-3.5-flash-lite` |
| Measured latency | **55–70 seconds**, roughly independent of variant count |
| Timeout | `AI_REQUEST_TIMEOUT_SECONDS=120` |

### Two operational facts

1. **`gemini-3.1-flash-lite` returned HTTP 503 "experiencing high demand" on
   every attempt during this audit**, across four consecutive runs and its
   `-preview` sibling. The adapter handles this correctly — bounded retries,
   then a reported failure that leaves the quiz untouched — but a teacher would
   see generation fail. `gemini-flash-lite-latest` generated successfully on
   every attempt. Switching is an environment variable, no code change:

   ```
   GEMINI_MODEL=gemini-flash-lite-latest
   ```

2. **Latency is ~1 minute and is fixed overhead, not per-variant** (1 variant
   62s, 5 variants 68s). Asking for more variants per request is therefore
   nearly free. Do not lower the 120s timeout; a 60s timeout failed on
   essentially every real call.

   A generation request can therefore occupy an HTTP connection for over a
   minute. If Render's proxy terminates it, the teacher sees a failure and the
   quiz is unchanged — retrying is safe.

Run the smoke test after configuring:

```bash
cd backend
GEMINI_API_KEY=<key> ./.venv/Scripts/python.exe scripts/gemini_smoke.py
# exit 0 verified · 1 failed · 2 not attempted (no key)
```

---

## 7. Resend

**UNVERIFIED** — no API key was available during this audit. The provider is
implemented and tested against a mock transport, but no real email has been
sent.

To verify:

1. Verify a sending domain in Resend and set `EMAIL_FROM` to an address on it.
2. Set `RESEND_API_KEY` on Render.
3. Complete one attempt on a test quiz, then press **Email results to me**.
4. Confirm: the response says `sent: true`, the delivery row shows `SENT` with a
   provider message id, and the email arrives with an `.xlsx` attachment.

Until a domain is verified Resend delivers only to the account owner's address.

A failed send never affects results: the delivery is recorded `FAILED` with a
reason and the teacher can retry.

---

## 8. Order of operations

1. Supabase project, buckets, auth redirect URLs.
2. Migrations from a machine that can reach the **direct** (5432) URI.
3. Render service with all backend variables. Confirm
   `GET /api/health/ready` returns `{"database": "ok"}`.
4. Vercel project with `VITE_API_URL` pointing at Render.
5. **Set `CORS_ORIGINS` and `FRONTEND_URL` on Render to the Vercel URL and
   redeploy.** This is the step that is always forgotten; the symptom is a CORS
   error on every teacher request.
6. Add the Vercel URL to Supabase Auth redirect URLs.
7. Run the production smoke test in §9.

---

## 9. Production smoke test

Run against the deployed system. Nothing here is automated yet.

**Teacher**

1. Create an account, sign in.
2. Create a quiz (2 questions, 10 minutes, show score after submission).
3. Add a question family.
4. Generate variants — **real Gemini**. Expect roughly a minute.
5. Confirm each variant shows as *awaiting review*, none auto-approved.
6. Approve at least one variant per family.
7. Publish. Copy the `/q/<code>` link.

**Student** — use a different browser profile or a private window

8. Open the link. Confirm the rules screen appears and shows no question text.
9. Start the exam. Confirm the paper appears and the timer counts down.
10. Answer a question, then **refresh**. Confirm the same paper, same option
    order, and the answer still selected.
11. Confirm the countdown resumed from the server's value, not from zero.
12. Submit. Confirm the result screen.

**Teacher again**

13. Open the results dashboard. Confirm the attempt appears with its score.
14. Open the attempt detail. Confirm the answers and the marking.
15. Download the Excel export. Confirm four sheets and correct data.
16. Press **Email results to me** — **real Resend**. Confirm `SENT` and that
    the email arrives with the attachment.

**Security — every one of these must fail**

17. Change a quiz id in the URL to another teacher's quiz → 404.
18. Change an attempt id → 404.
19. Replay an answer request with `"is_correct": true` added → 400
    `GRADING_FIELD_REJECTED`.
20. Replay the start request with an extra `"seed"` field → 422.
21. Inspect every student API response in DevTools → no `correct_option`,
    `numeric_answer_json` or `explanation` anywhere.
22. Search the deployed JS bundle for `correct_option` → present only in
    teacher-screen code paths, never with data attached.

---

## 10. Performance

The 200-student load test runs the real stack against real PostgreSQL and
passes with 0% errors and full correctness. **Its latency figures are not
production latency**: the harness drives the app in-process, so all requests
share one Python process and one GIL.

To measure production latency, deploy a staging instance and run an
out-of-process generator (k6, locust) against it. Until then production latency
is **UNVERIFIED**.

```bash
cd backend
LOAD_DATABASE_URL=<postgres-url> ./.venv/Scripts/python.exe scripts/load_test.py \
  --students 200 --questions 5 --variants 4 --workers 8
```

---

## 11. Post-deployment checks

- `GET /api/health` → `{"status": "ok"}`
- `GET /api/health/ready` → `{"database": "ok"}`
- Teacher sign-in returns a profile from `GET /api/auth/me`
- `GET /api/public/quizzes/<code>` returns metadata and **no** question text
- A second attempt with the same student email is refused with 409
- `POST /submit` twice returns the same score and sends one email
