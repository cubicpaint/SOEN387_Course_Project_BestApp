"""Boundary, rollback and retry evidence for the complete change transaction."""

from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.data.appointments import AppointmentRepository
from app.data.idempotency import IdempotencyRepository
from app.data.models import AvailabilityBlock
from app.domain.types import ServiceChoice
from app.services.errors import ServiceUnavailable
from tests.integration.booking_helpers import ONE, TWO, START, book, error_code, stored_state
from tests.integration.test_appointment_changes import changes


pytestmark = pytest.mark.integration


@pytest.mark.parametrize("delta,status", [(-1, 200), (0, 200), (1, 422)])
def test_reschedule_original_cutoff_boundary(changes, session_factory, delta, status):
    created = book(changes, choice=ServiceChoice.BOTH)
    before = stored_state(session_factory)
    calls = []
    def clock():
        calls.append(True)
        return START - timedelta(hours=24) + timedelta(microseconds=delta)
    changes.clock = clock
    result = changes.reschedule(ONE, created.body["id"], slot_id="slot_alex_1", expected_version=1, key="boundary")
    assert result.status == status and len(calls) == 1
    if status == 422:
        assert error_code(result) == "change_cutoff_violation"
        assert stored_state(session_factory)["versions"] == before["versions"]


@pytest.mark.parametrize("delta,status", [(-1, 200), (0, 200), (1, 422)])
def test_replacement_two_hour_boundary_preserves_old_on_rejection(changes, session_factory, delta, status):
    created = book(changes, choice=ServiceChoice.BOTH)
    destination = START - timedelta(days=1)
    with session_factory.begin() as session:
        session.add_all(AvailabilityBlock(id=f"slot_early_{index}", barber_id="bar_alex",
            starts_at=destination + timedelta(minutes=30 * index),
            ends_at=destination + timedelta(minutes=30 * (index + 1))) for index in range(2))
    before = stored_state(session_factory)
    changes.clock = lambda: destination - timedelta(hours=2) + timedelta(microseconds=delta)
    result = changes.reschedule(ONE, created.body["id"], slot_id="slot_early_0", expected_version=1, key="early")
    assert result.status == status
    if status == 422:
        assert error_code(result) == "booking_notice_violation"
        assert changes.detail(ONE, created.body["id"]).body == {**created.body, "version": 1}
        assert stored_state(session_factory)["versions"] == before["versions"]


@pytest.mark.parametrize("choice", [ServiceChoice.HAIRCUT, ServiceChoice.BEARD])
def test_single_block_service_can_move_to_same_barber(changes, session_factory, choice):
    created = book(changes, choice=choice)
    result = changes.reschedule(ONE, created.body["id"], slot_id="slot_alex_2", expected_version=1, key="single")
    assert result.status == 200 and result.body["service"] == choice.value
    assert result.body["durationMinutes"] == 30 and stored_state(session_factory)["allocations"] == 1


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
@pytest.mark.parametrize("failure", ["allocations", "appointment", "outcome"])
def test_transient_failure_rolls_back_every_change_and_leaves_key_unbound(changes, session_factory, monkeypatch, action, failure):
    created = book(changes, choice=ServiceChoice.BOTH)
    apt = created.body["id"]
    before = stored_state(session_factory)
    owner, name = {
        "allocations": (AppointmentRepository, "change_allocations"),
        "appointment": (AppointmentRepository, "update_appointment"),
        "outcome": (IdempotencyRepository, "store"),
    }[failure]
    original = getattr(owner, name)
    def failing(*args, **kwargs):
        original(*args, **kwargs)
        raise OperationalError("injected change failure", None, Exception("simulated"))
    monkeypatch.setattr(owner, name, failing)
    def request():
        method = getattr(changes, action)
        return method(ONE, apt, expected_version=1, key="recover",
                      **({"slot_id": "slot_alex_1"} if action == "reschedule" else {}))
    with pytest.raises(ServiceUnavailable):
        request()
    assert stored_state(session_factory) == before
    assert changes.detail(ONE, apt).body == {**created.body, "version": 1}
    monkeypatch.setattr(owner, name, original)
    assert request().status == 200


def test_rejected_move_replays_after_destination_is_freed(changes, session_factory):
    created = book(changes, choice=ServiceChoice.BOTH)
    other = book(changes, TWO, slot="slot_alex_2", key="occupied")
    def request():
        return changes.reschedule(ONE, created.body["id"], slot_id="slot_alex_1", expected_version=1, key="rejected")
    first = request()
    assert error_code(first) == "slot_unavailable"
    assert changes.cancel(TWO, other.body["id"], expected_version=1, key="release").status == 200
    assert request() == first
    assert changes.reschedule(ONE, created.body["id"], slot_id="slot_alex_1", expected_version=1, key="new-intent").status == 200


def test_management_key_binding_includes_operation_target_body_and_precondition(changes, session_factory):
    first = book(changes).body["id"]
    second = book(changes, slot="slot_alex_4", key="second").body["id"]
    assert changes.reschedule(ONE, first, slot_id="slot_alex_1", expected_version=1, key="bound").status == 200
    conflicts = (
        changes.cancel(ONE, first, expected_version=1, key="bound"),
        changes.reschedule(ONE, second, slot_id="slot_alex_1", expected_version=1, key="bound"),
        changes.reschedule(ONE, first, slot_id="slot_alex_2", expected_version=1, key="bound"),
        changes.reschedule(ONE, first, slot_id="slot_alex_1", expected_version=2, key="bound"),
    )
    assert all(error_code(result) == "idempotency_key_conflict" for result in conflicts)
    assert stored_state(session_factory)["outcomes"] == 3
