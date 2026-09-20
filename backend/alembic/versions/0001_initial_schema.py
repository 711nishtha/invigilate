"""Initial schema: users, quizzes, question families/variants, attempts, answers, integrity events

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum types are created explicitly so that both upgrade and downgrade are
# deterministic; the table definitions below reference them with
# create_type=False.
_ENUMS: dict[str, tuple[str, ...]] = {
    "user_role": ("TEACHER", "STUDENT", "ADMIN"),
    "quiz_status": ("DRAFT", "GENERATING", "READY", "PUBLISHED", "CLOSED", "ARCHIVED"),
    "security_level": ("STANDARD", "SECURE", "STRICT"),
    "question_type": ("MCQ", "NUMERICAL", "TRUE_FALSE", "SHORT_ANSWER"),
    "generation_status": ("AI_GENERATED", "DETERMINISTIC", "MANUAL", "FAILED"),
    "validation_status": ("PENDING", "APPROVED", "REJECTED"),
    "attempt_status": (
        "IN_PROGRESS",
        "SUBMITTED",
        "AUTO_SUBMITTED",
        "TIME_EXPIRED",
        "ABANDONED",
    ),
    "integrity_status": ("CLEAN", "WARNING", "FLAGGED"),
    "integrity_event_type": (
        "TAB_HIDDEN",
        "TAB_VISIBLE",
        "WINDOW_BLUR",
        "WINDOW_FOCUS",
        "FULLSCREEN_ENTER",
        "FULLSCREEN_EXIT",
        "COPY_ATTEMPT",
        "CUT_ATTEMPT",
        "PASTE_ATTEMPT",
        "RIGHT_CLICK",
        "KEYBOARD_SHORTCUT_ATTEMPT",
        "SCREEN_SHARE_STARTED",
        "SCREEN_SHARE_STOPPED",
        "CAMERA_STARTED",
        "CAMERA_STOPPED",
        "NETWORK_DISCONNECTED",
        "NETWORK_RECONNECTED",
        "ATTEMPT_RESUMED",
        "MULTIPLE_SESSION_DETECTED",
        "TIME_EXPIRED",
        "AUTO_SUBMISSION_TRIGGERED",
    ),
    "integrity_action": ("NONE", "LOGGED", "WARNING_ISSUED", "AUTO_SUBMITTED"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("supabase_user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", _enum("user_role"), nullable=False, server_default="TEACHER"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("supabase_user_id", name="uq_users_supabase_user_id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_supabase_user_id", "users", ["supabase_user_id"])

    op.create_table(
        "quizzes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_code", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("security_level", _enum("security_level"), nullable=False, server_default="SECURE"),
        sa.Column("max_integrity_violations", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("randomization_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_backtracking", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "show_score_after_submission", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("status", _enum("quiz_status"), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_quizzes_owner_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quizzes"),
        sa.UniqueConstraint("public_code", name="uq_quizzes_public_code"),
        sa.CheckConstraint(
            "duration_seconds >= 60 AND duration_seconds <= 28800", name="ck_quizzes_duration_range"
        ),
        sa.CheckConstraint(
            "question_count >= 1 AND question_count <= 200", name="ck_quizzes_question_count_range"
        ),
        sa.CheckConstraint(
            "max_integrity_violations >= 0", name="ck_quizzes_max_violations_non_negative"
        ),
    )
    op.create_index("ix_quizzes_owner_id_created_at", "quizzes", ["owner_id", "created_at"])

    op.create_table(
        "question_families",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quiz_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_question", sa.Text(), nullable=False),
        sa.Column("question_type", _enum("question_type"), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=True),
        sa.Column("difficulty", sa.String(length=20), nullable=True),
        sa.Column("learning_objective", sa.Text(), nullable=True),
        sa.Column(
            "generation_config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quizzes.id"],
            name="fk_question_families_quiz_id_quizzes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_question_families"),
    )
    op.create_index(
        "ix_question_families_quiz_id_position", "question_families", ["quiz_id", "position"]
    )

    op.create_table(
        "question_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("options_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("correct_option", sa.Integer(), nullable=True),
        sa.Column("numeric_answer_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("generation_model", sa.String(length=100), nullable=True),
        sa.Column("generation_prompt_version", sa.String(length=50), nullable=True),
        sa.Column(
            "generation_status", _enum("generation_status"), nullable=False, server_default="AI_GENERATED"
        ),
        sa.Column(
            "validation_status", _enum("validation_status"), nullable=False, server_default="PENDING"
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["question_families.id"],
            name="fk_question_variants_family_id_question_families",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_question_variants"),
        # Exact duplicates are refused by the database, not only by the pipeline.
        sa.UniqueConstraint(
            "family_id", "content_hash", name="uq_question_variants_family_id_content_hash"
        ),
        sa.CheckConstraint(
            "correct_option IS NULL OR correct_option >= 0",
            name="ck_question_variants_correct_option_non_negative",
        ),
    )
    op.create_index(
        "ix_question_variants_family_id_validation_status",
        "question_variants",
        ["family_id", "validation_status"],
    )

    op.create_table(
        "attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quiz_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("student_name", sa.String(length=200), nullable=False),
        sa.Column("student_email", sa.String(length=320), nullable=False),
        sa.Column("attempt_token_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", _enum("attempt_status"), nullable=False, server_default="IN_PROGRESS"),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("max_score", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("percentage", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("integrity_violation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("integrity_status", _enum("integrity_status"), nullable=False, server_default="CLEAN"),
        sa.Column("results_emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["quiz_id"], ["quizzes.id"], name="fk_attempts_quiz_id_quizzes", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["student_user_id"],
            ["users.id"],
            name="fk_attempts_student_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attempts"),
        # One attempt per student per quiz — enforced here so that two
        # simultaneous starts cannot both create a paper.
        sa.UniqueConstraint("quiz_id", "student_email", name="uq_attempts_quiz_id_student_email"),
    )
    op.create_index("ix_attempts_quiz_id_status", "attempts", ["quiz_id", "status"])
    op.create_index("ix_attempts_attempt_token_hash", "attempts", ["attempt_token_hash"])

    op.create_table(
        "attempt_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("options_order_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_attempt_questions_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        # RESTRICT: a variant that has been served to a student can never be
        # deleted out from under the record of that exam.
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["question_variants.id"],
            name="fk_attempt_questions_variant_id_question_variants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attempt_questions"),
        sa.UniqueConstraint("attempt_id", "position", name="uq_attempt_questions_attempt_id_position"),
        sa.UniqueConstraint(
            "attempt_id", "variant_id", name="uq_attempt_questions_attempt_id_variant_id"
        ),
    )

    op.create_table(
        "answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected_answer", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_question_id"],
            ["attempt_questions.id"],
            name="fk_answers_attempt_question_id_attempt_questions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_answers"),
        # Makes an answer write an upsert rather than an append.
        sa.UniqueConstraint("attempt_question_id", name="uq_answers_attempt_question_id"),
    )

    op.create_table(
        "integrity_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", _enum("integrity_event_type"), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("action_taken", _enum("integrity_action"), nullable=False, server_default="LOGGED"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_integrity_events_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integrity_events"),
    )
    op.create_index(
        "ix_integrity_events_attempt_id_occurred_at", "integrity_events", ["attempt_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_integrity_events_attempt_id_occurred_at", table_name="integrity_events")
    op.drop_table("integrity_events")
    op.drop_table("answers")
    op.drop_table("attempt_questions")
    op.drop_index("ix_attempts_attempt_token_hash", table_name="attempts")
    op.drop_index("ix_attempts_quiz_id_status", table_name="attempts")
    op.drop_table("attempts")
    op.drop_index("ix_question_variants_family_id_validation_status", table_name="question_variants")
    op.drop_table("question_variants")
    op.drop_index("ix_question_families_quiz_id_position", table_name="question_families")
    op.drop_table("question_families")
    op.drop_index("ix_quizzes_owner_id_created_at", table_name="quizzes")
    op.drop_table("quizzes")
    op.drop_index("ix_users_supabase_user_id", table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    for name in reversed(list(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
