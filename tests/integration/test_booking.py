"""Booking rules, ownership and rollback against the migrated PostgreSQL schema."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.data.models import Appointment, AvailabilityBlock, IdempotencyRecord, SlotAllocation
from app.domain.types import ServiceChoice
from app.services.appointments import AppointmentService
from app.services.errors import Forbidden, ServiceUnavailable, Unauthenticated
from tests.integration.booking_helpers import NOW, ONE, STAFF, START, TWO, book, error_code, seed_booking_data, stored_state


pytestmark = pytest.mark.integration


@pytest.fixture
def booking(session_factory):
    seed_booking_data(session_factory)
    return AppointmentService(session_factory, clock=lambda: NOW)


@pytest.mark.parametrize("choice,minutes,blocks", [
    (ServiceChoice.HAIRCUT, 30, 1), (ServiceChoice.BEARD, 30, 1), (ServiceChoice.BOTH, 60, 2),
])
def test_booking_persists_full_service_and_matching_outcome(booking, session_factory, choice, minutes, blocks):
    result = book(booking, choice=choice)
    assert result.status == 201
    body = result.body
    assert body["service"] == choice
    assert body["durationMinutes"] == minutes
    assert body["slotId"] == "slot_alex_0"
    assert body["barberId"] == "bar_alex"
    assert body["status"] == "confirmed"
    with session_factory() as session:
        appointment = session.get(Appointment, body["id"])
        assert appointment.customer_id == ONE.id
        assert appointment.ends_at - appointment.starts_at == timedelta(minutes=minutes)
        allocations = session.scalars(select(SlotAllocation).where(SlotAllocation.appointment_id == appointment.id)).all()
        assert len(allocations) == blocks
        outcome = session.get(IdempotencyRecord, (ONE.id, "booking-one"))
        assert outcome.response_status == 201
        assert outcome.response_body == body
        assert all(session.get(AvailabilityBlock, row.slot_id).version == 2 for row in allocations)


@pytest.mark.parametrize("offset,status,code", [
    (timedelta(microseconds=-1), 201, None),
    (timedelta(0), 201, None),
    (timedelta(microseconds=1), 422, "booking_notice_violation"),
])
def test_notice_boundary_uses_one_trusted_decision_instant(session_factory, offset, status, code):
    seed_booking_data(session_factory)
    calls = []
    def clock():
        calls.append(True)
        return START - timedelta(hours=2) + offset
    result = book(AppointmentService(session_factory, clock=clock))
    assert result.status == status
    assert len(calls) == 1
    if code:
        assert error_code(result) == code
        state = stored_state(session_factory)
        assert state["appointments"] == state["allocations"] == 0
        assert state["outcomes"] == 1


@pytest.mark.parametrize("caller,error", [(None, Unauthenticated), (STAFF, Forbidden)])
def test_service_authorization_precedes_booking_and_listing(booking, session_factory, caller, error):
    with pytest.raises(error):
        book(booking, caller)
    with pytest.raises(error):
        booking.list_own(caller)
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == state["outcomes"] == 0


def test_own_listing_is_private_ordered_and_excludes_started_or_cancelled(booking, session_factory):
    assert booking.list_own(ONE) == ()
    later = book(booking, slot="slot_alex_2", key="later")
    first = book(booking, slot="slot_alex_0", key="first")
    other = book(booking, TWO, slot="slot_sam_0", key="other")
    cancelled = book(booking, slot="slot_alex_4", key="cancelled")
    with session_factory.begin() as session:
        session.get(Appointment, cancelled.body["id"]).status = "cancelled"
    assert [row["id"] for row in booking.list_own(ONE)] == [first.body["id"], later.body["id"]]
    assert [row["id"] for row in booking.list_own(TWO)] == [other.body["id"]]
    at_start = AppointmentService(session_factory, clock=lambda: START)
    assert [row["id"] for row in at_start.list_own(ONE)] == [first.body["id"], later.body["id"]]
    after_start = AppointmentService(session_factory, clock=lambda: START + timedelta(microseconds=1))
    assert [row["id"] for row in after_start.list_own(ONE)] == [later.body["id"]]


@pytest.mark.parametrize("mutation", ["missing", "disabled", "gap", "other-barber"])
def test_complete_interval_is_required_without_partial_allocation(booking, session_factory, mutation):
    with session_factory.begin() as session:
        block = session.get(AvailabilityBlock, "slot_alex_1")
        if mutation == "missing":
            session.delete(block)
        elif mutation == "disabled":
            block.enabled = False
        elif mutation == "gap":
            block.starts_at += timedelta(minutes=1)
            block.ends_at += timedelta(minutes=1)
        else:
            # Remove Sam's block first to keep its unique barber/start identity valid.
            session.execute(delete(AvailabilityBlock).where(AvailabilityBlock.id == "slot_sam_1"))
            block.barber_id = "bar_sam"
    result = book(booking, choice=ServiceChoice.BOTH)
    assert result.status == 409
    assert error_code(result) == "slot_unavailable"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 0
    assert set(state["versions"].values()) == {1}


def test_occupied_continuation_does_not_claim_the_first_block(booking, session_factory):
    assert book(booking, TWO, slot="slot_alex_1", key="occupied").status == 201
    result = book(booking, choice=ServiceChoice.BOTH)
    assert result.status == 409
    assert error_code(result) == "slot_unavailable"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 1
    assert state["versions"]["slot_alex_0"] == 1


def test_customer_overlap_checks_whole_interval_and_allows_back_to_back(booking, session_factory):
    assert book(booking, slot="slot_alex_0", choice=ServiceChoice.BOTH, key="combined").status == 201
    conflict = book(booking, slot="slot_sam_1", key="cross-barber")
    assert conflict.status == 409
    assert error_code(conflict) == "customer_overlap"
    assert book(booking, slot="slot_sam_2", key="adjacent").status == 201
    state = stored_state(session_factory)
    assert state["appointments"] == 2
    assert state["allocations"] == 3


def test_failed_second_compare_and_swap_rolls_back_first_version(booking, session_factory, monkeypatch):
    from app.data.appointments import AppointmentRepository
    original = AppointmentRepository.claim_block
    calls = []
    def competing_claim(repository, block):
        calls.append(block)
        return original(repository, block) if len(calls) == 1 else False
    monkeypatch.setattr(AppointmentRepository, "claim_block", competing_claim)
    result = book(booking, choice=ServiceChoice.BOTH)
    assert len(calls) == 2
    assert result.status == 409
    assert error_code(result) == "slot_unavailable"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 0
    assert set(state["versions"].values()) == {1}
    assert state["outcomes"] == 1


def test_exception_after_allocation_rolls_back_every_write_and_does_not_bind_key(booking, session_factory, monkeypatch):
    from app.data.appointments import AppointmentRepository
    original = AppointmentRepository.add_appointment
    def failed_response(repository, **kwargs):
        original(repository, **kwargs)
        raise OperationalError("injected database failure", None, Exception("simulated"))
    monkeypatch.setattr(AppointmentRepository, "add_appointment", failed_response)
    with pytest.raises(ServiceUnavailable):
        book(booking, choice=ServiceChoice.BOTH)
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == state["outcomes"] == 0
    assert set(state["versions"].values()) == {1}
    monkeypatch.setattr(AppointmentRepository, "add_appointment", original)
    assert book(booking, choice=ServiceChoice.BOTH).status == 201


def test_new_service_and_database_pool_recover_a_committed_booking(booking, session_factory, postgres_engine):
    committed = book(booking, choice=ServiceChoice.BOTH)
    postgres_engine.dispose()
    restarted = AppointmentService(session_factory, clock=lambda: START + timedelta(days=1))
    replay = book(restarted, choice=ServiceChoice.BOTH)
    assert replay.status == committed.status
    assert replay.body == committed.body
    state = stored_state(session_factory)
    assert state["appointments"] == 1
    assert state["allocations"] == 2
    assert state["outcomes"] == 1


def test_failure_while_storing_outcome_rolls_back_appointment_and_allocations(booking, session_factory, monkeypatch):
    from app.data.idempotency import IdempotencyRepository
    original = IdempotencyRepository.store
    def failed_commit(repository, **kwargs):
        original(repository, **kwargs)
        raise OperationalError("injected outcome persistence failure", None, Exception("simulated"))
    monkeypatch.setattr(IdempotencyRepository, "store", failed_commit)
    with pytest.raises(ServiceUnavailable):
        book(booking, choice=ServiceChoice.BOTH)
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == state["outcomes"] == 0
    assert set(state["versions"].values()) == {1}
