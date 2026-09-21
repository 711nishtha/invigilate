# Architecture

## 1. Guiding principle

**The browser is an untrusted client.** Every decision below follows from that.
The React app renders state and reports signals; it never decides what a student
sees, how long they have, or what they scored. The FastAPI backend is the only
authority, and Supabase PostgreSQL is the only system of record.

## 2. Component overview

| Concern | Technology | Where it runs |
|---|---|---|
| Teacher + student UI | React 19, TypeScript, Vite, Tailwind | Vercel (static CDN) |
| API / exam engine | FastAPI, Pydantic v2, SQLAlchemy 2, Alembic | Render (Docker) |
| Relational store | Supabase PostgreSQL | Supabase |
| Identity | Supabase Auth (JWT) | Supabase |
| Files (evidence, exports) | Supabase Storage | Supabase |
| Question variant generation | Gemini API via `AIProvider` | called from backend only |
| Result delivery | Resend API via `EmailProvider` | called from backend only |
| Spreadsheet export | openpyxl | backend, in-memory |

### 2.1 System architecture

```mermaid
graph TB
    subgraph Browser["Untrusted client"]
        T["Teacher SPA /teacher/*"]
        S["Student exam runner /q/:code"]
    end

    subgraph Vercel["Vercel"]
        FE["React + Vite static bundle"]
    end

    subgraph Render["Render (Docker)"]
        API["FastAPI authoritative exam engine"]
        RT["Routers: authz + validation"]
        SVC["Services: attempt, grading, integrity, export, email"]
        PROV["Providers: AIProvider / EmailProvider / Storage"]
    end

    subgraph Supabase["Supabase"]
        AUTH["Auth (JWT issuer)"]
        PG[("PostgreSQL system of record")]
        ST["Storage: evidence / exports"]
    end

    GEM["Gemini API"]
    RES["Resend API"]

    T --> FE
    S --> FE
    FE -->|"HTTPS + Bearer JWT"| API
    T -.->|"sign in / refresh"| AUTH
    API -->|"verify JWT signature"| AUTH
    API --> RT
    RT --> SVC
    SVC --> PROV
    SVC -->|"SQLAlchemy, parameterized"| PG
    PROV -->|"service-role key"| ST
    PROV -->|"GEMINI_API_KEY"| GEM
    PROV -->|"RESEND_API_KEY"| RES

    classDef untrusted fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    classDef trusted fill:#dcfce7,stroke:#15803d,color:#14532d
    class T,S,FE untrusted
    class API,RT,SVC,PROV,PG trusted
```

Red = untrusted, green = the security boundary. Secrets exist only inside the
green zone. The frontend receives the Supabase **anon** key and nothing else.

## 3. Request authority model

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as FastAPI
    participant DB as PostgreSQL

    B->>A: Request + Authorization Bearer supabase jwt
    A->>A: Verify signature, exp, aud (local HS256 verify)
    A->>DB: Load user row by supabase_user_id
    A->>A: Read role from DB row, never from request body
    A->>DB: Check ownership (quiz.owner_id / attempt token)
    A->>DB: Check server clock against attempt.expires_at
    A-->>B: Only then act
```

Role is read from the `users` table, not from the JWT payload or request body.
A JWT proves *who*; the database decides *what they may do*.

## 4. Quiz creation

```mermaid
flowchart TD
    A["Teacher signs in (Supabase Auth)"] --> B["POST /api/auth/profile upserts users row role=TEACHER"]
    B --> C["POST /api/quizzes with title, duration, question_count, security_level, max_integrity_violations"]
    C --> D["Quiz status = DRAFT, public_code = NULL"]
    D --> E["POST /api/quizzes/:id/questions creates one QuestionFamily per original question"]
    E --> F{"Enough approved variants?"}
    F -- no --> G["Generate and review variants (see section 5)"]
    G --> F
    F -- yes --> H["POST /api/quizzes/:id/publish"]
    H --> I["Non-sequential public_code, status = PUBLISHED, published_at set"]
