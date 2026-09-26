"""Structured JSON logging with secret redaction.

Logs are the audit trail for exam integrity, so they carry the domain events
listed in docs/architecture.md. They must never carry credentials or answer
keys.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.core.config import settings

# Keys whose values are replaced before a record is serialized. Matching is on
# substring, so "supabase_jwt_secret" and "jwt" both redact.
_REDACT_KEYS = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "jwt",
    "service_role",
    "correct_option",
    "numeric_answer",
    # An evidence object path is the only secret protecting a recording of a
    # student, and a signed URL is a bearer credential for it. Neither is
    # logged deliberately; this makes an accidental one harmless too.
    "object_path",
    "signed_url",
)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _redact(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _REDACT_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact(value, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # Uvicorn installs its own handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# --- Domain events ---------------------------------------------------------
# The vocabulary from docs/architecture.md section 40. Using the constant rather
# than a literal keeps log queries reliable.
class Event:
    QUIZ_CREATED = "QUIZ_CREATED"
    QUIZ_PUBLISHED = "QUIZ_PUBLISHED"
    VARIANT_GENERATION_STARTED = "VARIANT_GENERATION_STARTED"
    VARIANT_GENERATION_COMPLETED = "VARIANT_GENERATION_COMPLETED"
    VARIANT_GENERATION_FAILED = "VARIANT_GENERATION_FAILED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ANSWER_SUBMITTED = "ANSWER_SUBMITTED"
    INTEGRITY_EVENT = "INTEGRITY_EVENT"
    AUTO_SUBMISSION = "AUTO_SUBMISSION"
    TIME_EXPIRED = "TIME_EXPIRED"
    MANUAL_SUBMISSION = "MANUAL_SUBMISSION"
    EXPORT_CREATED = "EXPORT_CREATED"
    EMAIL_SENT = "EMAIL_SENT"
    EMAIL_FAILED = "EMAIL_FAILED"
    # --- Phase 5: proctoring ---------------------------------------------
    PRECHECK_STARTED = "PRECHECK_STARTED"
    PRECHECK_BLOCKED = "PRECHECK_BLOCKED"
    PRECHECK_EVALUATED = "PRECHECK_EVALUATED"
    PRECHECK_AUTHORIZED = "PRECHECK_AUTHORIZED"
    PRECHECK_CONSUMED = "PRECHECK_CONSUMED"
    PROCTORING_HEARTBEAT = "PROCTORING_HEARTBEAT"
    PROCTORING_DEGRADED = "PROCTORING_DEGRADED"
    PROCTORING_REVIEWED = "PROCTORING_REVIEWED"
    EVIDENCE_STORED = "EVIDENCE_STORED"
    EVIDENCE_STORE_FAILED = "EVIDENCE_STORE_FAILED"
    EVIDENCE_PURGED = "EVIDENCE_PURGED"


_domain_logger = get_logger("securequiz.event")


def log_event(event: str, **fields: Any) -> None:
    """Emit one structured domain event."""
    _domain_logger.info(event, extra={"event": event, **fields})
