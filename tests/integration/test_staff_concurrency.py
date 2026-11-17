"""Controlled PostgreSQL races between schedule writers and actual bookings."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import select, text

from app.data.appointments import AppointmentRepository
from app.data.models import AvailabilityBlock, SlotAllocation, StaffBarber, User
from app.data.staff import StaffRepository
from app.domain.identity import Caller
from app.domain.types import Role
from app.services.appointments import AppointmentService
from app.services.errors import ServiceUnavailable
from tests.integration.booking_helpers import STAFF, book, error_code, stored_state
from tests.integration.test_staff import DAY, NOW, ranges, replace, staff_service


pytestmark = pytest.mark.integration


def test_booking_commits_while_staff_holds_barber_and_waits_for_block(staff_service, session_factory, monkeypatch):
    """FOR UPDATE on Barber would deadlock with booking's foreign-key check."""
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    claimed, editing = Event(), Event()
    original_claim = AppointmentRepository.claim_block
    original_change = StaffRepository.change_block
    def claim(repository, block):
        result = original_claim(repository, block)
        if block.id == "slot_alex_0" and result:
            claimed.set()
            assert editing.wait(15)
        return result
    def change(repository, block, *, enabled):
        if block.id == "slot_alex_0":
            editing.set()
        return original_change(repository, block, enabled=enabled)
    monkeypatch.setattr(AppointmentRepository, "claim_block", claim)
    monkeypatch.setattr(StaffRepository, "change_block", change)
    with ThreadPoolExecutor(max_workers=2) as pool:
        booking = pool.submit(book, AppointmentService(session_factory, clock=lambda: NOW))
        assert claimed.wait(15)
        edit = pool.submit(replace, staff_service, intervals=(), revision=revision)
        created, rejected = booking.result(20), edit.result(20)
    assert created.status == 201
    assert rejected.status == 409 and error_code(rejected) == "availability_conflict"
    with session_factory() as session:
        assert session.get(AvailabilityBlock, "slot_alex_0").enabled
        assert session.get(SlotAllocation, "slot_alex_0").appointment_id == created.body["id"]
    assert stored_state(session_factory)["allocations"] == 1


def test_staff_disable_wins_before_waiting_booking_claim(staff_service, session_factory, monkeypatch):
    disabled, claiming = Event(), Event()
    original_claim = AppointmentRepository.claim_block
    original_change = StaffRepository.change_block
    def change(repository, block, *, enabled):
        result = original_change(repository, block, enabled=enabled)
        if block.id == "slot_alex_0" and result:
            disabled.set()
            assert claiming.wait(15)
        return result
    def claim(repository, block):
        if block.id == "slot_alex_0":
            claiming.set()
        return original_claim(repository, block)
    monkeypatch.setattr(AppointmentRepository, "claim_block", claim)
    monkeypatch.setattr(StaffRepository, "change_block", change)
    with ThreadPoolExecutor(max_workers=2) as pool:
        edit = pool.submit(replace, staff_service, intervals=())
        assert disabled.wait(15)
        booking = pool.submit(book, AppointmentService(session_factory, clock=lambda: NOW))
        changed, rejected = edit.result(20), booking.result(20)
    assert changed.status == 200
    assert rejected.status == 409 and error_code(rejected) == "slot_unavailable"
    assert stored_state(session_factory)["appointments"] == stored_state(session_factory)["allocations"] == 0


def test_identical_concurrent_staff_retries_make_one_change(staff_service, session_factory):
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    barrier = Barrier(2)
    def request():
        barrier.wait(10)
        return replace(staff_service, revision=revision)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        first, second = [future.result(20) for future in futures]
    assert first.status == second.status == 200 and first.body == second.body
    assert stored_state(session_factory)["outcomes"] == 1


def test_different_staff_intents_with_same_revision_have_one_winner(staff_service, session_factory):
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    other = Caller("usr_other_staff", Role.STAFF, ("bar_alex",))
    with session_factory.begin() as session:
        session.add(User(id=other.id, email="other-staff@example.test", role="staff", password_hash="unused"))
        session.flush()
        session.add(StaffBarber(staff_id=other.id, barber_id="bar_alex"))
    barrier = Barrier(2)
    def request(caller, key):
        barrier.wait(10)
        return staff_service.replace_availability(caller, "bar_alex", DAY, ranges(), revision=revision, key=key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request, caller, key) for caller, key in ((STAFF, "one"), (other, "two"))]
        results = [future.result(20) for future in futures]
    assert sorted(result.status for result in results) == [200, 412]
    assert stored_state(session_factory)["outcomes"] == 2


def test_staff_block_lock_timeout_does_not_consume_key(staff_service, session_factory):
    revision = staff_service.availability(STAFF, "bar_alex", DAY)["revision"]
    def short_wait():
        session = session_factory()
        session.execute(text("SET SESSION lock_timeout = '100ms'"))
        session.commit()
        return session
    staff_service.session_factory = short_wait
    with session_factory.begin() as holder:
        holder.execute(select(AvailabilityBlock).where(AvailabilityBlock.id == "slot_alex_0").with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(replace, staff_service, revision=revision, intervals=())
            with pytest.raises(ServiceUnavailable):
                result.result(10)
    assert stored_state(session_factory)["outcomes"] == 0
    assert replace(staff_service, revision=revision, intervals=()).status == 200
