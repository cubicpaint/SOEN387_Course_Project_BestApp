"""Real JWTs and PostgreSQL exercise owner management through HTTP."""

from datetime import timedelta

import pytest

from tests.integration.booking_helpers import ONE, TWO, STAFF, START, stored_state
from tests.integration.test_booking_http import client, headers


pytestmark = pytest.mark.integration


def create(client):
    client.app.state.appointments.clock = lambda: START - timedelta(days=2)
    response = client.post("/appointments", headers=headers(), json={"slotId": "slot_alex_0", "service": "both"})
    assert response.status_code == 201
    return response


def test_management_http_keeps_historical_replies_separate_from_live_state(client, session_factory):
    original = create(client)
    path = "/appointments/" + original.json()["id"]
    detail = client.get(path, headers=headers())
    assert detail.status_code == 200 and detail.headers["ETag"] == '"1"'
    available = client.get(path + "/availability?date=2030-01-15", headers=headers())
    assert available.status_code == 200
    assert "slot_alex_1" in [slot["slotId"] for slot in available.json()["slots"]]
    supplied = {**headers(key="move"), "If-Match": detail.headers["ETag"]}
    moved = client.put(path, json={"slotId": "slot_alex_1"}, headers=supplied)
    assert moved.status_code == 200 and moved.headers["ETag"] == '"2"'
    cancelled = client.delete(path, headers={**headers(key="cancel"), "If-Match": '"2"'})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    replay = client.put(path, json={"slotId": "slot_alex_1"}, headers=supplied)
    assert replay.status_code == 200 and replay.content == moved.content and replay.headers["ETag"] == moved.headers["ETag"]
    assert replay.headers["X-Correlation-ID"] != moved.headers["X-Correlation-ID"]
    booking_replay = client.post("/appointments", headers=headers(), json={"slotId": "slot_alex_0", "service": "both"})
    assert booking_replay.content == original.content
    assert client.get(path, headers=headers()).json()["status"] == "cancelled"
    assert client.get("/appointments", headers=headers()).json() == {"appointments": []}
    assert stored_state(session_factory)["allocations"] == 0


def test_new_http_routes_authorize_before_returning_other_customer_data(client, session_factory):
    path = "/appointments/" + create(client).json()["id"]
    for supplied, status in ((headers(TWO), 403), (headers(STAFF), 403), ({}, 401),
                             ({"Authorization": "Bearer forged.invalid.signature"}, 401)):
        for method, url, body in (("get", path, None), ("get", path + "/availability?date=2030-01-15", None),
                                  ("put", path, {"slotId": "slot_alex_2"}), ("delete", path, None)):
            result = client.request(method, url, headers={**supplied, "Idempotency-Key": "denied", "If-Match": '"1"'},
                                    **({"json": body} if body else {}))
            assert result.status_code == status
    assert stored_state(session_factory)["outcomes"] == 1


def test_change_http_rejects_injected_fields_and_invalid_preconditions_without_binding(client, session_factory):
    path = "/appointments/" + create(client).json()["id"]
    supplied = {**headers(key="valid-after-input-error"), "If-Match": '"1"'}
    for extra, value in (("service", "haircut"), ("barberId", "bar_sam"), ("customerId", TWO.id), ("version", 1)):
        assert client.put(path, headers=supplied, json={"slotId": "slot_alex_1", extra: value}).status_code == 422
    for match in ("*", "1", '"0"', 'W/"1"', '"01"', '"-1"'):
        assert client.put(path, headers={**supplied, "If-Match": match}, json={"slotId": "slot_alex_1"}).status_code == 422
    for name in ("If-Match", "Idempotency-Key"):
        missing = {key: value for key, value in supplied.items() if key != name}
        assert client.put(path, headers=missing, json={"slotId": "slot_alex_1"}).status_code == 422
        assert client.delete(path, headers=[*supplied.items(), (name, supplied[name])]).status_code == 422
    assert client.request("DELETE", path, headers=supplied, json={}).status_code == 422
    assert client.get(path + "/availability?date=2030-01-15&barberId=bar_sam", headers=supplied).status_code == 422
    assert stored_state(session_factory)["outcomes"] == 1
    assert client.put(path, headers=supplied, json={"slotId": "slot_alex_1"}).status_code == 200
