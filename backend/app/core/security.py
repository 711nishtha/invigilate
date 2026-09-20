"""Credential verification and token minting.

Two credential types exist (docs/api.md):
  * Supabase Auth JWT  -> teachers and admins
  * Attempt token      -> one student, one attempt, nothing else
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import InvalidTokenError

from app.core.config import settings
from app.core.errors import NotAuthenticated

# Unambiguous alphabet for the public quiz code: no O/0, no I/1/L.
_PUBLIC_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PUBLIC_CODE_LENGTH = 6


@dataclass(frozen=True)
class SupabaseIdentity:
    """The verified contents of a Supabase JWT.

    Note what is absent: role. The token proves identity; the database decides
    authority.
    """

    subject: str
    email: str | None
    raw_claims: dict[str, Any]


def verify_supabase_jwt(token: str) -> SupabaseIdentity:
    """Verify signature, expiry and audience locally.

    Local verification means no network round trip per request and no
    server-side session to fall out of sync with Supabase.
    """
    if not settings.supabase_jwt_secret:
        # Failing closed matters: an empty secret must never mean "skip checks".
        raise NotAuthenticated(
            "Authentication is not configured on this server.",
            code="AUTH_NOT_CONFIGURED",
        )
    try:
        claims = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience=settings.supabase_jwt_audience,
            options={"require": ["exp", "sub"]},
        )
    except InvalidTokenError as exc:
        raise NotAuthenticated("Invalid or expired session.", code="INVALID_TOKEN") from exc

    subject = claims.get("sub")
    if not subject:
        raise NotAuthenticated("Token has no subject.", code="INVALID_TOKEN")

    return SupabaseIdentity(
        subject=str(subject),
        email=claims.get("email"),
        raw_claims=claims,
    )


# --- Attempt tokens --------------------------------------------------------


def generate_attempt_token() -> str:
    """Opaque, high-entropy bearer token shown to the student exactly once."""
    return secrets.token_urlsafe(32)


def hash_attempt_token(token: str) -> str:
    """Only the hash is stored, so a database leak does not hand out attempts."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def attempt_token_matches(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_attempt_token(token), stored_hash)


# --- Public quiz codes -----------------------------------------------------


def generate_public_code(length: int = PUBLIC_CODE_LENGTH) -> str:
    """Non-sequential code for the public quiz URL, e.g. /q/8F7K2P.

    Sequential codes would let anyone enumerate other people's quizzes.
    """
    return "".join(secrets.choice(_PUBLIC_CODE_ALPHABET) for _ in range(length))


def generate_attempt_seed() -> int:
    """Cryptographically secure seed driving this student's draw.

    63 bits keeps it inside PostgreSQL's signed bigint.
    """
    return secrets.randbits(63)
