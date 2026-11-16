"""Durable first outcomes, caller/key scope, uncertain replies and simultaneous retries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete

from app.data.models import Appointment, AvailabilityBlock, IdempotencyRecord, SlotAllocation
from app.domain.types import ServiceChoice
from app.services.appointments import AppointmentService
from tests.integration.booking_helpers import NOW, ONE, START, TWO, book, error_code, seed_booking_data, stored_state


pytestmark = pytest.mark.integration


@pytest.fixture
def booking(session_factory):
    seed_booking_data(session_factory)
    return AppointmentService(session_factory, clock=lambda: NOW)


def simultaneous(*operations):
    ready = Barrier(len(operations))
    def invoke(operation):
        ready.wait(timeout=10)
        return operation()
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        futures = [pool.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=15) for future in futures]


def test_sequential_replay_preserves_json_even_after_eligibility_passes(booking, session_factory):
    original = book(booking, choice=ServiceChoice.BOTH)
    later = AppointmentService(session_factory, clock=lambda: START + timedelta(days=1))
    for _ in range(3):
        repeated = book(later, choice=ServiceChoice.BOTH)
        assert repeated.status == original.status == 201
        assert repeated.body == original.body
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == 1
    assert state["allocations"] == 2


def test_simultaneous_identical_retries_return_one_durable_result(booking, session_factory):
    results = simultaneous(*(lambda: book(booking, choice=ServiceChoice.BOTH) for _ in range(4)))
    assert all(result.status == 201 for result in results)
    assert all(result.body == results[0].body for result in results)
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == 1
    assert state["allocations"] == 2


@pytest.mark.parametrize("changed", ["slot", "service"])
def test_conflicting_reuse_keeps_first_binding_and_response(booking, session_factory, changed):
    original = book(booking)
    conflict = book(booking, slot="slot_sam_0") if changed == "slot" else book(booking, choice=ServiceChoice.BEARD)
    assert conflict.status == 409
    assert error_code(conflict) == "idempotency_key_conflict"
    replay = book(booking)
    assert replay.body == original.body
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == state["outcomes"] == 1


def test_simultaneous_conflicting_bodies_bind_only_one_request(booking, session_factory):
    haircut, beard = simultaneous(lambda: book(booking), lambda: book(booking, choice=ServiceChoice.BEARD))
    assert sorted([haircut.status, beard.status]) == [201, 409]
    winner = haircut if haircut.status == 201 else beard
    loser = beard if haircut.status == 201 else haircut
    assert error_code(loser) == "idempotency_key_conflict"
    with session_factory() as session:
        stored = session.get(IdempotencyRecord, (ONE.id, "booking-one"))
        assert stored.response_body == winner.body
    assert stored_state(session_factory)["appointments"] == 1


def test_same_key_has_independent_callers_and_is_case_sensitive(booking, session_factory):
    assert book(booking, ONE, key="Shared-Key").status == 201
    assert book(booking, TWO, slot="slot_sam_0", key="Shared-Key").status == 201
    assert book(booking, ONE, slot="slot_alex_1", key="shared-key").status == 201
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == 3


@pytest.mark.parametrize("failure", ["missing", "occupied", "overlap", "notice"])
def test_recorded_business_rejection_remains_immutable_after_conditions_change(booking, session_factory, failure):
    slot = "slot_alex_0"
    choice = ServiceChoice.HAIRCUT
    active = booking
    if failure == "missing":
        slot = "slot_missing"
        expected = "slot_not_found"
    elif failure == "occupied":
        assert book(booking, TWO, key="occupier").status == 201
        expected = "slot_unavailable"
    elif failure == "overlap":
        assert book(booking, ONE, slot="slot_sam_0", key="existing").status == 201
        expected = "customer_overlap"
    else:
        active = AppointmentService(session_factory, clock=lambda: START - timedelta(hours=2) + timedelta(microseconds=1))
        expected = "booking_notice_violation"
    rejected = book(active, slot=slot, choice=choice, key="rejected")
    assert error_code(rejected) == expected
    with session_factory.begin() as session:
        session.execute(delete(SlotAllocation))
        session.execute(delete(Appointment))
        if failure == "missing":
            session.add(AvailabilityBlock(
                id="slot_missing", barber_id="bar_alex",
                starts_at=START + timedelta(hours=6),
                ends_at=START + timedelta(hours=6, minutes=30),
            ))
    replay = book(booking, slot=slot, choice=choice, key="rejected")
    assert replay.status == rejected.status
    assert replay.body == rejected.body
    assert book(booking, slot=slot, choice=choice, key="new-attempt").status == 201


def test_operation_is_bound_even_when_payload_fingerprint_matches(booking, session_factory):
    original = book(booking)
    with session_factory.begin() as session:
        stored = session.get(IdempotencyRecord, (ONE.id, "booking-one"))
        stored.operation = "differentOperation"
    conflict = book(booking)
    assert conflict.status == 409
    assert error_code(conflict) == "idempotency_key_conflict"
    with session_factory() as session:
        stored = session.get(IdempotencyRecord, (ONE.id, "booking-one"))
        assert stored.operation == "differentOperation"
        assert stored.response_body == original.body


def test_lost_reply_after_commit_is_recovered_using_original_key(booking, session_factory):
    committed = []
    def response_lost():
        committed.append(book(booking, choice=ServiceChoice.BOTH))
        raise ConnectionError("simulated loss of response after commit")
    with pytest.raises(ConnectionError):
        response_lost()
    retry = book(AppointmentService(session_factory, clock=lambda: NOW), choice=ServiceChoice.BOTH)
    assert retry.status == 201
    assert retry.body == committed[0].body
    state = stored_state(session_factory)
    assert state["appointments"] == state["outcomes"] == 1
    assert state["allocations"] == 2
