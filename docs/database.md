# Database

Supabase PostgreSQL is the single system of record. Every schema change ships as
an Alembic migration; nothing is altered by hand in the Supabase SQL editor.

All primary keys are `uuid` generated application-side (`uuid4`) so that inserts
do not depend on a database extension. All timestamps are `timestamptz` and are
written from the **database/server clock**, never from a client-supplied value.

## Entity relationships

```mermaid
erDiagram
    users ||--o{ quizzes : owns
    quizzes ||--o{ question_families : contains
    question_families ||--o{ question_variants : "has variants"
    quizzes ||--o{ attempts : "is attempted as"
    users ||--o{ attempts : "may be linked to"
    attempts ||--o{ attempt_questions : "frozen exam"
    question_variants ||--o{ attempt_questions : "served as"
    attempt_questions ||--o| answers : "answered by"
    attempts ||--o{ integrity_events : logs
```

## Enumerated types

Enums are stored as PostgreSQL native enum types so the database rejects invalid
states outright.

| Type | Values |
|---|---|
| `user_role` | `TEACHER`, `STUDENT`, `ADMIN` |
| `quiz_status` | `DRAFT`, `GENERATING`, `READY`, `PUBLISHED`, `CLOSED`, `ARCHIVED` |
| `security_level` | `STANDARD`, `SECURE`, `STRICT` |
| `question_type` | `MCQ`, `NUMERICAL`, `TRUE_FALSE`, `SHORT_ANSWER` |
| `generation_status` | `AI_GENERATED`, `DETERMINISTIC`, `MANUAL`, `FAILED` |
| `variant_status` | `GENERATED`, `VALIDATED`, `APPROVED`, `PUBLISHED`, `REJECTED` |
| `attempt_status` | `IN_PROGRESS`, `SUBMITTED`, `AUTO_SUBMITTED`, `TIME_EXPIRED`, `ABANDONED` |
| `integrity_status` | `CLEAN`, `WARNING`, `FLAGGED` |
| `integrity_event_type` | 20 values, listed in [security.md](./security.md) |
| `integrity_action` | `NONE`, `LOGGED`, `WARNING_ISSUED`, `AUTO_SUBMITTED` |

## Tables

### users

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `supabase_user_id` | varchar(64) NOT NULL UNIQUE | `sub` claim of the Supabase JWT |
| `name` | text NOT NULL | |
| `email` | varchar(320) NOT NULL UNIQUE | lowercased on write, so uniqueness is case-insensitive without the citext extension |
| `role` | `user_role` NOT NULL DEFAULT `TEACHER` | authoritative role, never taken from the client |
| `created_at` | timestamptz NOT NULL DEFAULT now() | |

### quizzes

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `owner_id` | uuid FK -> users.id, ON DELETE CASCADE | |
| `public_code` | varchar(16) UNIQUE NULL | non-sequential, assigned at publish |
| `title` | text NOT NULL | |
| `description` | text NULL | |
| `duration_seconds` | int NOT NULL, CHECK 60..28800 | |
| `question_count` | int NOT NULL, CHECK 1..200 | how many questions each student receives |
| `security_level` | `security_level` NOT NULL DEFAULT `SECURE` | |
| `max_integrity_violations` | int NOT NULL DEFAULT 2, CHECK >= 0 | cumulative **severity** budget |
| `randomization_enabled` | bool NOT NULL DEFAULT true | |
| `allow_backtracking` | bool NOT NULL DEFAULT true | |
| `show_score_after_submission` | bool NOT NULL DEFAULT false | |
| `status` | `quiz_status` NOT NULL DEFAULT `DRAFT` | |
| `coverage_config_json` | jsonb NOT NULL DEFAULT '{}' | optional publish requirements: `required_topics`, `required_difficulties`, `required_objectives` |
| `created_at` / `published_at` | timestamptz | |

Indexes: `(owner_id, created_at DESC)`, unique `(public_code)`.

### question_families

