"""Protected appointment routes, strict input, and stable HTTP representations."""

from copy import deepcopy
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.config import AuthSettings, Settings
from app.domain.appointments import BookingOutcome
from app.domain.identity import Caller
from app.domain.types import Role, ServiceChoice
from app.main import create_app
from app.services.errors import ServiceUnavailable, Unauthenticated


APPOINTMENT = {
    "id": "apt_one", "barberId": "bar_one", "slotId": "slot_one", "service": "both",
    "durationMinutes": 60, "startTime": "2030-01-15T15:00:00Z", "endTime": "2030-01-15T16:00:00Z",
    "status": "confirmed", "createdAt": "2030-01-14T15:00:00.123456Z",
}
BODY = {"slotId": "slot_one", "service": "both"}
HEADERS = {"Authorization": "Bearer header.payload.signature", "Idempotency-Key": "booking-one"}


@pytest.fixture
def client():
    app = create_app(
        Settings(DATABASE_URL="postgresql+psycopg://test:unit-booking-only@localhost/bestapp", _env_file=None),
        AuthSettings(JWT_SECRET="private-unit-booking-signing-key-only-12345", _env_file=None),
    )
    app.state.authentication = Mock()
    app.state.authentication.authenticate.return_value = Caller("usr_one", Role.CUSTOMER)
    app.state.appointments = Mock()
    app.state.appointments.book.return_value = BookingOutcome(201, deepcopy(APPOINTMENT))
    app.state.appointments.list_own.return_value = (deepcopy(APPOINTMENT),)
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http


def test_booking_returns_direct_appointment_and_forwards_trusted_customer(client):
    response = client.post("/appointments", json=BODY, headers=HEADERS)
    assert response.status_code == 201
    assert response.json() == APPOINTMENT
    client.app.state.appointments.book.assert_called_once_with(
        Caller("usr_one", Role.CUSTOMER), slot_id="slot_one", service=ServiceChoice.BOTH, key="booking-one",
    )


def test_get_appointments_needs_no_json_body_or_content_type(client):
    response = client.get("/appointments", headers={"Authorization": HEADERS["Authorization"]})
    assert response.status_code == 200
    assert response.json() == {"appointments": [APPOINTMENT]}
    client.app.state.appointments.list_own.assert_called_once_with(Caller("usr_one", Role.CUSTOMER))
    client.app.state.appointments.list_own.return_value = ()
    assert client.get("/appointments", headers=HEADERS).json() == {"appointments": []}


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("authorization", [[], [("Authorization", "invalid")], [
    ("Authorization", "Bearer one"), ("Authorization", "Bearer two"),
]])
def test_protected_operations_reject_missing_malformed_or_repeated_bearer(client, method, authorization):
    response = client.request(method, "/appointments", json=BODY if method == "post" else None,
                              headers=[("Idempotency-Key", "one"), *authorization])
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    client.app.state.appointments.book.assert_not_called()
    client.app.state.appointments.list_own.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
def test_staff_is_rejected_before_any_outcome_or_appointments_are_disclosed(client, method):
    client.app.state.authentication.authenticate.return_value = Caller("usr_staff", Role.STAFF)
    response = client.request(method, "/appointments", json=BODY if method == "post" else None, headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    client.app.state.appointments.book.assert_not_called()
    client.app.state.appointments.list_own.assert_not_called()


@pytest.mark.parametrize("key", [None, "", "two words", "\u00e9", "x" * 129])
def test_invalid_keys_do_not_reach_booking_service(client, key):
    headers = {"Authorization": HEADERS["Authorization"]}
    if key is not None:
        # Raw bytes let malformed non-ASCII input reach the HTTP boundary.
        headers["Idempotency-Key"] = key.encode("utf-8")
    response = client.post("/appointments", json=BODY, headers=headers)
    assert response.status_code == 422
    client.app.state.appointments.book.assert_not_called()


def test_duplicate_key_headers_are_rejected(client):
    response = client.post("/appointments", json=BODY, headers=[
        ("Authorization", HEADERS["Authorization"]), ("Idempotency-Key", "one"), ("idempotency-key", "one"),
    ])
    assert response.status_code == 422
    client.app.state.appointments.book.assert_not_called()


@pytest.mark.parametrize("key", ["a", "x" * 128, "A._:-09"])
def test_all_contracted_key_formats_are_accepted(client, key):
    response = client.post("/appointments", json=BODY, headers={**HEADERS, "Idempotency-Key": key})
    assert response.status_code == 201


@pytest.mark.parametrize("field,value", [
    ("customerId", "usr_other"), ("barberId", "bar_other"), ("durationMinutes", 30),
    ("role", "staff"), ("notes", "later feature"),
])
def test_booking_rejects_injected_fields(client, field, value):
    response = client.post("/appointments", json={**BODY, field: value}, headers=HEADERS)
    assert response.status_code == 422
    client.app.state.appointments.book.assert_not_called()


@pytest.mark.parametrize("body", [{}, [], {"slotId": 7, "service": "both"},
                                  {"slotId": "slot_one", "service": "unknown"}])
def test_invalid_request_shapes_do_not_reach_service(client, body):
    response = client.post("/appointments", json=body, headers=HEADERS)
    assert response.status_code == 422
    client.app.state.appointments.book.assert_not_called()


@pytest.mark.parametrize("content,status,media", [
    ("{", 400, "application/json"),
    ('{"slotId":"slot_one","slotId":"slot_two","service":"both"}', 400, "application/json"),
    ("{}", 415, "text/plain"),
])
def test_booking_preserves_strict_json_and_media_handling(client, content, status, media):
    response = client.post("/appointments", content=content, headers={**HEADERS, "Content-Type": media})
    assert response.status_code == status
    client.app.state.appointments.book.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("query", ["?customerId=usr_other", "?unknown=one&unknown=two"])
def test_appointment_operations_accept_no_query_parameters(client, method, query):
    response = client.request(method, "/appointments" + query, json=BODY if method == "post" else None, headers=HEADERS)
    assert response.status_code == 422
    client.app.state.appointments.book.assert_not_called()
    client.app.state.appointments.list_own.assert_not_called()


def test_replay_keeps_body_and_generates_fresh_correlation_headers(client):
    client.app.state.appointments.book.side_effect = [
        BookingOutcome(201, APPOINTMENT), BookingOutcome(201, dict(reversed(list(APPOINTMENT.items())))),
    ]
    first = client.post("/appointments", json=BODY, headers=HEADERS)
    replay = client.post("/appointments", json=BODY, headers=HEADERS)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json() == APPOINTMENT
    assert first.content == replay.content
    assert first.headers["X-Correlation-ID"] != replay.headers["X-Correlation-ID"]
    assert replay.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("failure,status,code", [
    (Unauthenticated(), 401, "unauthenticated"),
    (ServiceUnavailable(), 503, "service_unavailable"),
    (RuntimeError("private database details"), 500, "internal_error"),
])
def test_booking_failures_are_safe_and_have_retry_headers(client, failure, status, code):
    client.app.state.appointments.book.side_effect = failure
    response = client.post("/appointments", json=BODY, headers=HEADERS)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is (status >= 500)
    assert "private database details" not in response.text
    if status == 503:
        assert response.headers["Retry-After"] == "5"
