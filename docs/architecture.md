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

Publishing is refused unless the quiz has at least `question_count` families and
every one of them has at least one `APPROVED` variant.

## 5. Question generation

```mermaid
flowchart TD
    A["POST /api/questions/:id/generate-variants"] --> B["Quiz status becomes GENERATING"]
    B --> C{"question_type"}
    C -- NUMERICAL --> D["Deterministic parameter generation, backend computes the answer"]
    C -- "MCQ / TRUE_FALSE" --> E["AIProvider.generate_question_variants()"]
    D --> E2["AI may reword only, numeric answer stays backend-computed"]
    E --> F["Strict JSON parse, no silent repair"]
    E2 --> F
    F --> G{"Schema valid?"}
    G -- no --> X["Rejected plus structured log VARIANT_GENERATION_FAILED"]
    G -- yes --> H["Quality checks: four distinct options, index in range, topic, difficulty, explanation"]
    H --> I["Duplicate detection against existing variants"]
    I --> J["Persist variant with validation_status = PENDING"]
    J --> K["Teacher reviews: APPROVE / REJECT / REGENERATE"]
    K --> L["APPROVED variants enter the pool"]
    L --> M["Only APPROVED variants can be served to students"]
```

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
    S->>A: POST /api/attempts/start with public_code, name, email
    A->>DB: SELECT quiz WHERE public_code AND status = PUBLISHED
    A->>DB: One-attempt check via unique index
    A->>A: seed = secrets.randbits(63)
    A->>A: started_at = server now, expires_at = started_at + duration
    A->>DB: Pick one APPROVED variant per family, seeded PRNG, shuffle order and options
    A->>DB: INSERT attempt_questions (the frozen exam)
    A-->>S: attempt_id, attempt_token, questions without correct_option
```

The exam is **frozen at creation**: `attempt_questions` rows are never
regenerated. Reloading the page replays the same paper; a different student
starting from the same URL gets a different draw because the seed differs.

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