One row per original question written by the teacher. A family is the unit that
a student's exam draws **one** variant from.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `quiz_id` | uuid FK -> quizzes.id, ON DELETE CASCADE | |
| `original_question` | text NOT NULL | |
| `question_type` | `question_type` NOT NULL | |
| `topic` | text NULL | |
| `difficulty` | text NULL | `easy` / `medium` / `hard` |
| `learning_objective` | text NULL | |
| `generation_config_json` | jsonb NOT NULL DEFAULT '{}' | variant count, numeric template, parameter ranges |
| `position` | int NOT NULL | teacher-facing ordering |
| `created_at` | timestamptz | |

Index: `(quiz_id, position)`.

### question_variants

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `family_id` | uuid FK -> question_families.id, ON DELETE CASCADE | |
| `question_text` | text NOT NULL | |
| `options_json` | jsonb NULL | array of option strings, canonical order |
| `correct_option` | int NULL | index into `options_json`. **Never leaves the backend.** |
| `numeric_answer_json` | jsonb NULL | `{value, tolerance, unit}` for NUMERICAL |
| `explanation` | text NULL | |
| `generation_model` | text NULL | e.g. `gemini-2.5-flash` |
| `generation_prompt_version` | text NULL | prompt versioning for auditability |
| `generation_status` | `generation_status` NOT NULL | |
| `validation_status` | `variant_status` NOT NULL DEFAULT `GENERATED` | lifecycle state; only `APPROVED`/`PUBLISHED` are assignable |
| `content_hash` | varchar(64) NOT NULL | normalized SHA-256, backs duplicate detection |
| `rejection_reason` | text NULL | why a teacher refused it |
| `validation_errors_json` | jsonb NULL | why deterministic validation refused it; teacher-only |
| `version` | int NOT NULL DEFAULT 1 | incremented by each supersession |
| `superseded_by_id` | uuid FK -> question_variants.id NULL | set when an edit created a newer version |
| `approved_at` | timestamptz NULL | audit trail |
| `approved_by_id` | uuid FK -> users.id NULL | who let this question through |
| `created_at` | timestamptz | |

Constraints:
- `UNIQUE (family_id, content_hash)` — the database itself refuses exact duplicates.
- `CHECK (correct_option IS NULL OR correct_option >= 0)`.
- Index `(family_id, validation_status)` — the hot path when building an exam.

Per-type shape (an MCQ must carry four options and a key) is enforced in the
validation pipeline rather than by a CHECK, because `question_type` lives on the
family rather than on the variant row.

### attempts

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `quiz_id` | uuid FK -> quizzes.id | |
| `student_user_id` | uuid FK -> users.id NULL | set when a student is signed in |
| `student_name` | text NOT NULL | |
| `student_email` | varchar(320) NOT NULL | lowercased on write; the identity the one-attempt rule keys on |
| `attempt_token_hash` | varchar(64) NOT NULL | SHA-256 of the bearer token given to anonymous students |
| `started_at` | timestamptz NOT NULL | **server** clock |
| `expires_at` | timestamptz NOT NULL | `started_at + duration_seconds` |
| `submitted_at` | timestamptz NULL | |
| `status` | `attempt_status` NOT NULL DEFAULT `IN_PROGRESS` | |
| `seed` | bigint NOT NULL | cryptographically generated, drives the draw |
| `score` / `max_score` | numeric(8,2) NULL | server-computed only |
| `percentage` | numeric(5,2) NULL | |
| `integrity_violation_count` | int NOT NULL DEFAULT 0 | cumulative severity, not event count |
| `integrity_status` | `integrity_status` NOT NULL DEFAULT `CLEAN` | |
| `results_emailed_at` | timestamptz NULL | suppresses duplicate result emails |
| `created_at` | timestamptz | |

One-attempt enforcement: `UNIQUE (quiz_id, student_email)`. The database, not
the application, is the thing that makes a second attempt impossible — so a race
between two simultaneous `POST /attempts/start` calls ends in an integrity error
rather than two papers.

### attempt_questions

The frozen exam. Written once at attempt creation and never regenerated.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `attempt_id` | uuid FK -> attempts.id, ON DELETE CASCADE | |
| `variant_id` | uuid FK -> question_variants.id | |
| `position` | int NOT NULL | 0-based order for this student |
| `options_order_json` | jsonb NULL | permutation of canonical option indices |

