"""Class-sized load test: ~200 students starting at once.

Runs the real HTTP stack in-process against a real PostgreSQL, so the exam
engine's locking and constraints are exercised rather than mocked.

    docker compose up -d db
    LOAD_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/secure_quiz \\
        ./.venv/Scripts/python.exe scripts/load_test.py --students 200

It measures latency and error rate, but its real job is **correctness under
load**: every student must get exactly one attempt, a complete paper, and one
finalization with a score. Throughput numbers from a laptop mean little;
"200 students, 200 attempts, 0 duplicates, every answer graded" means a lot.

Reading the latency numbers honestly: this harness drives the app in-process
through TestClient, so every request shares one Python process and one GIL.
Raising --workers past ~8 inflates per-request latency without improving
throughput, which is client-side contention rather than server or database
saturation. Measured on one machine: 200 students at 32 workers gave
start p50 845ms / 39 req/s, and the same run at 8 workers gave 166ms / 47 req/s.

Treat the latency figures as indicative only. Production latency needs an
out-of-process generator (k6, locust) against a deployed instance. What this
harness does establish is correctness under concurrency, which is the part that
cannot be inferred from a single-user test.

Exit codes: 0 all correctness assertions held, 1 something was wrong.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AI_PROVIDER", "stub")
os.environ.setdefault("EMAIL_PROVIDER", "stub")
os.environ.setdefault("STORAGE_PROVIDER", "stub")
os.environ.setdefault("SUPABASE_JWT_SECRET", "load-test-secret-not-a-real-secret")
os.environ.setdefault("SUPABASE_URL", "https://load.test")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "load-test")

DATABASE_URL = os.environ.get("LOAD_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
    print(
        "LOAD_DATABASE_URL must be a PostgreSQL URL.\n"
        "  docker compose up -d db\n"
        "  LOAD_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/secure_quiz "
        "python scripts/load_test.py"
    )
    raise SystemExit(2)
os.environ["DATABASE_URL"] = DATABASE_URL

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.ai.factory import set_ai_provider  # noqa: E402
from app.ai.stub import StubProvider  # noqa: E402
from app.db.models import (  # noqa: E402
    Answer,
    Attempt,
    AttemptQuestion,
    AttemptStatus,
    Base,
    QuestionFamily,
    QuestionVariant,
    Quiz,
    QuizStatus,
    User,
    UserRole,
    VariantStatus,
)
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402


@dataclass
class Timings:
    name: str
    samples: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, seconds: float) -> None:
        self.samples.append(seconds * 1000)

    def summary(self) -> str:
        if not self.samples:
            return f"{self.name:<22} no samples  errors={self.errors}"
        ordered = sorted(self.samples)
        p50 = statistics.median(ordered)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return (
            f"{self.name:<22} n={len(ordered):<5} "
            f"p50={p50:7.1f}ms p95={p95:7.1f}ms max={ordered[-1]:7.1f}ms "
            f"errors={self.errors}"
        )


def seed_quiz(session_factory, *, questions: int, variants: int) -> tuple[uuid.UUID, str]:
    """A published quiz with a real variant pool, created directly."""
    session = session_factory()
    teacher = User(
        supabase_user_id=f"load-{uuid.uuid4().hex[:8]}",
        name="Load Test Teacher",
        email=f"load-{uuid.uuid4().hex[:8]}@example.edu",
        role=UserRole.TEACHER,
    )
    session.add(teacher)
    session.flush()

    code = uuid.uuid4().hex[:6].upper().translate(str.maketrans("OIL01", "ABCDE"))
    quiz = Quiz(
        owner_id=teacher.id,
        title="Load Test Midterm",
        duration_seconds=3600,
        question_count=questions,
        status=QuizStatus.PUBLISHED,
        public_code=code,
        max_integrity_violations=5,
        show_score_after_submission=True,
    )
    session.add(quiz)
    session.flush()

    for position in range(questions):
        family = QuestionFamily(
            quiz_id=quiz.id,
            original_question=f"Load question {position + 1}?",
            question_type="MCQ",
            topic="Load",
            difficulty="medium",
            position=position,
        )
        session.add(family)
        session.flush()
        for index in range(variants):
            session.add(
                QuestionVariant(
                    family_id=family.id,
                    question_text=f"Q{position + 1} variant {index + 1}: which option is right?",
                    options_json=[f"opt A{index}", f"opt B{index}", f"opt C{index}", f"opt D{index}"],
                    correct_option=index % 4,
                    content_hash=uuid.uuid4().hex,
                    validation_status=VariantStatus.PUBLISHED,
                )
            )
    session.commit()
    quiz_id = quiz.id
    session.close()
    return quiz_id, code


def run(students: int, questions: int, variants: int, workers: int) -> int:
    engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=20, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    print(f"database : {DATABASE_URL.split('@')[-1]}")
    print(f"students : {students}   questions: {questions}   variants/family: {variants}")
    print(f"workers  : {workers}\n")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    set_ai_provider(StubProvider())

    quiz_id, code = seed_quiz(session_factory, questions=questions, variants=variants)

    app = create_app()

    def db_override():  # type: ignore[no-untyped-def]
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = db_override

    # Rate limits are a production control, not a load-test subject: the whole
    # class shares one IP here, which is not how a real classroom looks.
    from app.core.rate_limit import limiter

    limiter.enabled = False

    timings = {
        name: Timings(name)
        for name in ("start", "questions", "answers", "integrity", "submit", "result")
    }
    failures: list[str] = []

    def sit_exam(index: int) -> None:
        client = TestClient(app)
        email = f"student{index}@loadtest.example.edu"

        began = time.perf_counter()
        response = client.post(
            f"/api/public/quizzes/{code}/attempts",
            json={"student_name": f"Student {index}", "student_email": email},
        )
        timings["start"].record(time.perf_counter() - began)
        if response.status_code != 201:
            timings["start"].errors += 1
            failures.append(f"start {index}: {response.status_code} {response.text[:120]}")
            return

        body = response.json()
        attempt_id, token = body["attempt_id"], body["attempt_token"]
        headers = {"X-Attempt-Token": token}

        began = time.perf_counter()
        paper = client.get(f"/api/student/attempts/{attempt_id}/questions", headers=headers)
        timings["questions"].record(time.perf_counter() - began)
        if paper.status_code != 200:
            timings["questions"].errors += 1
            failures.append(f"questions {index}: {paper.status_code}")
            return

        served = paper.json()["questions"]
        if len(served) != questions:
            failures.append(f"paper {index}: got {len(served)} questions, expected {questions}")

        for question in served:
            began = time.perf_counter()
            saved = client.post(
                f"/api/student/attempts/{attempt_id}/answers",
                headers=headers,
                json={
                    "attempt_question_id": question["attempt_question_id"],
                    "selected_answer": {"displayed_index": index % 4},
                },
            )
            timings["answers"].record(time.perf_counter() - began)
            if saved.status_code != 200:
                timings["answers"].errors += 1
                failures.append(f"answer {index}: {saved.status_code}")

        began = time.perf_counter()
        event = client.post(
            f"/api/student/attempts/{attempt_id}/integrity-events",
            headers=headers,
            json={"event_type": "TAB_HIDDEN"},
        )
        timings["integrity"].record(time.perf_counter() - began)
        if event.status_code != 200:
            timings["integrity"].errors += 1

        began = time.perf_counter()
        submitted = client.post(
            f"/api/student/attempts/{attempt_id}/submit", headers=headers, json={}
        )
        timings["submit"].record(time.perf_counter() - began)
        if submitted.status_code != 200:
            timings["submit"].errors += 1
            failures.append(f"submit {index}: {submitted.status_code}")
            return

        began = time.perf_counter()
        result = client.get(f"/api/student/attempts/{attempt_id}/result", headers=headers)
        timings["result"].record(time.perf_counter() - began)
        if result.status_code != 200:
            timings["result"].errors += 1

    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(sit_exam, range(students)))
    wall = time.perf_counter() - wall_start

    print("--- latency ---")
    for timing in timings.values():
        print("  " + timing.summary())
    total_requests = sum(len(t.samples) for t in timings.values())
    total_errors = sum(t.errors for t in timings.values())
    print(
        f"\n  wall clock: {wall:.1f}s   requests: {total_requests}   "
        f"throughput: {total_requests / wall:.0f} req/s   "
        f"error rate: {total_errors / max(total_requests, 1) * 100:.2f}%"
    )

    # --- correctness, which is the point ---------------------------------
    print("\n--- correctness ---")
    check = session_factory()
    problems: list[str] = []

    attempts = check.scalars(select(Attempt).where(Attempt.quiz_id == quiz_id)).all()
    emails = [attempt.student_email for attempt in attempts]

    def assert_that(label: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            problems.append(label)

    assert_that(
        f"one attempt per student ({len(attempts)}/{students})",
        len(attempts) == students,
        f"got {len(attempts)}",
    )
    assert_that(
        "no duplicate attempts", len(emails) == len(set(emails)), f"{len(emails) - len(set(emails))} dupes"
    )

    finalized = [a for a in attempts if a.status is not AttemptStatus.IN_PROGRESS]
    assert_that(
        f"every attempt finalized ({len(finalized)}/{len(attempts)})",
        len(finalized) == len(attempts),
    )
    assert_that(
        "every finalized attempt has a score",
        all(a.score is not None and a.max_score is not None for a in finalized),
    )
    assert_that(
        "every finalized attempt has a submitted_at",
        all(a.submitted_at is not None for a in finalized),
    )
    assert_that(
        "integrity counts recorded",
        all(a.integrity_violation_count >= 1 for a in attempts),
    )

    question_rows = check.scalar(
        select(func.count()).select_from(AttemptQuestion).where(
            AttemptQuestion.attempt_id.in_([a.id for a in attempts])
        )
    )
    assert_that(
        f"every paper complete ({question_rows}/{students * questions})",
        question_rows == students * questions,
    )

    graded = check.scalars(select(Answer)).all()
    assert_that(
        f"every answer graded ({len(graded)} answers)",
        bool(graded) and all(row.is_correct is not None for row in graded),
        "a None here means an answer was silently treated as unanswered",
    )

    # No paper may contain two variants of the same family.
    repeats = 0
    for attempt in attempts:
        families = [
            check.get(QuestionVariant, row.variant_id).family_id
            for row in check.scalars(
                select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt.id)
            ).all()
        ]
        if len(families) != len(set(families)):
            repeats += 1
    assert_that("no paper repeats a question family", repeats == 0, f"{repeats} papers repeat")

    papers = set()
    for attempt in attempts:
        rows = check.scalars(
            select(AttemptQuestion)
            .where(AttemptQuestion.attempt_id == attempt.id)
            .order_by(AttemptQuestion.position)
        ).all()
        papers.add(tuple(str(row.variant_id) for row in rows))
    assert_that(
        f"papers differ between students ({len(papers)} distinct)",
        len(papers) > 1 or variants == 1,
    )

    check.close()
    engine.dispose()
    limiter.enabled = True

    if failures:
        print(f"\n--- request failures ({len(failures)}) ---")
        for failure in failures[:15]:
            print("  " + failure)

    print()
    if problems or failures:
        print(f"RESULT: FAILED — {len(problems)} correctness problem(s), {len(failures)} request failure(s)")
        return 1
    print("RESULT: PASSED — correctness held under load")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--students", type=int, default=200)
    parser.add_argument("--questions", type=int, default=5)
    parser.add_argument("--variants", type=int, default=4)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    return run(args.students, args.questions, args.variants, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
