"""Login input and safe errors through real routes, without a database."""

from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.config import AuthSettings, Settings
from app.domain.identity import Caller
from app.domain.types import Role
from app.main import create_app
from app.services.authentication import LoginResult
from app.services.errors import InvalidCredentials, ServiceUnavailable


@pytest.fixture
def client():
    app = create_app(
        Settings(DATABASE_URL="postgresql+psycopg://test:unit-login-test-only@localhost/bestapp", _env_file=None),
        AuthSettings(JWT_SECRET="private-unit-login-signing-key-only-12345", _env_file=None),
    )
    app.state.authentication = Mock()
    app.state.authentication.login.return_value = LoginResult("header.payload.signature", 1800, Caller("usr_one", Role.CUSTOMER))
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http


def test_login_uses_json_and_preserves_exact_email_and_password(client):
    response = client.post("/auth/login", json={"email": "Customer@EXAMPLE.test", "password": " private password "})

    assert response.status_code == 200
    client.app.state.authentication.login.assert_called_once_with("Customer@EXAMPLE.test", " private password ")
    assert response.json() == {
        "accessToken": "header.payload.signature", "tokenType": "Bearer", "expiresIn": 1800,
        "user": {"id": "usr_one", "role": "customer", "managedBarberIds": []},
    }


@pytest.mark.parametrize("body", [
    b'{', b'', b'{"email":"one@example.com","email":"two@example.com","password":"secret"}',
    b'{"unexpected":{"nested":1,"nested":2}}', b'{"password":NaN}', b'\xff',
])
def test_malformed_json_is_rejected_before_schema_validation(client, body):
    response = client.post("/auth/login", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"
    client.app.state.authentication.login.assert_not_called()


@pytest.mark.parametrize("body", [
    {}, [], None, {"email": "one@example.com"}, {"email": "invalid", "password": "secret"},
    {"email": "@example.com", "password": "secret"}, {"email": "one@", "password": "secret"},
    {"email": "one@@example.com", "password": "secret"},
    {"email": "one@example.com", "password": ""}, {"email": "one@example.com", "password": 123},
    {"email": "one@example.com", "password": "x" * 1025},
    {"email": "one@example.com", "password": "secret", "role": "staff"},
])
def test_schema_violations_receive_safe_validation_errors(client, body):
    import json
    response = client.post("/auth/login", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "secret" not in response.text
    client.app.state.authentication.login.assert_not_called()


@pytest.mark.parametrize("media", [None, "text/plain", "application/x-www-form-urlencoded", "application/problem+json"])
def test_unsupported_media_type_is_rejected(client, media):
    headers = {"Content-Type": media} if media else {}
    response = client.post("/auth/login", content='{"email":"one@example.com","password":"secret"}', headers=headers)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("query", ["?unknown=1", "?unknown=1&unknown=2"])
def test_login_rejects_undeclared_queries(client, query):
    response = client.post("/auth/login" + query, json={"email": "one@example.com", "password": "secret"})
    assert response.status_code == 422


def test_login_remains_public_and_accepts_json_charset(client):
    response = client.post("/auth/login", content='{"email":"one@example.test","password":"secret"}', headers=[
        ("Content-Type", "application/json; charset=utf-8"),
        ("Authorization", "invalid"), ("Authorization", "Bearer invalid"),
    ])
    assert response.status_code == 200


@pytest.mark.parametrize("failure,status,code", [
    (InvalidCredentials(), 401, "invalid_credentials"),
    (ServiceUnavailable(), 503, "service_unavailable"),
    (RuntimeError("password-sentinel"), 500, "internal_error"),
])
def test_login_failures_use_safe_contract_errors(client, failure, status, code):
    client.app.state.authentication.login.side_effect = failure
    response = client.post("/auth/login", json={"email": "one@example.com", "password": "password-sentinel"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is (status >= 500)
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Correlation-ID" in response.headers
    assert "password-sentinel" not in response.text
    if status == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"
    if status == 503:
        assert response.headers["Retry-After"] == "5"


def test_login_logs_exclude_credentials_and_tokens(client, caplog):
    with caplog.at_level("INFO", logger="bestapp.http"):
        client.post("/auth/login", json={"email": "private@example.com", "password": "password-sentinel"})
    records = [r for r in caplog.records if r.name.startswith("bestapp")]
    logged = repr([r.__dict__ for r in records])
    assert "password-sentinel" not in logged
    assert "private@example.com" not in logged
    assert "header.payload.signature" not in logged