```

Publishing is refused unless the quiz has at least `question_count` families
with an assignable variant, and every assignable variant passes structural
checks (distinct options, exactly one correct answer in range, a
backend-computed numeric answer where applicable). `GET /readiness` returns the
same blockers without attempting to publish, so the UI can explain them before
the teacher presses the button.

## 5. Question generation

```mermaid
flowchart TD
    A["POST /api/questions/:id/generate-variants"] --> C{"question_type"}
    C -- NUMERICAL --> D["Deterministic parameter generation, backend computes the answer"]
    C -- "MCQ / TRUE_FALSE" --> E["AIProvider.generate_question_variants()"]
    D --> E2["AI may reword only, numeric answer stays backend-computed"]
    E --> F["Strict JSON parse, no silent repair"]
    E2 --> F
    F --> G{"Schema valid?"}
    G -- no --> X["Rejected with a recorded reason, nothing stored"]
    G -- yes --> H["Quality checks: distinct options, index in range, one correct answer, metadata"]
    H --> I["Duplicate detection: exact hash plus near-duplicate similarity"]
    I --> J["Persist with status = VALIDATED"]
    J --> K["Teacher reviews: APPROVE / REJECT / REGENERATE"]
    K --> L["APPROVED, and PUBLISHED when the quiz goes live"]
    L --> M["Only assignable variants can be served: APPROVED or PUBLISHED, not superseded"]
```


### 5.1 Variant lifecycle

```mermaid
stateDiagram-v2
    [*] --> GENERATED: provider returned a candidate
    GENERATED --> VALIDATED: passed deterministic validation
    GENERATED --> REJECTED: failed validation
    VALIDATED --> APPROVED: a teacher approved it
    VALIDATED --> REJECTED: a teacher refused it
    APPROVED --> PUBLISHED: its quiz was published
    APPROVED --> REJECTED: withdrawn before publication
    PUBLISHED --> REJECTED: withdrawn from future papers
    REJECTED --> VALIDATED: sent back for review
```

Only a human performs `VALIDATED -> APPROVED`. The transition table in
`services/variant_lifecycle.py` has no path from `GENERATED` to `APPROVED`, and
a test asserts it, so approval cannot become automatic by configuration.

`APPROVED` and `PUBLISHED` variants are **content-immutable**: editing one
creates a new version and marks the original superseded, so a question already
served to a student never changes. See
[database.md](./database.md#variant-immutability-and-versioning).

10 originals x 5 variants = a **pool of 50**, not four fixed papers. See
[ai-generation.md](./ai-generation.md).

## 6. Attempt creation — one link, different exams

```mermaid
sequenceDiagram
    participant S as Student browser
    participant A as FastAPI
    participant DB as PostgreSQL

    S->>A: GET /api/public/quizzes/{public_code}
    A-->>S: title, duration, question_count, rules only
    S->>A: POST /api/public/quizzes/{code}/attempts with name, email
    A->>DB: SELECT quiz WHERE public_code AND status = PUBLISHED
    A->>DB: One-attempt check via unique index
    A->>A: seed = secrets.randbits(63)
    A->>A: started_at = server now, expires_at = started_at + duration
    A->>DB: Sample families, then one assignable variant each, seeded PRNG
    A->>A: Shuffle question order and option order from the same seed
    A->>DB: INSERT attempt_questions (the frozen exam)
    A-->>S: attempt_id, attempt_token, questions without correct_option
```

The exam is **frozen at creation**: `attempt_questions` rows are never
regenerated. Reloading the page replays the same paper; a different student
starting from the same URL gets a different draw because the seed differs.

Families are sampled first and one variant is drawn per family, so a student is
never given two variants of the same question. `ORDER BY random() LIMIT n` is
deliberately not used — it would do exactly that. The seed comes from
`secrets.randbits(63)` server-side; no request field can supply one, and the
whole paper is reproducible from the seed stored on the attempt row.

No AI provider is called here. Generation happens during quiz preparation, so an
exam never depends on a third party being reachable.

## 7. Exam lifecycle

```mermaid
stateDiagram-v2
    [*] --> IN_PROGRESS: POST /attempts/start
    IN_PROGRESS --> IN_PROGRESS: POST /answer (upsert)
    IN_PROGRESS --> IN_PROGRESS: POST /event below threshold
    IN_PROGRESS --> SUBMITTED: POST /submit by student
    IN_PROGRESS --> AUTO_SUBMITTED: integrity severity over threshold
    IN_PROGRESS --> TIME_EXPIRED: server now reaches expires_at
    SUBMITTED --> [*]
    AUTO_SUBMITTED --> [*]
    TIME_EXPIRED --> [*]