Constraints: `UNIQUE (attempt_id, position)`, `UNIQUE (attempt_id, variant_id)`.

`options_order_json` is the mapping from displayed position to canonical index.
The student sends back a **displayed** index; the backend maps it through this
permutation before comparing with `correct_option`. The permutation lives in the
database, so a student cannot infer the key by replaying a request.

### answers

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `attempt_question_id` | uuid FK UNIQUE -> attempt_questions.id, ON DELETE CASCADE | one answer per question, upserted |
| `selected_answer` | jsonb NOT NULL | `{displayed_index}` or `{value}` or `{text}` |
| `is_correct` | bool NULL | **null until grading**; never written from a request |
| `answered_at` | timestamptz NOT NULL | server clock |

`is_correct` is deliberately left null while the exam runs. Grading happens once,
at finalization, so a mid-exam database read cannot leak correctness.

### integrity_events

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `attempt_id` | uuid FK -> attempts.id, ON DELETE CASCADE | |
| `event_type` | `integrity_event_type` NOT NULL | |
| `severity` | int NOT NULL | assigned **server-side** from the policy table |
| `occurred_at` | timestamptz NOT NULL | server receipt time |
| `client_reported_at` | timestamptz NULL | kept for forensics, never trusted |
| `metadata_json` | jsonb NOT NULL DEFAULT '{}' | size-capped |
| `action_taken` | `integrity_action` NOT NULL DEFAULT `LOGGED` | |

Index: `(attempt_id, occurred_at)` — powers the attempt timeline view.

## Integrity and concurrency rules encoded in the schema

1. `UNIQUE (quiz_id, student_email)` on `attempts` — one attempt per student.
2. `UNIQUE (attempt_id, position)` on `attempt_questions` — a stable paper.
3. `UNIQUE (attempt_question_id)` on `answers` — an upsert, never a duplicate row.
4. `UNIQUE (family_id, content_hash)` on `question_variants` — no duplicate variants.
5. `SELECT ... FOR UPDATE` on `attempts` during submission — exactly one finalization.


### Variant immutability and versioning

**Decision: an approved variant is never rewritten. Editing supersedes it.**

The rule that forces this: *a question already used by an attempt must never
silently change underneath that attempt.* A student who sat question V3 must be
able to have their paper re-read, disputed and re-marked against exactly the
text they saw.

| Variant state | Editing behaviour |
|---|---|
| `GENERATED`, `VALIDATED` | Edited **in place**. Nothing has been shown to anyone, so there is nothing to protect. |
| `APPROVED`, `PUBLISHED`, or referenced by any `attempt_questions` row | **Superseded**: a new row is inserted with `version = old.version + 1`, and the old row's `superseded_by_id` points at it. |

Consequences, all enforced in `services/variant_service.py` and tested in
`tests/test_variant_lifecycle.py`:

- The original row keeps its text, options and key forever. `attempt_questions`
  references it by id, so historical papers are unaffected.
- A superseded variant is no longer **assignable** even while `APPROVED`, so new
  papers draw the newer version. `QuestionVariant.is_assignable` is the single
  definition of eligibility.
- A replacement enters at `VALIDATED`, not `APPROVED`: a new version is a new
  question and is approved on its own merits.
- A variant referenced by an attempt can never be deleted. `ON DELETE RESTRICT`
  on `attempt_questions.variant_id` enforces this in the database; the service
  checks first so the teacher gets an explanation rather than a constraint error.
- A published variant can still be **rejected**, which withdraws it from future
  papers while changing nothing about papers already built.

The alternative — forbidding edits outright — was rejected because teachers do
need to fix a typo, and forcing "reject and rewrite from scratch" loses the
provenance chain that `version` and `superseded_by_id` preserve.

## PostgreSQL-specific behaviour

The unit test suite runs on in-memory SQLite for speed. PostgreSQL is the only
supported production database, and the list below is everything the application
relies on PostgreSQL to do. Each entry says what breaks if it is absent and
where it is tested.

