"""Service permissions and the real bearer dependency without production test routes."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient
import jwt
import pytest

from app.config import AuthSettings, Settings
from app.domain.identity import Caller
from app.domain.types import Role
from app.interface.auth import current_caller
from app.main import create_app
from app.services.authorization import require_customer, require_owner, require_staff_barber
from app.services.errors import Forbidden, Unauthenticated


CUSTOMER = Caller("usr_one", Role.CUSTOMER)
STAFF = Caller("usr_staff", Role.STAFF, ("bar_one",))
SECRET = "private-authorization-http-test-key-only-12345"


def test_service_permissions_work_without_http():
    assert require_customer(CUSTOMER) == CUSTOMER
    assert require_owner(CUSTOMER, "usr_one") == CUSTOMER
    assert require_staff_barber(STAFF, "bar_one") == STAFF


@pytest.mark.parametrize("guard", [
    lambda: require_customer(None), lambda: require_owner(None, "usr_one"),
    lambda: require_staff_barber(None, "bar_one"),
])
def test_service_permissions_require_a_trusted_caller(guard):
    with pytest.raises(Unauthenticated):
        guard()


@pytest.mark.parametrize("guard", [
    lambda: require_customer(STAFF), lambda: require_owner(CUSTOMER, "usr_two"),
    lambda: require_owner(STAFF, "usr_one"), lambda: require_staff_barber(CUSTOMER, "bar_one"),
    lambda: require_staff_barber(STAFF, "bar_two"),
])
def test_service_permissions_reject_wrong_role_owner_and_assignment(guard):
    with pytest.raises(Forbidden):
        guard()


@pytest.fixture
def client(monkeypatch):
    app = create_app(
        Settings(DATABASE_URL="postgresql+psycopg://test:authorization-test-only@localhost/bestapp", _env_file=None),
        AuthSettings(JWT_SECRET=SECRET, _env_file=None),
    )

    def unexpected_database_access():
        raise AssertionError("Invalid bearer authorization must fail before database access")

    monkeypatch.setattr(app.state.authentication, "session_factory", unexpected_database_access)

    @app.get("/test/customer")
    def protected(caller: Annotated[Caller, Depends(current_caller)]):
        return {"id": require_customer(caller).id}

    with TestClient(app, raise_server_exceptions=False) as http:
        yield http


@pytest.mark.parametrize("headers", [
    [], [("Authorization", "")], [("Authorization", "Bearer")], [("Authorization", "Basic abc")],
    [("Authorization", "Bearer a.b.c")], [("Authorization", "Bearer a.b.c extra")],
    [("Authorization", "Bearer a.b.c"), ("Authorization", "Bearer a.b.c")],
])
def test_bearer_dependency_rejects_absent_malformed_and_duplicate_headers(client, headers):
    response = client.get("/test/customer", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("kind", ["expired", "unsigned", "wrong-key", "tampered", "wrong-audience"])
def test_real_bearer_dependency_rejects_invalid_tokens(client, kind):
    now = int(datetime.now(UTC).timestamp())
    claims = {"sub": "usr_one", "iss": "bestapp", "aud": "bestapp-api", "iat": now, "exp": now + 1800}
    key, algorithm = SECRET, "HS256"
    if kind == "expired":
        claims.update(iat=now - 1800, exp=now)
    elif kind == "unsigned":
        key, algorithm = "", "none"
    elif kind == "wrong-key":
        key = "another-private-authorization-test-key-12345"
    elif kind == "wrong-audience":
        claims["aud"] = "different-api"
    token = jwt.encode(claims, key, algorithm=algorithm)
    if kind == "tampered":
        token_parts = token.split(".")
        claims["sub"] = "usr_two"
        token_parts[1] = jwt.encode(claims, key, algorithm=algorithm).split(".")[1]
        token = ".".join(token_parts)
    response = client.get("/test/customer", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_valid_identity_with_wrong_role_gets_403_from_service_guard(client, monkeypatch):
    monkeypatch.setattr(client.app.state.authentication, "authenticate", lambda token: STAFF)
    response = client.get("/test/customer", headers={"Authorization": "Bearer token"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_no_test_security_routes_are_published(client):
    assert "/test/customer" not in client.get("/openapi.json").json()["paths"]
