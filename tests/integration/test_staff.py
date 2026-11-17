"""Real PostgreSQL staff permissions, date replacement, retry, and rollback."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.data.idempotency import IdempotencyRepository
from app.data.models import Appointment, AvailabilityBlock, StaffBarber
from app.data.staff import StaffRepository
from app.domain.identity import Caller
from app.domain.staff_availability import AvailabilityRange
from app.domain.types import Role, ServiceChoice
from app.services.appointments import AppointmentService
from app.services.errors import Forbidden, Unauthenticated
from app.services.staff import StaffService
from tests.integration.booking_helpers import ONE, STAFF, START, book, error_code, seed_booking_data, stored_state


pytestmark = pytest.mark.integration
DAY = date(2030, 1, 15)
NOW = START - timedelta(days=2)


@pytest.fixture
def staff_service(session_factory):
    seed_booking_data(session_factory)
    with session_factory.begin() as session:
        session.add(StaffBarber(staff_id=STAFF.id, barber_id="bar_alex"))
    return StaffService(session_factory, clock=lambda: NOW)


def ranges(start="2030-01-15T09:00:00-05:00", end="2030-01-15T11:00:00-05:00"):
    return (AvailabilityRange(datetime.fromisoformat(start), datetime.fromisoformat(end)),)


def replace(service, *, intervals=None, revision=None, key="staff-change", day=DAY):
    revision = revision if revision is not None else service.availability(STAFF, "bar_alex", day)["revision"]
    return service.replace_availability(STAFF, "bar_alex", day, ranges() if intervals is None else intervals, revision=revision, key=key)


def block_state(session_factory):
    with session_factory() as session:
        return {block.id: (block.starts_at, block.ends_at, block.enabled, block.version)
                for block in session.scalars(select(AvailabilityBlock).where(AvailabilityBlock.barber_id == "bar_alex"))}


def test_replacement_materializes_date_preserves_ids_and_can_reenable(staff_service, session_factory):
    original = block_state(session_factory)
    outcome = replace(staff_service)
    assert outcome.status == 200
    assert len(outcome.body["blocks"]) == 48
    assert sum(block["enabled"] for block in outcome.body["blocks"]) == 4
    assert outcome.body["ranges"] == [{"startTime": "2030-01-15T09:00:00-05:00", "endTime": "2030-01-15T11:00:00-05:00"}]
    after = block_state(session_factory)
    assert all(after[key][:2] == value[:2] for key, value in original.items())
    closed = replace(staff_service, intervals=(), key="close")
    assert closed.status == 200 and closed.body["ranges"] == []
    restored = replace(staff_service, key="reopen")
    assert restored.status == 200
    assert {block["slotId"] for block in restored.body["blocks"]} == set(after)


@pytest.mark.parametrize("day,count", [(date(2026, 3, 8), 46), (date(2026, 11, 1), 50)])
def test_staff_closure_materializes_both_dst_dates(staff_service, day, count):
    result = replace(staff_service, intervals=(), day=day)
    assert result.status == 200
    assert len(result.body["blocks"]) == count
    assert not any(block["enabled"] for block in result.body["blocks"])


def test_occupied_removal_rejects_entire_edit(staff_service, session_factory):
    created = book(AppointmentService(session_factory, clock=lambda: NOW), choice=ServiceChoice.BOTH)
    assert created.status == 201
    before = block_state(session_factory)
    outcome = replace(staff_service, intervals=())
    assert outcome.status == 409 and error_code(outcome) == "availability_occupied"
    assert block_state(session_factory) == before
    state = stored_state(session_factory)
    assert state["appointments"] == 1 and state["allocations"] == 2 and state["outcomes"] == 2


def test_retaining_occupied_interval_is_allowed(staff_service, session_factory):
    assert book(AppointmentService(session_factory, clock=lambda: NOW), choice=ServiceChoice.BOTH).status == 201
    result = replace(staff_service)
    assert result.status == 200
    assert sum(block["occupied"] for block in result.body["blocks"]) == 2
    assert stored_state(session_factory)["allocations"] == 2


def test_stale_revision_and_business_rejection_replay_are_immutable(staff_service, session_factory):
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    assert replace(staff_service, revision=revision).status == 200
    stale = replace(staff_service, revision=revision, key="stale")
    assert stale.status == 412 and error_code(stale) == "stale_version"
    assert replace(staff_service, revision=revision, key="stale").body == stale.body
    assert stored_state(session_factory)["outcomes"] == 2


def test_success_replay_after_later_edit_and_conflicting_key_reuse(staff_service, session_factory):
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    first = replace(staff_service, revision=revision)
    assert replace(staff_service, intervals=(), key="later").status == 200
    before = block_state(session_factory)
    replay = replace(staff_service, revision=revision)
    assert replay == first
    assert block_state(session_factory) == before
    conflict = replace(staff_service, revision=revision, intervals=())
    assert conflict.status == 409 and error_code(conflict) == "idempotency_key_conflict"
    assert stored_state(session_factory)["outcomes"] == 2


def test_replay_rechecks_current_database_assignment(staff_service, session_factory):
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    assert replace(staff_service, revision=revision).status == 200
    with session_factory.begin() as session:
        session.execute(delete(StaffBarber).where(StaffBarber.staff_id == STAFF.id))
    with pytest.raises(Forbidden):
        replace(staff_service, revision=revision)
    assert stored_state(session_factory)["outcomes"] == 1


@pytest.mark.parametrize("caller,error", [(None, Unauthenticated), (ONE, Forbidden), (Caller("usr_other_staff", Role.STAFF, ("bar_sam",)), Forbidden)])
def test_staff_services_enforce_permissions_without_http(staff_service, caller, error):
    for operation in (
        lambda: staff_service.availability(caller, "bar_alex", DAY),
        lambda: staff_service.list_appointments(caller, "bar_alex"),
        lambda: staff_service.replace_availability(caller, "bar_alex", DAY, (), revision="0" * 64, key="forbidden"),
    ):
        with pytest.raises(error):
            operation()


def test_staff_upcoming_view_uses_barber_scope_and_controlled_clock(staff_service, session_factory):
    appointment = book(AppointmentService(session_factory, clock=lambda: NOW))
    listed = staff_service.list_appointments(STAFF, "bar_alex")
    assert len(listed) == 1 and listed[0]["id"] == appointment.body["id"]
    assert listed[0]["customerEmail"] == "usr_one@example.test"
    assert "customerId" not in listed[0] and "password_hash" not in listed[0]
    staff_service.clock = lambda: START + timedelta(microseconds=1)
    assert staff_service.list_appointments(STAFF, "bar_alex") == ()
    staff_service.clock = lambda: START
    assert len(staff_service.list_appointments(STAFF, "bar_alex")) == 1
    with session_factory.begin() as session:
        session.get(Appointment, appointment.body["id"]).status = "cancelled"
    assert staff_service.list_appointments(STAFF, "bar_alex") == ()


def test_cas_failure_restores_earlier_block_changes_and_new_blocks(staff_service, session_factory, monkeypatch):
    before = block_state(session_factory)
    original = StaffRepository.change_block
    calls = []
    def fail_second(repository, block, *, enabled):
        calls.append(block.id)
        return original(repository, block, enabled=enabled) if len(calls) == 1 else False
    monkeypatch.setattr(StaffRepository, "change_block", fail_second)
    result = replace(staff_service, intervals=())
    assert result.status == 409 and error_code(result) == "availability_conflict"
    assert len(calls) == 2 and block_state(session_factory) == before
    assert stored_state(session_factory)["outcomes"] == 1


def test_outcome_failure_rolls_back_complete_schedule(staff_service, session_factory, monkeypatch):
    before = block_state(session_factory)
    def fail(*args, **kwargs):
        raise RuntimeError("injected outcome persistence failure")
    monkeypatch.setattr(IdempotencyRepository, "store", fail)
    with pytest.raises(RuntimeError):
        replace(staff_service)
    assert block_state(session_factory) == before
    assert stored_state(session_factory)["outcomes"] == 0


def test_invalid_range_does_not_consume_key(staff_service, session_factory):
    with pytest.raises(ValueError):
        replace(staff_service, intervals=ranges("2030-01-15T09:15:00-05:00"))
    assert stored_state(session_factory)["outcomes"] == 0
    assert replace(staff_service).status == 200
