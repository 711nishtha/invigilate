"""Teacher-facing results schemas.

These carry the answer key and student identities on purpose: a teacher
reviewing their own quiz needs both. What confines them is ownership — every
route resolves the quiz through ``services.ownership`` first, and a quiz the
caller does not own returns 404.

Explicitly absent everywhere: attempt tokens, token hashes and seeds. Nothing a
teacher needs requires them, and an export or a JSON response is exactly where
they would go unnoticed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.db.models import (
    AttemptStatus,
    DeliveryStatus,
    IntegrityEventType,
    IntegrityStatus,
    QuestionType,
)
from app.schemas.common import APIModel, StrictModel


class ScoreBand(APIModel):
    band: str
    count: int


class QuizStatsOut(APIModel):
    """Dashboard counters. Averages are null when there is nothing to average."""

    attempt_count: int
    in_progress_count: int
    submitted_count: int
    auto_submitted_count: int
    expired_count: int
    finalized_count: int
    completion_rate: float | None

    average_percentage: float | None
    highest_percentage: float | None
    lowest_percentage: float | None
    average_score: float | None
    max_score: float | None

    clean_count: int
    warning_count: int
    flagged_count: int
    integrity_event_count: int

    score_distribution: list[ScoreBand]


class AttemptRowOut(APIModel):
    attempt_id: uuid.UUID
    student_name: str
    student_email: str
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None
    duration_seconds: int | None
    score: float | None
    max_score: float | None
    percentage: float | None
    integrity_status: IntegrityStatus
    violation_count: int
    answered_count: int
    question_count: int


class QuizResultsOut(APIModel):
    quiz_id: uuid.UUID
    title: str
    public_code: str | None
    question_count: int
    stats: QuizStatsOut
    attempts: list[AttemptRowOut]


class AttemptQuestionDetailOut(APIModel):
    """One question of one attempt, as the teacher sees it.

    Carries both the student's answer and the correct answer — that is the
    point of a marking view.
    """

    position: int
    question_type: QuestionType
    topic: str | None
    difficulty: str | None
    original_question: str
    question_text: str
    options: list[str] | None
    selected_answer: str | None
    correct_answer: str | None
    is_correct: bool | None
    answered_at: datetime | None


class IntegrityEventOut(APIModel):
    event_type: IntegrityEventType
    severity: int
    occurred_at: datetime
    client_reported_at: datetime | None
    action_taken: str
    metadata: dict = Field(default_factory=dict)


class AttemptDetailOut(APIModel):
    attempt_id: uuid.UUID
    quiz_id: uuid.UUID
    quiz_title: str
    student_name: str
    student_email: str
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None
    duration_seconds: int | None
    score: float | None
    max_score: float | None
    percentage: float | None
    integrity_status: IntegrityStatus
    violation_count: int
    questions: list[AttemptQuestionDetailOut]
    integrity_events: list[IntegrityEventOut]


class DeliveryOut(APIModel):
    id: uuid.UUID
    status: DeliveryStatus
    recipient: str
    provider: str | None
    provider_message_id: str | None
    attempt_count: int
    attempts_made: int
    error_code: str | None
    error_detail: str | None
    retained: bool = False
    created_at: datetime
    sent_at: datetime | None
    failed_at: datetime | None


class SendResultsRequest(StrictModel):
    """Intentionally empty.

    The recipient is the quiz owner, read from the database. Accepting an
    address here would turn the endpoint into an open relay for a workbook full
    of student marks.
    """


class SendResultsResponse(APIModel):
    sent: bool
    delivery: DeliveryOut
    error_code: str | None = None
    error_message: str | None = None


# Field names that must never appear on a teacher results payload either.
FORBIDDEN_RESULT_FIELDS = frozenset(
    {"attempt_token", "attempt_token_hash", "seed", "api_key", "service_role"}
)
