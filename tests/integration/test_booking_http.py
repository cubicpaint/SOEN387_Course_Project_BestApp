"""Actual bearer identity, HTTP parsing and booking transactions with PostgreSQL."""

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app.config import AuthSettings, Settings
from app.main import create_app
from app.services.tokens import TokenService
from tests.integration.booking_helpers import NOW, ONE, STAFF, TWO, seed_booking_data, stored_state


pytestmark = pytest.mark.integration
SECRET = "private-postgresql-booking-http-test-signing-secret-12345"


@pytest.fixture
def client(session_factory, postgres_engine):
    seed_booking_data(session_factory)
    with postgres_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = postgres_engine.url.update_query_dict({"options": f"-c search_path={schema}"})
    application = create_app(
        Settings(DATABASE_URL=url.render_as_string(hide_password=False), _env_file=None),
        AuthSettings(JWT_SECRET=SECRET, _env_file=None),
    )
    application.state.appointments.clock = lambda: NOW
    with TestClient(application, raise_server_exceptions=False) as http:
        yield http


def headers(caller=ONE, *, key="http-booking"):
    tokens = TokenService(AuthSettings(JWT_SECRET=SECRET, _env_file=None))
    return {
        "Authorization": "Bearer " + tokens.issue(caller.id),
        "Content-Type": "application/json",
        "Idempotency-Key": key,
    }


def test_json_order_and_whitespace_replay_the_same_database_outcome(client, session_factory):
    original = client.post("/appointments", headers=headers(), content='{"slotId":"slot_alex_0","service":"both"}')
    repeated = client.post("/appointments", headers=headers(), content=' { "service" : "both",\n "slotId" : "slot_alex_0" } ')
    assert original.status_code == repeated.status_code == 201
    assert repeated.json() == original.json()
    assert repeated.content == original.content
    assert original.headers["X-Correlation-ID"] != repeated.headers["X-Correlation-ID"]
    assert original.headers["Cache-Control"] == repeated.headers["Cache-Control"] == "no-store"
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == 1
    assert state["allocations"] == 2
    listed = client.get("/appointments", headers=headers())
    assert listed.status_code == 200
    assert listed.json() == {"appointments": [original.json()]}


@pytest.mark.parametrize("malformed,status", [
    ('{"slotId":"slot_alex_0","service":"haircut","customerId":"usr_two"}', 422),
    ('{"slotId":"slot_alex_0","service":"haircut","service":"beard"}', 400),
    ('{"slotId":"slot_alex_0","service":"unsupported"}', 422),
])
def test_invalid_input_leaves_key_available_for_a_corrected_request(client, session_factory, malformed, status):
    rejected = client.post("/appointments", headers=headers(), content=malformed)
    assert rejected.status_code == status
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == state["allocations"] == 0
    corrected = client.post("/appointments", headers=headers(), json={"slotId": "slot_alex_0", "service": "haircut"})
    assert corrected.status_code == 201


def test_actual_customer_routes_enforce_identity_and_staff_permissions(client, session_factory):
    body = {"slotId": "slot_alex_0", "service": "haircut"}
    assert client.post("/appointments", headers=headers(), json=body).status_code == 201
    assert client.get("/appointments", headers=headers(TWO)).json() == {"appointments": []}
    assert client.get("/appointments?customerId=usr_one", headers=headers(TWO)).status_code == 422
    for request in (
        lambda: client.post("/appointments", headers=headers(STAFF), json=body),
        lambda: client.get("/appointments", headers=headers(STAFF)),
    ):
        response = request()
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
    for auth in (None, "Bearer forged.invalid.signature"):
        request_headers = {} if auth is None else {"Authorization": auth}
        response = client.get("/appointments", headers=request_headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == 1
