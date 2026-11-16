"""Check strict HTTP input using a route registered only in tests."""

from unittest.mock import Mock
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from app.config import AuthSettings, Settings
from app.interface.validation import JSONRoute, validate_query
from app.main import create_app


@pytest.fixture
def client():
    app = create_app(
        Settings(DATABASE_URL="postgresql+psycopg://test:unit-login-test-only@localhost/bestapp", _env_file=None),
        AuthSettings(JWT_SECRET="private-unit-login-signing-key-only-12345", _env_file=None),
    )
    app.state.authentication = Mock()
    router = APIRouter(route_class=JSONRoute)

    @router.post('/test/json')
    def echo(request: Request, credentials: dict):
        validate_query(request, set())
        return {}

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http


@pytest.mark.parametrize("body", [
    b'{', b'', b'{"email":"one@example.com","email":"two@example.com","password":"secret"}',
    b'{"unexpected":{"nested":1,"nested":2}}', b'{"password":NaN}', b'\xff',
])
def test_malformed_json_is_rejected_before_schema_validation(client, body):
    response = client.post("/test/json", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"
    client.app.state.authentication.login.assert_not_called()



@pytest.mark.parametrize("media", [None, "text/plain", "application/x-www-form-urlencoded", "application/problem+json"])
def test_unsupported_media_type_is_rejected(client, media):
    headers = {"Content-Type": media} if media else {}
    response = client.post("/test/json", content='{"email":"one@example.com","password":"secret"}', headers=headers)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"



@pytest.mark.parametrize("query", ["?unknown=1", "?unknown=1&unknown=2"])
def test_login_rejects_undeclared_queries(client, query):
    response = client.post("/test/json" + query, json={"email": "one@example.com", "password": "secret"})
    assert response.status_code == 422
