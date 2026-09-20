"""Authentication and authorization.

These tests exist because auth is the layer where a mistake is worth the most
to an attacker, and because several of them encode rules that are easy to
regress: the role can never come from the client, and identity can never come
from the body.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User, UserRole
from tests.conftest import make_jwt


def test_me_requires_a_token(client: TestClient) -> None:
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_malformed_authorization_header_is_rejected(client: TestClient) -> None:
    response = client.get("/api/auth/me", headers={"Authorization": "Token abc"})
    assert response.status_code == 401


def test_token_signed_with_the_wrong_secret_is_rejected(client: TestClient) -> None:
    token = make_jwt(secret="an-attacker-chosen-secret")
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


def test_expired_token_is_rejected(client: TestClient) -> None:
    token = make_jwt(expires_in=-60)
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_token_for_the_wrong_audience_is_rejected(client: TestClient) -> None:
    token = make_jwt(audience="some-other-service")
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_profile_upsert_creates_a_teacher(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post("/api/auth/profile", json={"name": "Dr Rao"}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Dr Rao"
    assert body["email"] == "teacher@example.edu"
    assert body["role"] == "TEACHER"


def test_profile_upsert_is_idempotent(
    client: TestClient, auth_headers: dict[str, str], db: Session
) -> None:
    client.post("/api/auth/profile", json={"name": "Dr Rao"}, headers=auth_headers)
    client.post("/api/auth/profile", json={"name": "Prof Rao"}, headers=auth_headers)
    users = db.scalars(select(User)).all()
    assert len(users) == 1
    assert users[0].name == "Prof Rao"


def test_client_cannot_assign_its_own_role(
    client: TestClient, auth_headers: dict[str, str], db: Session
) -> None:
    """A body field the schema does not define is a 422, not a silent success."""
    response = client.post(
        "/api/auth/profile", json={"name": "Attacker", "role": "ADMIN"}, headers=auth_headers
    )
    assert response.status_code == 422
    assert db.scalar(select(User)) is None


def test_email_comes_from_the_token_not_the_body(
    client: TestClient, db: Session
) -> None:
    token = make_jwt(email="real@example.edu")
    client.post(
        "/api/auth/profile", json={"name": "Someone"}, headers={"Authorization": f"Bearer {token}"}
    )
    user = db.scalar(select(User))
    assert user is not None
    assert user.email == "real@example.edu"


def test_email_is_normalized_to_lowercase(client: TestClient, db: Session) -> None:
    token = make_jwt(email="Mixed.Case@Example.EDU")
    client.post(
        "/api/auth/profile", json={"name": "Someone"}, headers={"Authorization": f"Bearer {token}"}
    )
    user = db.scalar(select(User))
    assert user is not None
    assert user.email == "mixed.case@example.edu"


def test_me_requires_an_existing_profile(client: TestClient, auth_headers: dict[str, str]) -> None:
    """A valid token without a profile row is authenticated but not yet a user."""
    response = client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "PROFILE_NOT_FOUND"


def test_me_returns_the_database_role_not_a_claimed_one(
    client: TestClient, auth_headers: dict[str, str], db: Session
) -> None:
    client.post("/api/auth/profile", json={"name": "Dr Rao"}, headers=auth_headers)
    user = db.scalar(select(User))
    assert user is not None
    user.role = UserRole.ADMIN
    db.commit()

    response = client.get("/api/auth/me", headers=auth_headers)
    assert response.json()["role"] == "ADMIN"


def test_second_account_cannot_claim_an_existing_email(client: TestClient) -> None:
    first = make_jwt(subject="sub-one", email="shared@example.edu")
    second = make_jwt(subject="sub-two", email="shared@example.edu")
    client.post("/api/auth/profile", json={"name": "First"}, headers={"Authorization": f"Bearer {first}"})
    response = client.post(
        "/api/auth/profile", json={"name": "Second"}, headers={"Authorization": f"Bearer {second}"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
