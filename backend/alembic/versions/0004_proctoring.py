"""Proctoring layer: precheck sessions, evidence, and proctoring state

Adds the Phase 5 schema:

* ``precheck_sessions`` — a pre-exam security check that exists *before* an
  attempt, so that no clock runs while a student is granting permissions;
* ``proctoring_evidence`` — pointers to artefacts in a private bucket, each
  carrying its own retention date;
* proctoring columns on ``quizzes`` (policy) and ``attempts`` (observed state
  and teacher review);
* new ``integrity_event_type`` values for the proctoring signals.

Additive throughout. Every new column has a server default that reproduces the
existing behaviour, so an already-running deployment keeps working unchanged
and every existing row reads as a STANDARD, unproctored quiz.

``ALTER TYPE ... ADD VALUE`` runs inside the migration transaction, which
PostgreSQL 12 and later permit provided the new values are not *used* in the
same transaction. They are not: no data is written with them here.

Revision ID: 0004_proctoring
Revises: 0003_result_deliveries
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_proctoring"
down_revision: str | None = "0003_result_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ENUMS: dict[str, tuple[str, ...]] = {
    "proctoring_mode": ("STANDARD", "PROCTORED", "LOCKDOWN"),
    "precheck_status": ("PENDING", "BLOCKED", "FAILED", "READY", "CONSUMED", "EXPIRED"),
    "proctoring_status": ("NOT_REQUIRED", "ACTIVE", "DEGRADED", "UNVERIFIABLE"),
    "proctoring_review": (
        "PENDING",
        "REVIEWED_NO_ACTION",
        "REVIEWED_CONCERN",
        "REVIEWED_ESCALATED",
    ),
    "evidence_kind": (
        "PRECHECK_SNAPSHOT",
        "EVENT_SNAPSHOT",
        "SCREEN_CLIP",
        "CAMERA_CLIP",
    ),
    "evidence_source": ("CLIENT_CLAIM", "SERVER_OBSERVED", "HUMAN_REVIEW"),
}

# Appended to the existing integrity_event_type enum.
_NEW_EVENT_TYPES: tuple[str, ...] = (
    "MICROPHONE_STARTED",
    "MICROPHONE_STOPPED",
    "CAMERA_PERMISSION_DENIED",
    "MICROPHONE_PERMISSION_DENIED",
    "SCREEN_SHARE_PERMISSION_DENIED",
    "FACE_PRESENT",
    "FACE_MISSING",
    "MULTIPLE_FACES_DETECTED",
    "GAZE_AWAY_SUSTAINED",
    "OBJECT_DETECTED",
    "SCREEN_RECORDING_STARTED",
    "SCREEN_RECORDING_STOPPED",
    "DEVICE_CHANGED",
    "PROCTORING_UNAVAILABLE",
    "PROCTORING_HEARTBEAT_LOST",
    "PROCTORING_RESUMED",
    "PRECHECK_PASSED",
    "EVIDENCE_CAPTURED",
)

_ORIGINAL_EVENT_TYPES: tuple[str, ...] = (
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
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    for value in _NEW_EVENT_TYPES:
        op.execute(f"ALTER TYPE integrity_event_type ADD VALUE IF NOT EXISTS '{value}'")

    # --- quizzes: the policy -------------------------------------------------
    op.add_column(
        "quizzes",
        sa.Column(
            "proctoring_mode",
            _enum("proctoring_mode"),
            nullable=False,
            server_default="STANDARD",
        ),
    )
    op.add_column(
        "quizzes",
        sa.Column(
            "proctoring_config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    # --- precheck_sessions ---------------------------------------------------
    op.create_table(
        "precheck_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quiz_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_name", sa.String(length=200), nullable=False),
        sa.Column("student_email", sa.String(length=320), nullable=False),
        sa.Column("session_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", _enum("precheck_status"), nullable=False, server_default="PENDING"
        ),
        sa.Column(
            "proctoring_mode",
            _enum("proctoring_mode"),
            nullable=False,
            server_default="STANDARD",
        ),
        sa.Column(
            "capabilities_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "observations_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "verdict_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quizzes.id"],
            name="fk_precheck_sessions_quiz_id_quizzes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_precheck_sessions_attempt_id_attempts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_precheck_sessions"),
    )
    op.create_index(
        "ix_precheck_sessions_token_hash", "precheck_sessions", ["session_token_hash"]
    )
    op.create_index(
        "ix_precheck_sessions_quiz_id_email",
        "precheck_sessions",
        ["quiz_id", "student_email"],
    )

    # --- attempts: observed state and review ---------------------------------
    op.add_column(
        "attempts",
        sa.Column(
            "proctoring_mode",
            _enum("proctoring_mode"),
            nullable=False,
            server_default="STANDARD",
        ),
    )
    op.add_column(
        "attempts",
        sa.Column(
            "proctoring_status",
            _enum("proctoring_status"),
            nullable=False,
            server_default="NOT_REQUIRED",
        ),
    )
    op.add_column(
        "attempts", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "attempts",
        sa.Column("precheck_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "attempts",
        sa.Column(
            "proctoring_review",
            _enum("proctoring_review"),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column(
        "attempts", sa.Column("proctoring_review_note", sa.String(length=2000), nullable=True)
    )
    op.add_column("attempts", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "attempts", sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_attempts_precheck_session_id_precheck_sessions",
        "attempts",
        "precheck_sessions",
        ["precheck_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_attempts_reviewed_by_id_users",
        "attempts",
        "users",
        ["reviewed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- proctoring_evidence -------------------------------------------------
    op.create_table(
        "proctoring_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integrity_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", _enum("evidence_kind"), nullable=False),
        sa.Column(
            "source", _enum("evidence_source"), nullable=False, server_default="CLIENT_CLAIM"
        ),
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("object_path", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_proctoring_evidence_attempt_id_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["integrity_event_id"],
            ["integrity_events.id"],
            name="fk_proctoring_evidence_integrity_event_id_integrity_events",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proctoring_evidence"),
    )
    op.create_index(
        "ix_proctoring_evidence_attempt_id_captured_at",
        "proctoring_evidence",
        ["attempt_id", "captured_at"],
    )
    op.create_index(
        "ix_proctoring_evidence_retention_expires_at",
        "proctoring_evidence",
        ["retention_expires_at"],
    )


def downgrade() -> None:
    """Reverse the schema.

    **Lossy, deliberately and visibly.** PostgreSQL cannot remove a value from
    an enum in place, so restoring ``integrity_event_type`` means rebuilding the
    type — and any event row recorded with a Phase 5 value has no equivalent in
    the old vocabulary. Those rows are deleted rather than silently relabelled
    as something they were not: a proctoring signal rewritten as ``TAB_HIDDEN``
    would be a falsified audit trail, which is worse than a missing one.

    Run this only on a deployment that has no proctoring history worth keeping.
    """
    op.drop_index(
        "ix_proctoring_evidence_retention_expires_at", table_name="proctoring_evidence"
    )
    op.drop_index(
        "ix_proctoring_evidence_attempt_id_captured_at", table_name="proctoring_evidence"
    )
    op.drop_table("proctoring_evidence")

    op.drop_constraint("fk_attempts_reviewed_by_id_users", "attempts", type_="foreignkey")
    op.drop_constraint(
        "fk_attempts_precheck_session_id_precheck_sessions", "attempts", type_="foreignkey"
    )
    for column in (
        "reviewed_by_id",
        "reviewed_at",
        "proctoring_review_note",
        "proctoring_review",
        "precheck_session_id",
        "last_heartbeat_at",
        "proctoring_status",
        "proctoring_mode",
    ):
        op.drop_column("attempts", column)

    op.drop_index("ix_precheck_sessions_quiz_id_email", table_name="precheck_sessions")
    op.drop_index("ix_precheck_sessions_token_hash", table_name="precheck_sessions")
    op.drop_table("precheck_sessions")

    op.drop_column("quizzes", "proctoring_config_json")
    op.drop_column("quizzes", "proctoring_mode")

    # Rebuild integrity_event_type without the Phase 5 values.
    new_values = ", ".join(f"'{value}'" for value in _NEW_EVENT_TYPES)
    op.execute(f"DELETE FROM integrity_events WHERE event_type::text IN ({new_values})")

    old_values = ", ".join(f"'{value}'" for value in _ORIGINAL_EVENT_TYPES)
    op.execute(f"CREATE TYPE integrity_event_type_old AS ENUM ({old_values})")
    op.execute(
        "ALTER TABLE integrity_events "
        "ALTER COLUMN event_type TYPE integrity_event_type_old "
        "USING event_type::text::integrity_event_type_old"
    )
    op.execute("DROP TYPE integrity_event_type")
    op.execute("ALTER TYPE integrity_event_type_old RENAME TO integrity_event_type")

    bind = op.get_bind()
    for name in reversed(list(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
