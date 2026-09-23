"""Result delivery records

Adds ``result_deliveries``: one row per attempt to email a quiz's results to
its owner, with its own status, retry count and failure detail.

Deliberately a separate table rather than columns on ``quizzes``. A delivery is
an operational side effect with its own lifecycle; putting it on the quiz row
would mean a failed email mutated academic record, and would lose the history
of which send covered which attempts.

Revision ID: 0003_result_deliveries
Revises: 0002_variant_lifecycle
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_result_deliveries"
down_revision: str | None = "0002_variant_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, tuple[str, ...]] = {
    "delivery_kind": ("RESULTS_EMAIL",),
    "delivery_status": ("PENDING", "SENT", "FAILED"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "result_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quiz_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", _enum("delivery_kind"), nullable=False, server_default="RESULTS_EMAIL"),
        sa.Column("status", _enum("delivery_status"), nullable=False, server_default="PENDING"),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempts_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("export_object_path", sa.String(length=500), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quizzes.id"],
            name="fk_result_deliveries_quiz_id_quizzes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name="fk_result_deliveries_requested_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_result_deliveries"),
    )
    op.create_index(
        "ix_result_deliveries_quiz_id_created_at",
        "result_deliveries",
        ["quiz_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_result_deliveries_quiz_id_created_at", table_name="result_deliveries")
    op.drop_table("result_deliveries")

    bind = op.get_bind()
    for name in reversed(list(_ENUMS)):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
