"""Proctoring policy: what an exam requires, and what a signal is worth.

This module is the *only* place that decides:

* which pre-exam checks a quiz requires (derived from its mode plus the
  teacher's configuration, never from the browser);
* how much each proctoring signal contributes to the integrity budget;
* how long a signal must persist before it counts at all.

Three rules shape everything here, and each exists because the alternative is
worse than useless:

1. **A signal is not an accusation.** ``FACE_MISSING`` means a browser said it
   could not find a face in a frame. It does not mean a person left, and it
   certainly does not mean they cheated. The severities below are small on
   purpose.

2. **One frame is never a decision.** Every counted proctoring signal has a
   repeat window. A second occurrence inside that window is recorded for the
   timeline at severity zero. A camera that blinks, a student who scratches
   their nose, or a client that loops on a bug therefore cannot walk an honest
   attempt into auto-submission.

3. **Unverifiable is a state, not a pass.** When monitoring stops working the
   attempt becomes ``DEGRADED`` or ``UNVERIFIABLE`` and the teacher sees that.
   It never silently becomes a clean exam, and it never destroys the academic
   result either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.db.models import IntegrityEventType, ProctoringMode, Quiz

# --- Severity ---------------------------------------------------------------
#
# Weights sit alongside the Phase 3 table in ``integrity_policy.SEVERITY``,
# which merges this dict at import time. They are deliberately in the same
# range as the existing ones: a proctoring signal is not automatically more
# damning than switching tabs.
#
#   0  informational — the timeline records it, the budget does not
#   1  a soft signal that means little alone and something in aggregate
#   2  a required monitoring condition was not observed
#   3  a stream the student agreed to keep running stopped, or a permission
#      required to sit the exam was refused
PROCTORING_SEVERITY: dict[IntegrityEventType, int] = {
    # --- Informational -----------------------------------------------------
    IntegrityEventType.MICROPHONE_STARTED: 0,
    IntegrityEventType.FACE_PRESENT: 0,
    IntegrityEventType.SCREEN_RECORDING_STARTED: 0,
    IntegrityEventType.PROCTORING_RESUMED: 0,
    IntegrityEventType.PRECHECK_PASSED: 0,
    IntegrityEventType.EVIDENCE_CAPTURED: 0,
    # Honest self-reporting must never be punished, or clients stop doing it
    # and we lose the only signal that says "do not trust the green ticks".
    # It changes proctoring_status instead, which is what a teacher acts on.
    IntegrityEventType.PROCTORING_UNAVAILABLE: 0,
    # --- Soft signals ------------------------------------------------------
    # Gaze is the weakest signal in the whole system. Looking away from a
    # screen is what people do when they think. It is worth 1 so that a
    # sustained pattern shows up in review, and no more.
    IntegrityEventType.GAZE_AWAY_SUSTAINED: 1,
    IntegrityEventType.DEVICE_CHANGED: 1,
    # --- A required condition was not observed -----------------------------
    IntegrityEventType.FACE_MISSING: 2,
    IntegrityEventType.OBJECT_DETECTED: 2,
    IntegrityEventType.PROCTORING_HEARTBEAT_LOST: 2,
    # --- A required stream stopped or was refused --------------------------
    IntegrityEventType.MICROPHONE_STOPPED: 3,
    IntegrityEventType.SCREEN_RECORDING_STOPPED: 3,
    IntegrityEventType.CAMERA_PERMISSION_DENIED: 3,
    IntegrityEventType.MICROPHONE_PERMISSION_DENIED: 3,
    IntegrityEventType.SCREEN_SHARE_PERMISSION_DENIED: 3,
    # A second person in frame is the strongest signal a webcam can give, and
    # it is still only a signal: siblings walk past, rooms are shared.
    IntegrityEventType.MULTIPLE_FACES_DETECTED: 3,
}

# How long a counted signal suppresses repeats of itself.
#
# This is rule 2 above, enforced server-side. The client cannot shorten these
# windows, so a broken or hostile client cannot drive an honest student's
# violation total up by replaying the same event in a loop.
REPEAT_WINDOW_SECONDS: dict[IntegrityEventType, int] = {
    IntegrityEventType.FACE_MISSING: 30,
    IntegrityEventType.MULTIPLE_FACES_DETECTED: 30,
    IntegrityEventType.GAZE_AWAY_SUSTAINED: 60,
    IntegrityEventType.OBJECT_DETECTED: 30,
    IntegrityEventType.MICROPHONE_STOPPED: 30,
    IntegrityEventType.SCREEN_RECORDING_STOPPED: 30,
    IntegrityEventType.CAMERA_PERMISSION_DENIED: 30,
    IntegrityEventType.MICROPHONE_PERMISSION_DENIED: 30,
    IntegrityEventType.SCREEN_SHARE_PERMISSION_DENIED: 30,
    IntegrityEventType.DEVICE_CHANGED: 30,
    IntegrityEventType.PROCTORING_HEARTBEAT_LOST: 60,
}

# Proctoring events the server raises itself. A client claiming one of these is
# writing fiction into an audit trail, so the student endpoint refuses them.
# Merged into ``integrity_policy.SERVER_ONLY_EVENTS``.
PROCTORING_SERVER_ONLY_EVENTS = frozenset(
    {
        IntegrityEventType.PROCTORING_HEARTBEAT_LOST,
        IntegrityEventType.PRECHECK_PASSED,
        IntegrityEventType.EVIDENCE_CAPTURED,
    }
)

# Signals that mean monitoring is impaired right now. Their arrival moves the
# attempt out of ACTIVE regardless of what the budget says.
DEGRADING_EVENTS = frozenset(
    {
        IntegrityEventType.PROCTORING_UNAVAILABLE,
        IntegrityEventType.CAMERA_STOPPED,
        IntegrityEventType.MICROPHONE_STOPPED,
        IntegrityEventType.SCREEN_SHARE_STOPPED,
        IntegrityEventType.SCREEN_RECORDING_STOPPED,
        IntegrityEventType.PROCTORING_HEARTBEAT_LOST,
    }
)


def repeat_window(event_type: IntegrityEventType) -> timedelta | None:
    seconds = REPEAT_WINDOW_SECONDS.get(event_type)
    return timedelta(seconds=seconds) if seconds else None


# --- Per-quiz configuration --------------------------------------------------


@dataclass(frozen=True)
class ProctoringPolicy:
    """Resolved, server-side view of what one quiz requires.

    Built from the quiz row, never from a request body. A student's browser
    receives the *requirements* derived from this so it knows what to ask
    permission for; it never sends them back as fact.
    """

    mode: ProctoringMode

    require_camera: bool = False
    require_microphone: bool = False
    require_screen_share: bool = False
    require_fullscreen: bool = False

    require_face_presence: bool = False
    detect_multiple_faces: bool = False
    detect_gaze: bool = False
    detect_objects: bool = False
    record_screen: bool = False

    require_consent: bool = True
    store_evidence: bool = True
    evidence_retention_days: int = 30

    # Grace before a client may report a sustained condition, and before the
    # server treats silence as a lost heartbeat.
    face_missing_grace_seconds: int = 15
    gaze_away_grace_seconds: int = 20
    heartbeat_interval_seconds: int = 30
    heartbeat_grace_seconds: int = 120

    # What happens when a required stream stops mid-exam. The default is the
    # conservative one: warn the student, flag the attempt, let them finish.
    # Ending an exam because a webcam driver crashed punishes the wrong person.
    on_stream_loss: str = "WARN"

    allow_mobile: bool = False

    @property
    def requires_precheck(self) -> bool:
        return self.mode is not ProctoringMode.STANDARD

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        """Browser capabilities without which this exam cannot run.

        Used to *block*, not to downgrade: a browser missing one of these is
        told it cannot sit the exam here. Quietly running a reduced version of
        a proctored exam would be the dishonest option.
        """
        names: list[str] = []
        if self.require_camera:
            names.append("camera")
        if self.require_microphone:
            names.append("microphone")
        if self.require_screen_share:
            names.append("screen_share")
        if self.require_fullscreen:
            names.append("fullscreen")
        if self.require_face_presence or self.detect_multiple_faces:
            names.append("face_detection")
        if self.detect_objects:
            names.append("object_detection")
        if self.record_screen:
            names.append("media_recorder")
        return tuple(names)

    def as_public_requirements(self) -> dict[str, Any]:
        """What the student's browser is told to prepare.

        Contains no thresholds that would help someone game the policy: the
        grace windows a client needs in order to behave correctly are included,
        the severity weights and the budget are not.
        """
        return {
            "mode": self.mode.value,
            "camera": self.require_camera,
            "microphone": self.require_microphone,
            "screen_share": self.require_screen_share,
            "fullscreen": self.require_fullscreen,
            "face_presence": self.require_face_presence,
            "multiple_face_detection": self.detect_multiple_faces,
            "gaze_signals": self.detect_gaze,
            "object_detection": self.detect_objects,
            "screen_recording": self.record_screen,
            "consent": self.require_consent,
            "evidence_stored": self.store_evidence,
            "evidence_retention_days": self.evidence_retention_days,
            "face_missing_grace_seconds": self.face_missing_grace_seconds,
            "gaze_away_grace_seconds": self.gaze_away_grace_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "required_capabilities": list(self.required_capabilities),
        }


# Defaults per mode. A teacher's ``proctoring_config_json`` overrides
# individual keys; the mode decides the starting point so that choosing
# PROCTORED is meaningful on its own.
_MODE_DEFAULTS: dict[ProctoringMode, dict[str, Any]] = {
    ProctoringMode.STANDARD: {},
    ProctoringMode.PROCTORED: {
        "require_camera": True,
        "require_fullscreen": True,
        "require_face_presence": True,
        "detect_multiple_faces": True,
        "require_consent": True,
        "store_evidence": True,
    },
    ProctoringMode.LOCKDOWN: {
        "require_camera": True,
        "require_microphone": True,
        "require_screen_share": True,
        "require_fullscreen": True,
        "require_face_presence": True,
        "detect_multiple_faces": True,
        "require_consent": True,
        "store_evidence": True,
    },
}

# Keys a teacher may set. Anything else is rejected at write time rather than
# stored and ignored, so nobody believes a setting is in force when no code
# reads it. (Same reasoning as coverage_config_json in Phase 2.)
CONFIGURABLE_KEYS = frozenset(
    {
        "require_camera",
        "require_microphone",
        "require_screen_share",
        "require_fullscreen",
        "require_face_presence",
        "detect_multiple_faces",
        "detect_gaze",
        "detect_objects",
        "record_screen",
        "require_consent",
        "store_evidence",
        "evidence_retention_days",
        "face_missing_grace_seconds",
        "gaze_away_grace_seconds",
        "heartbeat_interval_seconds",
        "heartbeat_grace_seconds",
        "on_stream_loss",
        "allow_mobile",
    }
)

STREAM_LOSS_ACTIONS = ("WARN", "FLAG", "AUTO_SUBMIT")


def policy_for(quiz: Quiz) -> ProctoringPolicy:
    """The effective policy for a quiz. The single source of truth."""
    mode = quiz.proctoring_mode or ProctoringMode.STANDARD
    values: dict[str, Any] = dict(_MODE_DEFAULTS.get(mode, {}))
    stored = quiz.proctoring_config_json or {}
    for key, value in stored.items():
        if key in CONFIGURABLE_KEYS:
            values[key] = value
    return ProctoringPolicy(mode=mode, **values)


def validate_config(mode: ProctoringMode, config: dict[str, Any]) -> dict[str, Any]:
    """Validate a teacher-supplied override block.

    Raises ``ValueError`` with a message meant for the teacher. Called from the
    quiz schema so an invalid policy is a 422 and never reaches the database.
    """
    unknown = set(config) - CONFIGURABLE_KEYS
    if unknown:
        raise ValueError(f"Unsupported proctoring keys: {', '.join(sorted(unknown))}")

    booleans = {
        key
        for key in CONFIGURABLE_KEYS
        if key.startswith(("require_", "detect_", "record_", "store_", "allow_"))
    }
    for key, value in config.items():
        if key in booleans and not isinstance(value, bool):
            raise ValueError(f"'{key}' must be true or false")

    for key, low, high in (
        ("evidence_retention_days", 1, 365),
        ("face_missing_grace_seconds", 5, 300),
        ("gaze_away_grace_seconds", 5, 300),
        ("heartbeat_interval_seconds", 10, 300),
        ("heartbeat_grace_seconds", 30, 900),
    ):
        if key in config:
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"'{key}' must be a whole number of {'days' if 'days' in key else 'seconds'}")
            if not low <= value <= high:
                raise ValueError(f"'{key}' must be between {low} and {high}")

    if "on_stream_loss" in config and config["on_stream_loss"] not in STREAM_LOSS_ACTIONS:
        raise ValueError(f"'on_stream_loss' must be one of: {', '.join(STREAM_LOSS_ACTIONS)}")

    if mode is ProctoringMode.STANDARD and config:
        raise ValueError(
            "A STANDARD quiz has no proctoring policy. Set proctoring_mode to "
            "PROCTORED before configuring proctoring options."
        )

    # Turning on a detector whose stream is off cannot work, and a teacher who
    # ticks 'detect faces' without 'require camera' would otherwise get a quiz
    # that reports nothing and looks fine.
    resolved = {**_MODE_DEFAULTS.get(mode, {}), **config}
    needs_camera = ("require_face_presence", "detect_multiple_faces", "detect_gaze", "detect_objects")
    if any(resolved.get(key) for key in needs_camera) and not resolved.get("require_camera"):
        raise ValueError("Face, gaze and object signals need require_camera to be true.")
    if resolved.get("record_screen") and not resolved.get("require_screen_share"):
        raise ValueError("record_screen needs require_screen_share to be true.")

    return dict(config)


# --- Capability gating -------------------------------------------------------


@dataclass(frozen=True)
class CapabilityVerdict:
    """Whether a reported browser can run this exam at all."""

    satisfiable: bool
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def evaluate_capabilities(
    policy: ProctoringPolicy, reported: dict[str, Any]
) -> CapabilityVerdict:
    """Compare a browser's self-reported capabilities with what is required.

    The report is untrusted, but it can only be used to *deny* a start: a
    browser that claims less than it has is refused, and a browser that claims
    more still has to produce the actual permission grants and signals in the
    readiness step. So lying here buys nothing.
    """
    missing = [
        name
        for name in policy.required_capabilities
        if not bool(reported.get(name, False))
    ]
    notes: list[str] = []
    if not policy.allow_mobile and bool(reported.get("is_mobile", False)):
        missing.append("desktop_browser")
        notes.append("This exam must be taken on a desktop or laptop browser.")
    if not bool(reported.get("secure_context", True)):
        missing.append("secure_context")
        notes.append("Camera and screen sharing need an https connection.")
    return CapabilityVerdict(
        satisfiable=not missing, missing=tuple(missing), notes=tuple(notes)
    )


# --- Readiness ---------------------------------------------------------------

# Maps a required policy flag to the observation key the browser reports and
# the message a student sees when it is not satisfied.
_READINESS_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("require_camera", "camera_active", "Your camera is not running."),
    ("require_microphone", "microphone_active", "Your microphone is not running."),
    ("require_screen_share", "screen_share_active", "You are not sharing your screen."),
    ("require_fullscreen", "fullscreen_active", "The exam must run in fullscreen."),
    ("require_face_presence", "face_detected", "No face was detected in the camera preview."),
    ("require_consent", "consent_accepted", "You must accept the monitoring notice."),
)


@dataclass
class ReadinessVerdict:
    """Server's decision on whether an exam may start.

    ``checks`` is a per-item record so a student can be told what to fix, and
    so a teacher can later see what the server actually observed rather than
    a single opaque boolean.
    """

    ready: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "checks": dict(self.checks), "failures": list(self.failures)}


def evaluate_readiness(
    policy: ProctoringPolicy, observations: dict[str, Any]
) -> ReadinessVerdict:
    """Decide, server-side, whether the pre-exam checks pass.

    The browser submits *observations* — "the camera track is live", "a face
    was found in the preview". It never submits a verdict, and there is no
    field it can set that means "ready". This function is the only thing that
    can produce ``ready=True``, and the start endpoint calls it again rather
    than trusting a stored flag.

    An observation is a claim, not proof. That limitation is real and is
    documented for teachers: what makes a claim checkable is the evidence
    snapshot stored alongside it and the human who can look at it.
    """
    verdict = ReadinessVerdict(ready=True)
    if not policy.requires_precheck:
        return verdict

    for flag, key, message in _READINESS_CHECKS:
        if not getattr(policy, flag):
            continue
        satisfied = observations.get(key) is True
        verdict.checks[key] = satisfied
        if not satisfied:
            verdict.ready = False
            verdict.failures.append(message)

    # A browser that admits it cannot monitor is refused a start. This is the
    # opposite of a downgrade: honesty blocks, it does not wave through.
    if observations.get("monitoring_impaired") is True:
        verdict.ready = False
        verdict.checks["monitoring_impaired"] = True
        verdict.failures.append(
            "Monitoring is not working in this browser, so the exam cannot start."
        )

    # Two faces in the pre-check preview is a room problem to solve before the
    # exam begins, not something to discover thirty minutes in.
    if policy.detect_multiple_faces and observations.get("multiple_faces") is True:
        verdict.ready = False
        verdict.checks["multiple_faces"] = True
        verdict.failures.append("More than one person is visible. Please sit alone.")

    return verdict


__all__ = [
    "CONFIGURABLE_KEYS",
    "DEGRADING_EVENTS",
    "PROCTORING_SERVER_ONLY_EVENTS",
    "PROCTORING_SEVERITY",
    "REPEAT_WINDOW_SECONDS",
    "STREAM_LOSS_ACTIONS",
    "CapabilityVerdict",
    "ProctoringPolicy",
    "ReadinessVerdict",
    "evaluate_capabilities",
    "evaluate_readiness",
    "policy_for",
    "repeat_window",
    "validate_config",
]
