"""Per-quiz proctoring policy, and the honesty guardrails around it.

Two groups of tests.

The first is ordinary configuration: a teacher's policy is validated, an
unrecognised key is refused rather than stored and ignored, and a policy cannot
change under a live exam.

The second group is unusual for a test file and deliberate. Phase 5 makes claims
about what browser proctoring can do, and the easiest way to break this system
is not a bug — it is a sentence. A future contributor who adds "cheating is
impossible" to a template, or maps LOCKDOWN to the browser provider to make a
demo work, breaks the product's honesty without breaking a single assertion.
These tests assert on the source and the vocabulary so that overstatement is a
build failure.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import ProctoringMode, Quiz
from app.lockdown import LockdownClientUnavailable, get_lockdown_provider
from app.services import proctoring_policy
from app.services.proctoring_policy import policy_for, validate_config


def create_quiz(client: TestClient, headers: dict, **overrides) -> tuple[int, dict]:  # type: ignore[no-untyped-def]
    payload = {
        "title": "Proctored Final",
        "duration_seconds": 1800,
        "question_count": 2,
        **overrides,
    }
    response = client.post("/api/quizzes", json=payload, headers=headers)
    return response.status_code, response.json()


class TestModeDefaults:
    def test_standard_requires_nothing(self, db: Session) -> None:
        quiz = Quiz(proctoring_mode=ProctoringMode.STANDARD, proctoring_config_json={})
        policy = policy_for(quiz)
        assert policy.requires_precheck is False
        assert policy.required_capabilities == ()

    def test_proctored_requires_a_camera_and_a_face(self, db: Session) -> None:
        quiz = Quiz(proctoring_mode=ProctoringMode.PROCTORED, proctoring_config_json={})
        policy = policy_for(quiz)
        assert policy.requires_precheck is True
        assert policy.require_camera is True
        assert policy.require_face_presence is True
        assert "face_detection" in policy.required_capabilities

    def test_a_teacher_override_wins_over_the_mode_default(self, db: Session) -> None:
        quiz = Quiz(
            proctoring_mode=ProctoringMode.PROCTORED,
            proctoring_config_json={"require_camera": True, "require_face_presence": False},
        )
        assert policy_for(quiz).require_face_presence is False

    def test_a_missing_config_is_treated_as_empty(self, db: Session) -> None:
        quiz = Quiz(proctoring_mode=ProctoringMode.PROCTORED, proctoring_config_json=None)
        assert policy_for(quiz).require_camera is True


class TestConfigValidation:
    def test_an_unknown_key_is_refused(self) -> None:
        """Storing it would let a teacher believe a rule is enforced."""
        with pytest.raises(ValueError, match="Unsupported proctoring keys"):
            validate_config(ProctoringMode.PROCTORED, {"require_telepathy": True})

    def test_a_non_boolean_flag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="true or false"):
            validate_config(ProctoringMode.PROCTORED, {"require_camera": "yes"})

    @pytest.mark.parametrize(
        "key,bad",
        [
            ("evidence_retention_days", 0),
            ("evidence_retention_days", 4000),
            ("face_missing_grace_seconds", 1),
            ("heartbeat_interval_seconds", 5),
            ("heartbeat_grace_seconds", 10),
        ],
    )
    def test_out_of_range_numbers_are_refused(self, key: str, bad: int) -> None:
        with pytest.raises(ValueError):
            validate_config(ProctoringMode.PROCTORED, {"require_camera": True, key: bad})

    def test_an_unknown_stream_loss_action_is_refused(self) -> None:
        with pytest.raises(ValueError, match="on_stream_loss"):
            validate_config(
                ProctoringMode.PROCTORED,
                {"require_camera": True, "on_stream_loss": "EXPEL"},
            )

    def test_a_standard_quiz_cannot_carry_a_policy(self) -> None:
        """A policy nothing reads is worse than no policy."""
        with pytest.raises(ValueError, match="STANDARD quiz has no proctoring policy"):
            validate_config(ProctoringMode.STANDARD, {"require_camera": True})

    def test_face_detection_without_a_camera_is_refused(self) -> None:
        """Otherwise the exam reports nothing and looks configured."""
        with pytest.raises(ValueError, match="need require_camera"):
            validate_config(
                ProctoringMode.PROCTORED,
                {"require_camera": False, "require_face_presence": True},
            )

    def test_screen_recording_without_screen_share_is_refused(self) -> None:
        with pytest.raises(ValueError, match="record_screen needs"):
            validate_config(
                ProctoringMode.PROCTORED,
                {"require_camera": True, "record_screen": True, "require_screen_share": False},
            )

    def test_a_valid_policy_round_trips(self) -> None:
        config = {
            "require_camera": True,
            "require_microphone": True,
            "evidence_retention_days": 14,
            "on_stream_loss": "FLAG",
        }
        assert validate_config(ProctoringMode.PROCTORED, config) == config


class TestConfigThroughTheAPI:
    def test_a_quiz_defaults_to_standard(
        self, client: TestClient, teacher_a: dict
    ) -> None:
        """An existing client that never sends these fields is unaffected."""
        status, body = create_quiz(client, teacher_a)
        assert status == 201, body
        assert body["proctoring_mode"] == "STANDARD"
        assert body["proctoring_config_json"] == {}

    def test_a_proctored_quiz_can_be_created(
        self, client: TestClient, teacher_a: dict
    ) -> None:
        status, body = create_quiz(
            client,
            teacher_a,
            proctoring_mode="PROCTORED",
            proctoring_config_json={"require_camera": True, "evidence_retention_days": 14},
        )
        assert status == 201, body
        assert body["proctoring_mode"] == "PROCTORED"

    def test_an_invalid_policy_is_a_422(
        self, client: TestClient, teacher_a: dict
    ) -> None:
        status, _body = create_quiz(
            client,
            teacher_a,
            proctoring_mode="PROCTORED",
            proctoring_config_json={"require_lasers": True},
        )
        assert status == 422

    def test_an_invalid_policy_on_update_is_refused(
        self, client: TestClient, teacher_a: dict
    ) -> None:
        _status, quiz = create_quiz(client, teacher_a, proctoring_mode="PROCTORED")
        response = client.patch(
            f"/api/quizzes/{quiz['id']}",
            json={"proctoring_config_json": {"nope": True}},
            headers=teacher_a,
        )
        assert response.status_code == 422

    def test_an_update_is_validated_against_the_resulting_state(
        self, client: TestClient, teacher_a: dict
    ) -> None:
        """Sending only the config must be checked against the stored mode."""
        _status, quiz = create_quiz(client, teacher_a, proctoring_mode="STANDARD")
        response = client.patch(
            f"/api/quizzes/{quiz['id']}",
            json={"proctoring_config_json": {"require_camera": True}},
            headers=teacher_a,
        )
        assert response.status_code == 422

    def test_proctoring_cannot_change_while_published(
        self, client: TestClient, teacher_a: dict, db: Session
    ) -> None:
        """A student who passed a check for one exam must not find themselves
        in a differently monitored one."""
        from app.db.models import QuizStatus

        _status, created = create_quiz(client, teacher_a, proctoring_mode="PROCTORED")
        import uuid as _uuid

        quiz = db.get(Quiz, _uuid.UUID(created["id"]))
        assert quiz is not None
        quiz.status = QuizStatus.PUBLISHED
        db.commit()

        response = client.patch(
            f"/api/quizzes/{created['id']}",
            json={"proctoring_mode": "STANDARD"},
            headers=teacher_a,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "QUIZ_PUBLISHED_IMMUTABLE_FIELDS"


class TestLockdownIsNeverOverstated:
    def test_the_browser_provider_claims_no_os_control(self) -> None:
        capabilities = get_lockdown_provider(ProctoringMode.PROCTORED).capabilities()
        assert capabilities.provides_os_lockdown is False
        assert capabilities.can_block_other_applications is False
        assert capabilities.can_block_screenshots is False
        assert capabilities.can_detect_second_display is False
        assert capabilities.can_detect_virtual_machine is False

    def test_the_desktop_provider_claims_nothing_at_all(self) -> None:
        capabilities = get_lockdown_provider(ProctoringMode.LOCKDOWN).capabilities()
        assert capabilities.provides_os_lockdown is False
        assert "Not implemented" in " ".join(capabilities.notes)

    def test_the_desktop_provider_refuses_a_self_reported_attestation(self) -> None:
        """A web page claiming to be the desktop client is exactly the claim an
        attacker would make, and a browser cannot prove it."""
        provider = get_lockdown_provider(ProctoringMode.LOCKDOWN)
        with pytest.raises(LockdownClientUnavailable):
            provider.verify_client({"is_desktop_client": True, "signed": True})

    def test_lockdown_does_not_resolve_to_the_browser_provider(self) -> None:
        """Mapping it there is the silent downgrade this platform refuses."""
        assert get_lockdown_provider(ProctoringMode.LOCKDOWN).name == "desktop"

    def test_the_browser_notes_say_what_it_cannot_see(self) -> None:
        notes = " ".join(get_lockdown_provider(ProctoringMode.PROCTORED).capabilities().notes)
        assert "cannot see other applications" in notes.lower()


class TestNoOverstatementInSource:
    """Honesty as a build constraint rather than a code-review habit."""

    PROSE_ROOTS = ("app", "../docs", "../frontend/src")
    BANNED = (
        "cheating is impossible",
        "impossible to cheat",
        # Scoped to the student on purpose. "The browser cannot cheat the
        # clock" is a precise and true claim about a component; "the student
        # cannot cheat" is a claim about a person in a room the server has
        # never seen.
        "student cannot cheat",
        "students cannot cheat",
        "cheat-proof",
        "cheatproof",
        "tamper-proof",
        "100% secure",
        "fully secure",
        "guarantees integrity",
        "prevents all cheating",
        "foolproof",
    )

    # A disclaimer has to be able to name the thing it disclaims. "This does
    # not claim cheating is impossible" is the sentence the platform is
    # supposed to contain; only the unqualified assertion is banned.
    NEGATIONS = (
        "not",
        "never",
        "no ",
        "cannot",
        "does not",
        "doesn't",
        "refuses",
        "avoid",
        "must never",
        "banned",
        "ban ",
    )

    def _sources(self) -> list[pathlib.Path]:
        files: list[pathlib.Path] = []
        for root in self.PROSE_ROOTS:
            base = pathlib.Path(root)
            if not base.exists():
                continue
            for pattern in ("*.py", "*.md", "*.ts", "*.tsx"):
                files.extend(base.rglob(pattern))
        files.extend(pathlib.Path("..").glob("*.md"))
        return files

    def test_no_source_file_claims_cheating_is_impossible(self) -> None:
        offenders = []
        for path in self._sources():
            # This file names the banned phrases in order to ban them.
            if path.name == pathlib.Path(__file__).name:
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").lower().splitlines()
            for number, line in enumerate(lines, 1):
                for phrase in self.BANNED:
                    if phrase not in line:
                        continue
                    before = line.split(phrase)[0]
                    if any(negation in before for negation in self.NEGATIONS):
                        continue  # a disclaimer, which is the point
                    offenders.append(f"{path}:{number}: {phrase!r}")
        assert not offenders, f"overstated security claims: {offenders}"

    def test_the_negation_allowance_does_not_swallow_a_real_claim(self) -> None:
        """Guard the guard: the exemption must not neuter the test."""
        claim = "this platform guarantees integrity and cheating is impossible"
        before = claim.split("cheating is impossible")[0]
        assert not any(negation in before for negation in self.NEGATIONS)

    def test_no_source_file_calls_browser_monitoring_lockdown(self) -> None:
        """"Lockdown" is reserved for OS-level control that does not exist yet."""
        offenders = []
        for path in self._sources():
            if path.name == pathlib.Path(__file__).name:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if "browser lockdown mode" in text or "locks down the browser" in text:
                offenders.append(str(path))
        assert not offenders, offenders

    def test_the_lockdown_module_states_the_limits_explicitly(self) -> None:
        source = pathlib.Path("app/lockdown/base.py").read_text(encoding="utf-8").lower()
        assert "cannot lock down an operating system" in source
        for limitation in ("second monitor", "virtual machine", "another person"):
            assert limitation in source


class TestTheDegradationVocabularyIsHonest:
    def test_there_is_no_way_to_report_monitoring_as_passed(self) -> None:
        from app.db.models import ProctoringStatus

        assert not {member.value for member in ProctoringStatus} & {
            "PASSED",
            "VERIFIED",
            "CLEAN",
            "SECURE",
        }

    def test_every_degrading_event_is_a_real_event_type(self) -> None:
        from app.db.models import IntegrityEventType

        for event_type in proctoring_policy.DEGRADING_EVENTS:
            assert isinstance(event_type, IntegrityEventType)

    def test_server_only_proctoring_events_are_not_client_reportable(self) -> None:
        from app.services import integrity_policy

        for event_type in proctoring_policy.PROCTORING_SERVER_ONLY_EVENTS:
            assert event_type in integrity_policy.SERVER_ONLY_EVENTS

    def test_the_public_requirements_leak_no_thresholds(self) -> None:
        """A student is told what to prepare, not what the budget is."""
        quiz = Quiz(proctoring_mode=ProctoringMode.PROCTORED, proctoring_config_json={})
        published = policy_for(quiz).as_public_requirements()
        assert "max_integrity_violations" not in published
        assert "on_stream_loss" not in published
        assert not any("severity" in key for key in published)
