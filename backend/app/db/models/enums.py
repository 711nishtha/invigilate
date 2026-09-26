"""Domain enumerations, stored as native PostgreSQL enum types.

Making these database-level types means an invalid state is rejected by
PostgreSQL even if application code is bypassed.
"""

from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"
    ADMIN = "ADMIN"


class QuizStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    GENERATING = "GENERATING"
    READY = "READY"
    PUBLISHED = "PUBLISHED"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class SecurityLevel(str, enum.Enum):
    STANDARD = "STANDARD"  # no fullscreen requirement, events still logged
    SECURE = "SECURE"  # fullscreen required, desktop only
    STRICT = "STRICT"  # SECURE plus optional camera/screen-share monitoring


class ProctoringMode(str, enum.Enum):
    """How much monitoring an exam requires before it may begin.

    Orthogonal to :class:`SecurityLevel`, which controls in-page browser
    behaviour (fullscreen, copy/paste). This decides whether a *pre-exam
    security check* is mandatory and whether the server will refuse to start
    the attempt until it has passed.
    """

    # No pre-exam check. The Phase 3 flow, unchanged.
    STANDARD = "STANDARD"
    # Camera/microphone/screen checks run before the timer starts, and the
    # server authorizes the start. Browser-based, with browser-sized limits.
    PROCTORED = "PROCTORED"
    # PROCTORED plus OS-level lockdown, which a web page cannot provide. This
    # requires a signed desktop client that does not exist yet, so the server
    # refuses to start a LOCKDOWN attempt rather than quietly running it as
    # PROCTORED. See app/lockdown/.
    LOCKDOWN = "LOCKDOWN"


class PrecheckStatus(str, enum.Enum):
    """State of one pre-exam security check session.

    The session exists *before* the attempt. No timer runs while a student is
    granting permissions, and only a CONSUMED session has produced an attempt.
    """

    PENDING = "PENDING"  # created; capabilities/readiness not yet submitted
    BLOCKED = "BLOCKED"  # this browser cannot satisfy a required check
    FAILED = "FAILED"  # capable, but a required check did not pass
    READY = "READY"  # server authorized the start
    CONSUMED = "CONSUMED"  # an attempt was created from it
    EXPIRED = "EXPIRED"