```

`SUBMITTED`, `AUTO_SUBMITTED` and `TIME_EXPIRED` are terminal. Any later request
returns the stored result; an attempt is never reopened.

Expiry is enforced lazily on every protected request *and* eagerly at submit — a
student who closes the laptop still ends up with a graded, finalized
`TIME_EXPIRED` attempt the next time anything touches it.

## 8. Submission

```mermaid
sequenceDiagram
    participant S as Student
    participant A as FastAPI
    participant DB as PostgreSQL

    S->>A: POST /api/attempts/{id}/submit
    A->>DB: BEGIN then SELECT FOR UPDATE on the attempt row
    alt already terminal
        DB-->>A: status is SUBMITTED, AUTO_SUBMITTED or TIME_EXPIRED
        A-->>S: 200 with the existing result, no re-grade
    else still in progress
        A->>DB: Grade each answer against the variant key
        A->>DB: Persist score, max_score, percentage, submitted_at, status
        A->>DB: COMMIT and release the lock
        A-->>S: 200 with result, score hidden unless show_score_after_submission
    end
```

A second concurrent `POST /submit` blocks on the row lock, then observes the
terminal state and returns the same result. One finalization, one email.

## 9. Export and email

```mermaid
flowchart LR
    A["POST /api/quizzes/:id/export"] --> B["Query attempts, answers, integrity events, variant stats"]
    B --> C["openpyxl workbook built in memory, four sheets"]
    C --> D{"Delivery"}
    D -- download --> E["StreamingResponse xlsx"]
    D -- persist --> F["Supabase Storage exports bucket"]
    F --> G["POST /api/quizzes/:id/email-results"]
    G --> H["EmailProvider.send with attachment"]
    H --> I{"Sent?"}
    I -- yes --> J["log EMAIL_SENT"]
    I -- no --> K["log EMAIL_FAILED, result already persisted, teacher can resend"]
```

Excel is an export format. PostgreSQL is the database. An email failure never
costs a result.

## 10. Backend layering

```
app/api/routes/    HTTP shape, authn/authz dependencies, Pydantic in/out
app/services/      Business rules. The only place that mutates exam state.
                     ownership.py            teacher tenant isolation
                     quiz_service.py         quiz CRUD, publish/unpublish/close
                     publish_rules.py        publish eligibility and blockers
                     family_service.py       question family CRUD
                     variant_service.py      generation, editing, supersession
                     variant_lifecycle.py    the variant state machine
                     selection.py            eligibility + seeded paper planning
                     attempt_access.py       student attempt authorization
                     attempt_service.py      attempt creation, paper assembly,
                                             sanitized delivery, answer saving
                     integrity_policy.py     severity table and threshold policy
                     attempt_finalization.py idempotent finalization
                     grading.py              scoring, at finalization only
app/db/models/     SQLAlchemy ORM, enums, constraints
app/ai/            AIProvider abstraction, GeminiProvider, validation, dedupe
app/email/         EmailProvider abstraction, ResendEmailProvider
app/storage/       Supabase Storage client
app/core/          config, logging, auth, rate limiting, server clock, errors
```

Routers never mutate exam state directly; services never import FastAPI. That
keeps the exam engine unit-testable without HTTP.

## 11. What this architecture deliberately does not include

No Kubernetes, no message broker, no Redis, no separate AI/grading/export
services. Variant generation is the only slow operation and it happens during
quiz preparation, not during an exam, so a background task inside the API
process is sufficient. A queue would be justified only by real generation
volume, and that threshold has not been reached.
