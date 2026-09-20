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


class ValidationStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


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


class IntegrityAction(str, enum.Enum):
    NONE = "NONE"
    LOGGED = "LOGGED"
    WARNING_ISSUED = "WARNING_ISSUED"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