| # | Reliance | Used by | If absent | Tested in |
|---|---|---|---|---|
| 1 | `SELECT ... FOR UPDATE` row locking | `attempt_access.resolve_attempt(for_update=True)` | Two concurrent submits could both grade and both email | `integration/test_postgres_concurrency.py::TestRowLocking` |
| 2 | `READ COMMITTED` re-read after a lock is granted | Idempotent submission | The second submitter would act on a stale `IN_PROGRESS` row | `TestConcurrentSubmission` |
| 3 | `UNIQUE (quiz_id, student_email)` under concurrent insert | One-attempt rule | Two simultaneous starts would both create a paper | `TestUniqueConstraintsUnderRace` |
| 4 | `UNIQUE (attempt_question_id)` on `answers` | Answer upsert | Duplicate answer rows; grading would double-count | `TestUniqueConstraintsUnderRace` |
| 5 | `UNIQUE (family_id, content_hash)` | Variant dedup | Duplicate variants could reach a paper | `test_models.py` (logic), integration (race) |
| 6 | Native enum types | Every status column | An invalid status could be written by raw SQL or a bad migration | `TestNativeEnums` |
| 7 | `jsonb` columns | `options_json`, `generation_config_json`, `metadata_json` | No indexed JSON querying for question statistics | exercised throughout |
| 8 | `timestamptz` | All timestamps | Timezone-naive comparison; exam deadlines could shift by hours | `test_review_invariants.py::TestServerClockAuthority` |
| 9 | `bigint` for `attempts.seed` | Seeded draw | A 63-bit seed would overflow a 32-bit integer | `test_core.py::TestSeeds` |
| 10 | Transactional DDL | Alembic migrations | A failed migration could leave a half-applied schema | manual, on deploy |
| 11 | Enum type replacement (`ALTER TYPE ... USING`) | migration `0002_variant_lifecycle` | The variant lifecycle could not be migrated without data loss | manual, on deploy |

### Transaction boundaries

One transaction per request, opened by the `get_db` dependency and rolled back
on any exception. Within a submission the boundary is:

```
BEGIN
  SELECT ... FROM attempts WHERE attempt_token_hash = :hash FOR UPDATE   -- lock
  (check terminal state; if terminal, return the stored result and COMMIT)
  UPDATE answers  SET is_correct = ...                                   -- grade
  UPDATE attempts SET score, max_score, percentage, status, submitted_at
COMMIT                                                                    -- lock released
```

`finalize_attempt` deliberately does **not** take the lock itself. The lock must
be acquired by the same read that decided the attempt was still open, otherwise
there is a window between the decision and the write in which another request
can finalize.

### Why SQLite is still used for unit tests

SQLite serializes writes with a database-level lock, so the state-machine tests
in `test_finalization.py` produce the right answers there — but they prove the
*logic*, not the *concurrency*. Anything whose correctness depends on row
locking, constraint enforcement under a race, or native enums is marked
`requires_postgres` and skipped with a stated reason when `TEST_DATABASE_URL` is
not a PostgreSQL URL. A green unit run is therefore never mistaken for a green
concurrency run.

```bash
docker compose up -d db
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/secure_quiz     pytest tests/integration -v
```

### pgBouncer

The application connects through Supabase's transaction pooler (port 6543) with
`prepare_threshold=None`, because transaction-mode pooling cannot hold
server-side prepared statements. Migrations use the direct connection (port
5432): DDL must not run through the pooler.

Session-level constructs (advisory locks, `LISTEN`/`NOTIFY`, temporary tables)
are unavailable through the pooler and are therefore not used anywhere in the
exam engine. Row locks are transaction-scoped and work normally.

## Migrations

```bash
cd backend
alembic revision --autogenerate -m "describe change"   # review the diff by hand
alembic upgrade head
alembic downgrade -1                                    # every migration is reversible
```

`alembic/env.py` reads `ALEMBIC_DATABASE_URL` when set (Supabase direct
connection, port 5432) and falls back to `DATABASE_URL`. Migrations must not run
through the pgBouncer transaction pooler.
