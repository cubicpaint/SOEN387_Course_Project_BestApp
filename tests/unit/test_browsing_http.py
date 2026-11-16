"""Exercise browsing HTTP validation and safe responses without a database."""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from app.config import AuthSettings, Settings
from app.domain.availability import AvailabilityResult, AvailableStart, BarberSummary, service_duration_minutes
from app.domain.types import ServiceChoice
from app.main import create_app
from app.services.errors import BarberNotFound, ServiceUnavailable


class StubBrowsing:
    def list_barbers(self):
        return (BarberSummary("bar_alex", "Alex"),)

    def list_availability(self, barber_id, local_date, service):
        assert (barber_id, local_date, service) == ("bar_alex", date(2026, 11, 17), ServiceChoice.BOTH)
        return AvailabilityResult(
            barber_id, local_date, service, service_duration_minutes(service),
            (AvailableStart("slot_original", datetime(2026, 11, 17, 15, tzinfo=UTC), datetime(2026, 11, 17, 16, tzinfo=UTC)),),
        )


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


def test_availability_http_response_uses_whole_duration_and_utc_z(client):
    response = client.get("/availability?barberId=bar_alex&date=2026-11-17&service=both")
    assert response.status_code == 200
    assert response.json() == {
        "barberId": "bar_alex", "date": "2026-11-17", "timeZone": "America/Toronto", "service": "both", "durationMinutes": 60,
        "slots": [{"slotId": "slot_original", "startTime": "2026-11-17T15:00:00Z", "endTime": "2026-11-17T16:00:00Z"}],
    }
    assert response.headers["Cache-Control"] == "no-store"
    UUID(response.headers["X-Correlation-ID"])


@pytest.mark.parametrize("path", ["/barbers?unknown=1", "/barbers?unknown=1&unknown=2", "/availability?barberId=bar_alex&date=2026-11-17&service=both&unknown=1", "/availability?barberId=bar_alex&barberId=bar_alex&date=2026-11-17&service=both", "/availability?barberId=bar_alex&date=2026-11-17&date=2026-11-17&service=both", "/availability?barberId=bar_alex&date=2026-11-17&service=both&service=both"])
def test_unknown_and_repeated_browsing_query_parameters_fail(client, path):
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["retryable"] is False


@pytest.mark.parametrize("changes", [{"barberId": "wrong"}, {"barberId": "bar_" + "a" * 65}, {"barberId": ""}, {"date": "2026-02-30"}, {"date": "2026-1-17"}, {"date": "20261117"}, {"date": "2026-11-17T00:00:00Z"}, {"date": "0000-01-01"}, {"service": "unsupported"}, {"service": ""}])
def test_invalid_browsing_values_receive_validation_error(client, changes):
    params = {"barberId": "bar_alex", "date": "2026-11-17", "service": "both"} | changes
    response = client.get("/availability", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("missing", ["barberId", "date", "service"])
def test_availability_requires_all_three_query_values(client, missing):
    params = {"barberId": "bar_alex", "date": "2026-11-17", "service": "both"}
    del params[missing]
    response = client.get("/availability", params=params)
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/barbers", "/availability?barberId=bar_alex&date=2026-11-17&service=both"])
def test_public_browsing_ignores_invalid_and_duplicate_bearer_headers(client, path):
    response = client.get(path, headers=[("Authorization", "invalid"), ("Authorization", "Bearer invalid")])
    assert response.status_code == 200


@pytest.mark.parametrize("path,method", [("/barbers", "list_barbers"), ("/availability?barberId=bar_alex&date=2026-11-17&service=both", "list_availability")])
def test_browsing_dependency_failure_returns_safe_retryable_503(client, monkeypatch, path, method):
    def unavailable(*args):
        raise ServiceUnavailable

    monkeypatch.setattr(client.app.state.browsing, method, unavailable)
    response = client.get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert response.headers["Retry-After"] == "5"


def test_unknown_barber_returns_documented_404(client, monkeypatch):
    def missing(*args):
        raise BarberNotFound

    monkeypatch.setattr(client.app.state.browsing, "list_availability", missing)
    response = client.get("/availability?barberId=bar_unknown&date=2026-11-17&service=both")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "barber_not_found"
