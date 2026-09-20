"""Variant lifecycle, versioning and supersession

Replaces the three-state validation_status enum with the explicit lifecycle
GENERATED -> VALIDATED -> APPROVED -> PUBLISHED (plus REJECTED), and adds the
columns that make an approved variant immutable: editing one supersedes it with
a new version rather than rewriting the row a historical attempt was served.

Revision ID: 0002_variant_lifecycle
Revises: 0001_initial
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_variant_lifecycle"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("PENDING", "APPROVED", "REJECTED")
_NEW_VALUES = ("GENERATED", "VALIDATED", "APPROVED", "PUBLISHED", "REJECTED")

# PENDING meant "generated, not yet looked at", which is GENERATED in the new
# vocabulary. APPROVED and REJECTED carry over unchanged.
_UPGRADE_MAP = {"PENDING": "GENERATED", "APPROVED": "APPROVED", "REJECTED": "REJECTED"}
# Going back, everything that had not yet been approved becomes PENDING again.
_DOWNGRADE_MAP = {
    "GENERATED": "PENDING",
    "VALIDATED": "PENDING",
    "APPROVED": "APPROVED",
    "PUBLISHED": "APPROVED",
    "REJECTED": "REJECTED",
}


def _swap_enum(
    *, new_name: str, new_values: tuple[str, ...], old_name: str, mapping: dict[str, str]
) -> None:
    """Retype question_variants.validation_status, translating existing rows.

    PostgreSQL cannot remove a value from an enum, so the type is rebuilt and
    the column converted through a CASE expression. Doing it in one statement
    keeps the table consistent at every point.
    """
    bind = op.get_bind()
    postgresql.ENUM(*new_values, name=new_name).create(bind, checkfirst=True)

    case_sql = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items())
    op.execute(
        f"ALTER TABLE question_variants "
        f"ALTER COLUMN validation_status DROP DEFAULT, "
        f"ALTER COLUMN validation_status TYPE {new_name} "
        f"USING (CASE validation_status::text {case_sql} END)::{new_name}"
    )
    op.execute(
        f"ALTER TABLE question_variants "
        f"ALTER COLUMN validation_status SET DEFAULT '{list(mapping.values())[0]}'::{new_name}"
    )
    postgresql.ENUM(name=old_name).drop(bind, checkfirst=True)


def upgrade() -> None:
    # Optional per-quiz publish requirements (required topics / difficulties).
    op.add_column(
        "quizzes",
        sa.Column(
            "coverage_config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    _swap_enum(
        new_name="variant_status",
        new_values=_NEW_VALUES,
        old_name="validation_status",
        mapping=_UPGRADE_MAP,
    )

    op.add_column(
        "question_variants",
        sa.Column("validation_errors_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "question_variants",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "question_variants",
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "question_variants", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "question_variants",
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.create_foreign_key(
        "fk_question_variants_superseded_by_id_question_variants",
        "question_variants",
        "question_variants",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_question_variants_approved_by_id_users",
        "question_variants",
        "users",
        ["approved_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # The assignment engine asks for "assignable variants in this family":
    # approved or published, and not superseded.
    op.create_index(
        "ix_question_variants_family_id_superseded_by_id",
        "question_variants",
        ["family_id", "superseded_by_id"],
    )


def downgrade() -> None:
    op.drop_column("quizzes", "coverage_config_json")
    op.drop_index(
        "ix_question_variants_family_id_superseded_by_id", table_name="question_variants"
    )
    op.drop_constraint(
        "fk_question_variants_approved_by_id_users", "question_variants", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_question_variants_superseded_by_id_question_variants",
        "question_variants",
        type_="foreignkey",
    )
    op.drop_column("question_variants", "approved_by_id")
    op.drop_column("question_variants", "approved_at")
    op.drop_column("question_variants", "superseded_by_id")
    op.drop_column("question_variants", "version")
    op.drop_column("question_variants", "validation_errors_json")

    _swap_enum(
        new_name="validation_status",
        new_values=_OLD_VALUES,
        old_name="variant_status",
        mapping=_DOWNGRADE_MAP,
    )
