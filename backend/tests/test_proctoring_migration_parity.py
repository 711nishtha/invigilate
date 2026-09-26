"""Migration 0004 must describe the same enums the models do.

A native PostgreSQL enum is defined twice: once in Python and once in the
migration that creates the type. Nothing makes the two agree automatically, and
a mismatch does not fail on SQLite — where the tests run — because SQLite stores
enums as text and accepts anything. It fails in production, on the first row
that uses the missing value, with ``invalid input value for enum``.

So the agreement is asserted here rather than discovered there.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from app.db.models import (
    EvidenceKind,
    EvidenceSource,
    IntegrityEventType,
    PrecheckStatus,
    ProctoringMode,
    ProctoringReview,
    ProctoringStatus,
)

MIGRATION = pathlib.Path("alembic/versions/0004_proctoring.py")


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0004", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENUM_PAIRS = {
    "proctoring_mode": ProctoringMode,
    "precheck_status": PrecheckStatus,
    "proctoring_status": ProctoringStatus,
    "proctoring_review": ProctoringReview,
    "evidence_kind": EvidenceKind,
    "evidence_source": EvidenceSource,
}


@pytest.mark.parametrize("type_name,enum_cls", sorted(ENUM_PAIRS.items()))
def test_each_new_enum_matches_its_model(type_name: str, enum_cls: type) -> None:
    migration = _load()
    assert set(migration._ENUMS[type_name]) == {member.value for member in enum_cls}


def test_the_event_type_lists_cover_the_python_enum_exactly() -> None:
    """Original plus new must equal the enum — no gaps, no inventions."""
    migration = _load()
    combined = set(migration._ORIGINAL_EVENT_TYPES) | set(migration._NEW_EVENT_TYPES)
    assert combined == {member.value for member in IntegrityEventType}


def test_the_original_and_new_event_lists_do_not_overlap() -> None:
    """An overlap would make the downgrade delete pre-Phase-5 audit rows."""
    migration = _load()
    assert not set(migration._ORIGINAL_EVENT_TYPES) & set(migration._NEW_EVENT_TYPES)


def test_the_downgrade_documents_that_it_is_lossy() -> None:
    """A destructive downgrade must say so where an operator will read it."""
    source = MIGRATION.read_text(encoding="utf-8").lower()
    assert "lossy" in source
    assert "deleted rather than silently relabelled" in source


def test_new_values_are_appended_not_inserted() -> None:
    """ADD VALUE without BEFORE/AFTER appends, which keeps existing ordinals
    stable. Reordering an in-use enum would rewrite stored values."""
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ADD VALUE IF NOT EXISTS" in source
    assert " BEFORE " not in source and " AFTER " not in source
