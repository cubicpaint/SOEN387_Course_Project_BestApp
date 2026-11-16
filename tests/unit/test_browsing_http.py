"""Check the public barber HTTP interface."""

import pytest
from fastapi.testclient import TestClient
from app.config import AuthSettings, Settings
from app.domain.availability import BarberSummary
from app.main import create_app
from app.services.errors import ServiceUnavailable


class StubBrowsing:
    def list_barbers(self):
        return (BarberSummary("bar_alex", "Alex"),)


@pytest.fixture
def client():
    settings = Settings(DATABASE_URL="postgresql+psycopg://bestapp:browsing-http-test@localhost:5432/bestapp", _env_file=None)
    auth = AuthSettings(JWT_SECRET="browsing-http-tests-private-key-123456", _env_file=None)
    app = create_app(settings, auth)
    app.state.browsing = StubBrowsing()
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http



def test_barber_http_response_contains_only_public_fields(client):
    response = client.get("/barbers")
    assert response.status_code == 200
    assert response.json() == {"barbers": [{"id": "bar_alex", "displayName": "Alex"}]}



@pytest.mark.parametrize("path", ["/barbers?unknown=1", "/barbers?unknown=1&unknown=2"])
def test_unknown_and_repeated_browsing_query_parameters_fail(client, path):
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["retryable"] is False



@pytest.mark.parametrize("path", ["/barbers"])
def test_public_browsing_ignores_invalid_and_duplicate_bearer_headers(client, path):
    response = client.get(path, headers=[("Authorization", "invalid"), ("Authorization", "Bearer invalid")])
    assert response.status_code == 200



@pytest.mark.parametrize("path,method", [("/barbers", "list_barbers")])
def test_browsing_dependency_failure_returns_safe_retryable_503(client, monkeypatch, path, method):
    def unavailable(*args):
        raise ServiceUnavailable

    monkeypatch.setattr(client.app.state.browsing, method, unavailable)
    response = client.get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert response.headers["Retry-After"] == "5"