class ProctoringStatus(str, enum.Enum):
    """What the server can currently say about monitoring on an attempt.

    There is deliberately no "PASSED". Monitoring is either observed to be
    running, observed to be impaired, or not verifiable — the last of which is
    a state the teacher sees, never one that silently becomes success.
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    UNVERIFIABLE = "UNVERIFIABLE"


class EvidenceKind(str, enum.Enum):
    """What a stored evidence object is.

    Stills by default. Continuous video is not recorded unless a teacher turns
    it on explicitly, and the bucket is private in every case.
    """

    PRECHECK_SNAPSHOT = "PRECHECK_SNAPSHOT"
    EVENT_SNAPSHOT = "EVENT_SNAPSHOT"
    SCREEN_CLIP = "SCREEN_CLIP"
    CAMERA_CLIP = "CAMERA_CLIP"


class EvidenceSource(str, enum.Enum):
    """Who produced a signal — which is how much it is worth.

    A CLIENT_CLAIM is an assertion by an untrusted browser. It is recorded
    because the timeline is useful, not because it is proof. SERVER_OBSERVED
    facts (a heartbeat that never arrived, a deadline that passed) are the only
    ones the server knows first-hand.
    """

    CLIENT_CLAIM = "CLIENT_CLAIM"
    SERVER_OBSERVED = "SERVER_OBSERVED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ProctoringReview(str, enum.Enum):
    """A teacher's decision about an attempt's proctoring timeline.

    Note what is missing: there is no CHEATING value. The platform surfaces
    signals and evidence; a person decides what they mean, and the decision is
    recorded as their decision.
    """

    PENDING = "PENDING"
    REVIEWED_NO_ACTION = "REVIEWED_NO_ACTION"
    REVIEWED_CONCERN = "REVIEWED_CONCERN"
    REVIEWED_ESCALATED = "REVIEWED_ESCALATED"


class QuestionType(str, enum.Enum):
    MCQ = "MCQ"
    NUMERICAL = "NUMERICAL"
    TRUE_FALSE = "TRUE_FALSE"
    SHORT_ANSWER = "SHORT_ANSWER"


class GenerationStatus(str, enum.Enum):
    AI_GENERATED = "AI_GENERATED"
    DETERMINISTIC = "DETERMINISTIC"  # backend-computed numeric answer
    MANUAL = "MANUAL"  # written by the teacher
    FAILED = "FAILED"


class VariantStatus(str, enum.Enum):
    """Explicit lifecycle for a generated question variant.

    Generated content is never automatically publishable. A variant walks
    GENERATED -> VALIDATED -> APPROVED -> PUBLISHED, and only a human moves it
    from VALIDATED to APPROVED. Legal transitions are enforced in
    ``services/variant_lifecycle.py``; this enum only names the states.
    """

    GENERATED = "GENERATED"  # raw provider output, nothing checked yet
    VALIDATED = "VALIDATED"  # passed deterministic backend validation
    APPROVED = "APPROVED"  # a teacher accepted it
    PUBLISHED = "PUBLISHED"  # its quiz went live; now immutable
    REJECTED = "REJECTED"  # failed validation or refused by a teacher

    @property
    def is_assignable(self) -> bool:
        """Whether a student's paper may draw this variant.

        The single source of truth for eligibility. GENERATED and VALIDATED are
        deliberately excluded: passing validation is not consent.
        """
        return self in _ASSIGNABLE_VARIANT_STATUSES

    @property
    def is_immutable(self) -> bool:
        """Whether editing must supersede rather than mutate.

        Once a teacher has approved a variant it may already be on a paper, so
        its text can never change underneath a historical attempt.
        """
        return self in _IMMUTABLE_VARIANT_STATUSES


_ASSIGNABLE_VARIANT_STATUSES = frozenset({VariantStatus.APPROVED, VariantStatus.PUBLISHED})
_IMMUTABLE_VARIANT_STATUSES = frozenset({VariantStatus.APPROVED, VariantStatus.PUBLISHED})


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
    TIME_EXPIRED = "TIME_EXPIRED"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        """Terminal attempts are never reopened."""
        return self in _TERMINAL_ATTEMPT_STATUSES


_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.TIME_EXPIRED}
)


class IntegrityStatus(str, enum.Enum):
    CLEAN = "CLEAN"
    WARNING = "WARNING"
    FLAGGED = "FLAGGED"


class IntegrityEventType(str, enum.Enum):
    TAB_HIDDEN = "TAB_HIDDEN"
    TAB_VISIBLE = "TAB_VISIBLE"
    WINDOW_BLUR = "WINDOW_BLUR"
    WINDOW_FOCUS = "WINDOW_FOCUS"
    FULLSCREEN_ENTER = "FULLSCREEN_ENTER"
    FULLSCREEN_EXIT = "FULLSCREEN_EXIT"
    COPY_ATTEMPT = "COPY_ATTEMPT"
    CUT_ATTEMPT = "CUT_ATTEMPT"
    PASTE_ATTEMPT = "PASTE_ATTEMPT"
    RIGHT_CLICK = "RIGHT_CLICK"
    KEYBOARD_SHORTCUT_ATTEMPT = "KEYBOARD_SHORTCUT_ATTEMPT"
    SCREEN_SHARE_STARTED = "SCREEN_SHARE_STARTED"
    SCREEN_SHARE_STOPPED = "SCREEN_SHARE_STOPPED"
    CAMERA_STARTED = "CAMERA_STARTED"
    CAMERA_STOPPED = "CAMERA_STOPPED"
    NETWORK_DISCONNECTED = "NETWORK_DISCONNECTED"
    NETWORK_RECONNECTED = "NETWORK_RECONNECTED"
    ATTEMPT_RESUMED = "ATTEMPT_RESUMED"
    MULTIPLE_SESSION_DETECTED = "MULTIPLE_SESSION_DETECTED"
    TIME_EXPIRED = "TIME_EXPIRED"
    AUTO_SUBMISSION_TRIGGERED = "AUTO_SUBMISSION_TRIGGERED"

    # --- Phase 5: proctoring signals ---------------------------------------
    # Every name below describes what was *observed*, not what it means. There
    # is no CHEATING_DETECTED, and there never should be: the browser reports
    # a signal, the server weighs it, a teacher interprets it.
    MICROPHONE_STARTED = "MICROPHONE_STARTED"
    MICROPHONE_STOPPED = "MICROPHONE_STOPPED"
    CAMERA_PERMISSION_DENIED = "CAMERA_PERMISSION_DENIED"
    MICROPHONE_PERMISSION_DENIED = "MICROPHONE_PERMISSION_DENIED"
    SCREEN_SHARE_PERMISSION_DENIED = "SCREEN_SHARE_PERMISSION_DENIED"
    FACE_PRESENT = "FACE_PRESENT"
    FACE_MISSING = "FACE_MISSING"
    MULTIPLE_FACES_DETECTED = "MULTIPLE_FACES_DETECTED"
    GAZE_AWAY_SUSTAINED = "GAZE_AWAY_SUSTAINED"
    OBJECT_DETECTED = "OBJECT_DETECTED"
    SCREEN_RECORDING_STARTED = "SCREEN_RECORDING_STARTED"
    SCREEN_RECORDING_STOPPED = "SCREEN_RECORDING_STOPPED"
    DEVICE_CHANGED = "DEVICE_CHANGED"
    # The client telling us honestly that it can no longer monitor. Worth zero
    # severity and a status change: an impaired stream is not misconduct, but
    # it must never look like a clean exam either.
    PROCTORING_UNAVAILABLE = "PROCTORING_UNAVAILABLE"
    # Server-generated.
    PROCTORING_HEARTBEAT_LOST = "PROCTORING_HEARTBEAT_LOST"
    PROCTORING_RESUMED = "PROCTORING_RESUMED"
    PRECHECK_PASSED = "PRECHECK_PASSED"
    EVIDENCE_CAPTURED = "EVIDENCE_CAPTURED"


class IntegrityAction(str, enum.Enum):
    NONE = "NONE"
    LOGGED = "LOGGED"
    WARNING_ISSUED = "WARNING_ISSUED"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
