"""Student-facing request and response schemas.

Every field here was chosen deliberately. None of these models can carry an
answer key, a grading field, generation configuration or teacher-only metadata,
so a handler that passes the wrong object still cannot leak one — the
serializer has nowhere to put it.

A regression test asserts that none of these models has a forbidden field.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import EmailStr, Field

from app.db.models import (
    ProctoringMode,
    ProctoringStatus,
    AttemptStatus,
    IntegrityEventType,
    IntegrityStatus,
    QuestionType,
    SecurityLevel,
)
from app.schemas.common import APIModel, StrictModel


# --- Public quiz -----------------------------------------------------------


class PublicQuizOut(APIModel):
    """The pre-exam screen, and nothing more.

    Carries no question text, no variant ids, no pool size and no internal
    quiz id: the public code is the only handle a student needs, and anything
    else would be a hint about content or scale.

    ``max_integrity_violations`` *is* included on purpose. A student is
    entitled to know the rules they are being held to before consenting to be
    monitored, and knowing the budget does not help them defeat it — the count
    is kept server-side.
    """

    public_code: str
    title: str
    description: str | None
    duration_seconds: int
    question_count: int
    security_level: SecurityLevel
    max_integrity_violations: int
    allow_backtracking: bool
    requires_fullscreen: bool
    shows_score_after_submission: bool
    owner_name: str | None


class AttemptStartRequest(StrictModel):
    student_name: str = Field(min_length=1, max_length=200)
    student_email: EmailStr

    # No seed, no attempt id, no start time, no question selection. Everything
    # that decides the exam is the server's.


class AttemptStartResponse(APIModel):
    """Returned once. ``attempt_token`` is never retrievable again."""

    attempt_id: uuid.UUID
    attempt_token: str
    server_time: datetime
    started_at: datetime
    expires_at: datetime
    seconds_remaining: int
    question_count: int
    allow_backtracking: bool
    security_level: SecurityLevel
    # Phase 5. STANDARD for every quiz that does not use a pre-exam check, so
    # an existing client that ignores these fields is unaffected.
    proctoring_mode: ProctoringMode = ProctoringMode.STANDARD
    # How often the runner must report monitoring state. The cadence is the
    # server's to set, not the client's to choose.
    heartbeat_interval_seconds: int = 0


# --- Attempt state ---------------------------------------------------------


class AttemptStateOut(APIModel):
    """Polled by the exam runner to resynchronize against the server clock."""

    attempt_id: uuid.UUID
    status: AttemptStatus
    server_time: datetime
    started_at: datetime
    expires_at: datetime
    seconds_remaining: int
    question_count: int
    answered_count: int
    allow_backtracking: bool
    security_level: SecurityLevel
    proctoring_mode: ProctoringMode = ProctoringMode.STANDARD
    # Never has a "passed" value; see ProctoringStatus. The runner shows this
    # to the student so a monitoring failure is visible rather than silent.
    proctoring_status: ProctoringStatus = ProctoringStatus.NOT_REQUIRED
    heartbeat_interval_seconds: int = 0
    integrity_status: IntegrityStatus
    # The official, server-side total. Shown so a student knows where they
    # stand; it is read from the attempt row, never from the client.
    violation_count: int
    max_integrity_violations: int
    is_terminal: bool


class StudentQuestionOut(APIModel):
    """One question as this student received it.

    ``options`` are already permuted for this attempt. The permutation itself
    stays in the database, so the mapping from displayed position to canonical
    option is never exposed.
    """

    attempt_question_id: uuid.UUID
    position: int
    question_type: QuestionType
    question_text: str
    options: list[str] | None = None
    selected_answer: dict[str, Any] | None = None


class AttemptQuestionsOut(APIModel):
    attempt_id: uuid.UUID
    server_time: datetime
    seconds_remaining: int
    questions: list[StudentQuestionOut]


# --- Answers ---------------------------------------------------------------


class AnswerRequest(StrictModel):
    """`extra="forbid"` from StrictModel, so a smuggled `is_correct` is a 422.

    The service rejects grading fields explicitly as well, because a clear
    error is more useful than a schema rejection when someone is probing.
    """

    attempt_question_id: uuid.UUID
    selected_answer: dict[str, Any]


class AnswerResponse(APIModel):
    """Deliberately silent about correctness.

    Telling a student whether an answer was right would turn the exam into an
    oracle they could brute-force.
    """

    saved: Literal[True] = True
    attempt_question_id: uuid.UUID
    answered_at: datetime
    server_time: datetime
    seconds_remaining: int


# --- Integrity events ------------------------------------------------------


class IntegrityEventRequest(StrictModel):
    event_type: IntegrityEventType
    # Kept for forensics only. The server stamps its own receipt time and
    # orders the timeline by that.
    client_reported_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    # There is no `severity` field and no `violation_count` field. Both are
    # decided server-side, and offering them here would invite the client to
    # understate its own violations.


class IntegrityEventResponse(APIModel):
    recorded: bool
    violation_total: int
    max_integrity_violations: int
    integrity_status: IntegrityStatus
    action_taken: str
    # True when the server has already finalized the attempt. The browser is
    # being told what happened; it is not being asked to do the submitting.
    auto_submitted: bool
    attempt_status: AttemptStatus


# --- Submission and result -------------------------------------------------


class SubmitRequest(StrictModel):
    """Intentionally empty.

    Nothing a client could send here would be trusted: the answers are already
    persisted, and the score is computed from them server-side.
    """


class AttemptResultOut(APIModel):
    """What a student may see after finishing.

    Score fields are ``None`` unless the quiz sets
    ``show_score_after_submission``. The decision is made server-side, so the
    numbers are absent from the payload rather than hidden by the UI.
    """

    attempt_id: uuid.UUID
    status: AttemptStatus
    submitted_at: datetime | None
    server_time: datetime
    question_count: int
    answered_count: int
    integrity_status: IntegrityStatus
    violation_count: int

    score_visible: bool
    score: float | None = None
    max_score: float | None = None
    percentage: float | None = None


# Field names that must never appear on a student-facing model. Shared with the
# disclosure tests so adding a schema without updating the guard fails CI.
FORBIDDEN_STUDENT_FIELDS = frozenset(
    {
        "correct_option",
        "correct_answer",
        "numeric_answer_json",
        "numeric_answer",
        "explanation",
        "variant_id",
        "family_id",
        "options_order_json",
        "content_hash",
        "validation_status",
        "validation_errors_json",
        "generation_config_json",
        "generation_prompt_version",
        "generation_model",
        "approved_by_id",
        "approved_at",
        "rejection_reason",
        "is_correct",
        "seed",
        "attempt_token_hash",
        "owner_id",
        "superseded_by_id",
    }
)

STUDENT_RESPONSE_MODELS = (
    PublicQuizOut,
    AttemptStartResponse,
    AttemptStateOut,
    StudentQuestionOut,
    AttemptQuestionsOut,
    AnswerResponse,
    IntegrityEventResponse,
    AttemptResultOut,
)
