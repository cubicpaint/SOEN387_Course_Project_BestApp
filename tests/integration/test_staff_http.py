"""Staff routes exercise real JWT verification and current database assignments."""

from copy import deepcopy

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, text

from app.config import AuthSettings, Settings
from app.data.models import StaffBarber
from app.main import create_app
from app.services.appointments import AppointmentService
from tests.integration.booking_helpers import ONE, STAFF, book, stored_state
from tests.integration.test_staff import NOW, staff_service


pytestmark = pytest.mark.integration
URL = "/barbers/bar_alex/availability?date=2030-01-15"
BODY = {"ranges": [{"startTime": "2030-01-15T09:00:00-05:00", "endTime": "2030-01-15T11:00:00-05:00"}]}


@pytest.fixture
def staff_client(postgres_engine, staff_service):
    with postgres_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = postgres_engine.url.update_query_dict({"options": f"-c search_path={schema}"})
    app = create_app(
        Settings(DATABASE_URL=url.render_as_string(hide_password=False), _env_file=None),
        AuthSettings(JWT_SECRET="private-staff-http-integration-signing-key-12345", _env_file=None),
    )
    app.state.staff = staff_service
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def authorization(client, subject=STAFF.id):
    return "Bearer " + client.app.state.authentication.tokens.issue(subject)


def headers(client, *, key="staff-http"):
    auth = authorization(client)
    current = client.get(URL, headers={"Authorization": auth})
    assert current.status_code == 200
    return {"Authorization": auth, "Idempotency-Key": key, "If-Match": current.headers["ETag"]}


def test_staff_http_write_replay_has_identical_bytes_and_stored_etag(staff_client, session_factory):
    supplied = headers(staff_client)
    first = staff_client.put(URL, json=BODY, headers=supplied)
    assert first.status_code == 200
    assert first.json()["timeZone"] == "America/Toronto"
    assert len(first.json()["blocks"]) == 48
    later = staff_client.put(URL, json={"ranges": []}, headers=headers(staff_client, key="later"))
    assert later.status_code == 200
    replay = staff_client.put(URL, json=BODY, headers=supplied)
    assert replay.status_code == 200 and first.content == replay.content
    assert replay.headers["ETag"] == first.headers["ETag"]
    assert replay.headers["X-Correlation-ID"] != first.headers["X-Correlation-ID"]
    assert replay.headers["Cache-Control"] == "no-store"
    assert stored_state(session_factory)["outcomes"] == 2


def test_staff_http_rejects_invalid_bearers_and_customer_role(staff_client):
    token = authorization(staff_client)
    for path in (URL, "/barbers/bar_alex/appointments"):
        for supplied in ([], [("Authorization", "bad")], [("Authorization", token + "broken")],
                         [("Authorization", token), ("authorization", token)]):
            result = staff_client.get(path, headers=supplied)
            assert result.status_code == 401
            assert result.headers["WWW-Authenticate"] == "Bearer"
        rejected = staff_client.get(path, headers={"Authorization": authorization(staff_client, ONE.id)})
        assert rejected.status_code == 403
    forbidden = staff_client.put(URL, json=BODY, headers={
        **headers(staff_client), "Authorization": authorization(staff_client, ONE.id),
    })
    assert forbidden.status_code == 403


def test_staff_http_strict_timestamps_body_queries_and_headers(staff_client, session_factory):
    supplied = headers(staff_client)
    for invalid in ("2030-01-15T09:00:00", "2030-01-15T14:00:00Z", "2030-01-15 09:00:00-05:00",
                    "2030-01-15T09:00:00-05", "2030-01-15T09:15:00-05:00", "2030-01-15T09:00:00-04:00", 1894701600):
        body = deepcopy(BODY)
        body["ranges"][0]["startTime"] = invalid
        assert staff_client.put(URL, json=body, headers=supplied).status_code == 422
    assert staff_client.put(URL, json={**BODY, "barberId": "bar_sam"}, headers=supplied).status_code == 422
    assert staff_client.put(URL + "&date=2030-01-15", json=BODY, headers=supplied).status_code == 422
    assert staff_client.put(URL + "&unknown=one", json=BODY, headers=supplied).status_code == 422
    for name in ("Idempotency-Key", "If-Match"):
        missing = {key: value for key, value in supplied.items() if key != name}
        assert staff_client.put(URL, json=BODY, headers=missing).status_code == 422
        repeated = list(supplied.items()) + [(name, supplied[name])]
        assert staff_client.put(URL, json=BODY, headers=repeated).status_code == 422
    for match in ("*", "0" * 64, 'W/"' + "0" * 64 + '"', '"' + "A" * 64 + '"'):
        assert staff_client.put(URL, json=BODY, headers={**supplied, "If-Match": match}).status_code == 422
    assert staff_client.put(URL, content='{"ranges":[],"ranges":[]}', headers={**supplied, "Content-Type": "application/json"}).status_code == 400
    assert staff_client.put(URL, content="{}", headers={**supplied, "Content-Type": "text/plain"}).status_code == 415
    assert stored_state(session_factory)["outcomes"] == 0


def test_revoked_assignment_denies_stored_http_outcome(staff_client, session_factory):
    supplied = headers(staff_client)
    assert staff_client.put(URL, json=BODY, headers=supplied).status_code == 200
    with session_factory.begin() as session:
        session.execute(delete(StaffBarber).where(StaffBarber.staff_id == STAFF.id))
    assert staff_client.put(URL, json=BODY, headers=supplied).status_code == 403
    assert staff_client.get(URL, headers={"Authorization": supplied["Authorization"]}).status_code == 403
    assert stored_state(session_factory)["outcomes"] == 1


def test_staff_http_upcoming_view_has_authorized_customer_contact(staff_client, session_factory):
    assert book(AppointmentService(session_factory, clock=lambda: NOW)).status == 201
    auth = {"Authorization": authorization(staff_client)}
    result = staff_client.get("/barbers/bar_alex/appointments", headers=auth)
    assert result.status_code == 200
    assert result.json()["appointments"][0]["customerEmail"] == "usr_one@example.test"
    assert staff_client.get("/barbers/bar_sam/appointments", headers=auth).status_code == 403
    assert staff_client.get("/barbers/bar_sam/availability?date=2030-01-15", headers=auth).status_code == 403
