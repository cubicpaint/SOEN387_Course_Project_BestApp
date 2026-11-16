"""Exercise the HTTP foundation without requiring a running database."""

import json
import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.main import create_app
from app.services.health import ReadinessService


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ReadinessService, "is_ready", lambda self: True)
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://bestapp:health-test-secret@localhost:5432/bestapp",
        _env_file=None,
    )
    auth = AuthSettings(JWT_SECRET="private-health-test-signing-key-only-12345", _env_file=None)
    with TestClient(create_app(settings, auth), raise_server_exceptions=False) as http:
        yield http


def test_liveness_does_not_consult_the_database(client, monkeypatch):
    def unexpected_database_check(self):
        raise AssertionError("Liveness must not check database readiness")

    monkeypatch.setattr(ReadinessService, "is_ready", unexpected_database_check)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_a_healthy_database(client):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_unready_database_does_not_make_the_process_unlive(client, monkeypatch):
    monkeypatch.setattr(ReadinessService, "is_ready", lambda self: False)

    ready = client.get("/health/ready")
    live = client.get("/health/live")

    assert ready.status_code == 503
    assert ready.headers["Retry-After"] == "5"
    assert ready.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "The service is temporarily unavailable.",
            "retryable": True,
        }
    }
    assert live.status_code == 200


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
@pytest.mark.parametrize("query", ["?unexpected=1", "?unexpected=1&unexpected=2"])
def test_health_rejects_undeclared_or_repeated_query_parameters(client, path, query):
    response = client.get(path + query)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["retryable"] is False


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
def test_public_health_ignores_invalid_and_repeated_authorization(client, path):
    response = client.get(
        path,
        headers=[("Authorization", "invalid"), ("Authorization", "Bearer invalid")],
    )

    assert response.status_code == 200


def test_responses_have_fresh_server_correlation_ids_and_disable_caching(client):
    responses = [
        client.get("/health/live", headers={"X-Correlation-ID": "caller-controlled"}),
        client.get("/health/ready"),
        client.get("/health/live?unexpected=1"),
    ]

    identifiers = [str(UUID(response.headers["X-Correlation-ID"])) for response in responses]
    assert len(set(identifiers)) == len(responses)
    assert all(response.headers["Cache-Control"] == "no-store" for response in responses)


def test_unexpected_failure_returns_a_safe_error_with_response_headers(client, monkeypatch):
    def fail(self):
        raise RuntimeError("health-test-secret must never reach the HTTP response")

    monkeypatch.setattr(ReadinessService, "is_ready", fail)

    response = client.get("/health/ready")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["retryable"] is True
    assert "health-test-secret" not in response.text
    assert "RuntimeError" not in response.text
    UUID(response.headers["X-Correlation-ID"])
    assert response.headers["Cache-Control"] == "no-store"


def test_request_logs_are_json_and_match_the_response_correlation_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="bestapp.http"):
        response = client.get("/health/live", headers={"Authorization": "Bearer private-test-token"})

    records = [record for record in caplog.records if record.name == "bestapp.http"]
    assert len(records) == 1
    handlers = logging.getLogger("bestapp").handlers
    assert handlers, "The application must install its structured logging handler"
    entry = json.loads(handlers[0].format(records[0]))
    assert entry["event"] == "http_request"
    assert entry["correlation_id"] == response.headers["X-Correlation-ID"]
    assert entry["method"] == "GET"
    assert entry["path"] == "/health/live"
    assert entry["status_code"] == 200
    assert "private-test-token" not in json.dumps(entry)
