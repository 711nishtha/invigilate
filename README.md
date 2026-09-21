# Secure Quiz Platform

Online assessment where every student opens the **same public link** and receives
a **different randomized exam**, with server-authoritative timing, grading and
integrity monitoring.

> **What this is:** secure online assessment with integrity monitoring.
> **What this is not:** a claim that cheating is impossible. A browser cannot see
> the operating system. See [docs/security.md](docs/security.md).

## Stack

React + TypeScript + Vite + Tailwind (Vercel) · FastAPI + SQLAlchemy + Alembic in
Docker (Render) · Supabase PostgreSQL / Auth / Storage · Gemini for question
variants · Resend for result email · openpyxl for XLSX.

## Core guarantees

| Guarantee | How |
|---|---|
| A teacher cannot touch another teacher's content | Ownership predicate inside every query; 404, never 403 |
| A served question never changes | Editing an approved variant supersedes it with a new version |
| Generated questions need human approval | `GENERATED -> VALIDATED -> APPROVED`; no automatic path |
| Same link, different exam | Seeded draw from a pool of approved variants, frozen at attempt creation |
| A running exam never changes | `attempt_questions` references the exact variant served; edits supersede |
| Integrity signals cannot be understated | Severity assigned server-side; the request has no severity field |
| The browser cannot cheat the clock | `started_at` / `expires_at` written from the server clock and checked on every request |
| The browser cannot cheat the score | Grading happens once, server-side, at finalization |
| The answer key never leaks | Absent from every student-facing schema; enforced by a test |
| One attempt per student | `UNIQUE (quiz_id, student_email)` in PostgreSQL |
| Double submit is safe | `SELECT ... FOR UPDATE`, idempotent finalization |
| AI output is never trusted | Strict validation, dedup, mandatory human approval |

## Repository layout

```
secure-quiz/
├── README.md
├── .env.example                  placeholders only
├── docker-compose.yml            API + local PostgreSQL
├── docs/
│   ├── architecture.md           system design + 7 Mermaid diagrams
│   ├── security.md               trust boundary, integrity policy, limitations
│   ├── database.md               schema, constraints, migrations
│   ├── api.md                    endpoint contract
│   ├── deployment.md             Supabase + Render + Vercel
│   └── ai-generation.md          generation pipeline and validation
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/versions/         schema migrations
│   ├── app/
│   │   ├── main.py               app factory, middleware, routers
│   │   ├── core/                 config, logging, auth, rate limit, clock, errors
│   │   ├── db/                   session + SQLAlchemy models
│   │   ├── schemas/              Pydantic request/response models
│   │   ├── api/routes/           HTTP layer
│   │   ├── services/             exam engine: attempts, grading, integrity, export, email
│   │   ├── ai/                   AIProvider, GeminiProvider, validation, dedupe
│   │   ├── email/                EmailProvider, ResendEmailProvider
│   │   └── storage/              Supabase Storage
│   └── tests/                    pytest
└── frontend/
    ├── vercel.json
    └── src/
        ├── lib/                  api client, supabase client, auth context
        ├── components/           layout, ui primitives
        ├── pages/teacher/        dashboard, quizzes, questions, variants, results
        └── pages/student/        public landing, exam runner, submitted
```

## Local development

Prerequisites: Python 3.11+, Node 20+, Docker (optional), a Supabase project for
Auth.

### Backend

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # fill in real values
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

`http://localhost:8000/api/health` should return `{"status":"ok"}` and
`/api/health/ready` should report `database: ok`. Interactive API docs are at
`http://localhost:8000/docs` outside production.

### Backend in Docker (API + local PostgreSQL)

```bash
docker compose up --build
docker compose exec api alembic upgrade head
```

This runs against the compose PostgreSQL, not Supabase, so you can develop
without touching the real database.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev          # http://localhost:5173
```

### Tests

```bash
cd backend && pytest -q          # unit + API + security tests (no network, no paid APIs)
cd frontend && npm run test:e2e  # Playwright
```

Tests use the `stub` AI and email providers, so no test run ever calls a paid
API.

## Environment variables

Backend (`backend/.env`, Render dashboard in production) — full list with
comments in [.env.example](.env.example):

`ENVIRONMENT`, `LOG_LEVEL`, `PORT`, `DATABASE_URL`, `ALEMBIC_DATABASE_URL`,
`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_JWT_SECRET`, `GEMINI_API_KEY`, `RESEND_API_KEY`, `EMAIL_FROM`,
`FRONTEND_URL`, `CORS_ORIGINS`, rate-limit and exam-engine tuning.

Frontend (`frontend/.env`, Vercel) — public by construction:
`VITE_API_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`.

`SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY` and `RESEND_API_KEY` are backend
only and must never appear in a `VITE_`-prefixed variable.

## Implementation phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Infrastructure: repo, backend, frontend, Docker, Supabase, migrations, auth, dashboard shell | done |
| 2 | Quiz engine: quiz CRUD, question families, variants, teacher review, AI generation, publish lifecycle | done |
| 3 | Student assessment engine: public link, attempt creation, assignment, server timer, answers, integrity events, auto-submit, grading, result | done |
| 4 | Results: teacher dashboard, attempt timeline, XLSX export, Resend email | next |
| 5 | Live Gemini verification and prompt iteration | pending a credential |
| 6 | Hardening: deployment, load behaviour, operational runbook | |

## Documentation

[Architecture](docs/architecture.md) · [Security](docs/security.md) ·
[Database](docs/database.md) · [API](docs/api.md) ·
[Deployment](docs/deployment.md) · [AI generation](docs/ai-generation.md)
