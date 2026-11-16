"""Deterministic competing transactions; unsafe behavior exists only in this test."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, Lock, local

import pytest
from sqlalchemy import select, text

from app.data.appointments import AppointmentRepository
from app.data.models import Appointment, AvailabilityBlock, SlotAllocation, User
from app.domain.types import ServiceChoice
from app.services.appointments import AppointmentService
from app.services.errors import ServiceUnavailable
from tests.integration.booking_helpers import NOW, ONE, START, TWO, book, error_code, seed_booking_data, stored_state


pytestmark = pytest.mark.integration


def run_together(*operations):
    start = Barrier(len(operations))
    def call(operation):
        start.wait(timeout=10)
        return operation()
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        futures = [pool.submit(call, operation) for operation in operations]
        return [future.result(timeout=15) for future in futures]


def synchronize_first_claim(monkeypatch):
    """Both callers read versions before either can claim its first block."""
    ready = Barrier(2)
    progress = local()
    original = AppointmentRepository.claim_block
    def claim(repository, block):
        if not getattr(progress, "started", False):
            progress.started = True
            ready.wait(timeout=10)
        return original(repository, block)
    monkeypatch.setattr(AppointmentRepository, "claim_block", claim)


def test_two_customers_cannot_allocate_the_same_slot(session_factory, monkeypatch):
    seed_booking_data(session_factory)
    service = AppointmentService(session_factory, clock=lambda: NOW)
    synchronize_first_claim(monkeypatch)
    results = run_together(lambda: book(service, ONE), lambda: book(service, TWO))
    assert sorted(result.status for result in results) == [201, 409]
    assert error_code(next(result for result in results if result.status == 409)) == "slot_unavailable"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 1
    assert state["outcomes"] == 2
    assert state["versions"]["slot_alex_0"] == 2


def test_combined_and_single_compete_without_overselling_or_partial_claims(session_factory, monkeypatch):
    seed_booking_data(session_factory)
    service = AppointmentService(session_factory, clock=lambda: NOW)
    synchronize_first_claim(monkeypatch)
    combined, single = run_together(
        lambda: book(service, ONE, choice=ServiceChoice.BOTH),
        lambda: book(service, TWO, slot="slot_alex_1"),
    )
    assert sorted([combined.status, single.status]) == [201, 409]
    state = stored_state(session_factory)
    assert state["appointments"] == 1
    expected_blocks = 2 if combined.status == 201 else 1
    assert state["allocations"] == expected_blocks
    assert state["versions"]["slot_alex_0"] == (2 if combined.status == 201 else 1)
    assert state["versions"]["slot_alex_1"] == 2
    with session_factory() as session:
        assert len(set(session.scalars(select(SlotAllocation.appointment_id)))) == 1


def test_customer_lock_serializes_overlapping_bookings_across_barbers(session_factory, monkeypatch):
    seed_booking_data(session_factory)
    service = AppointmentService(session_factory, clock=lambda: NOW)
    original = AppointmentRepository.lock_customer
    second_attempt = Event()
    mutex = Lock()
    attempts = 0
    def customer_lock(repository, customer_id):
        nonlocal attempts
        with mutex:
            attempts += 1
            index = attempts
        if index == 2:
            second_attempt.set()
        locked = original(repository, customer_id)
        if index == 1:
            assert second_attempt.wait(timeout=10)
        return locked
    monkeypatch.setattr(AppointmentRepository, "lock_customer", customer_lock)
    results = run_together(
        lambda: book(service, ONE, key="alex"),
        lambda: book(service, ONE, slot="slot_sam_0", key="sam"),
    )
    assert sorted(result.status for result in results) == [201, 409]
    assert error_code(next(result for result in results if result.status == 409)) == "customer_overlap"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 1
    assert state["outcomes"] == 2


def test_unsafe_overlap_check_without_customer_lock_reproduces_race(session_factory, monkeypatch):
    """Demonstrate the same schema permits the race if service protection is removed.

    Only this isolated schema and monkeypatched service omit the lock. There is
    no unsafe API mode or weakened database constraint in the application.
    """
    seed_booking_data(session_factory)
    service = AppointmentService(session_factory, clock=lambda: NOW)
    both_read = Barrier(2)
    original = AppointmentRepository.customer_overlaps
    def unsafe_check(repository, customer_id, starts_at, ends_at):
        overlaps = original(repository, customer_id, starts_at, ends_at)
        assert overlaps is False
        both_read.wait(timeout=10)
        return overlaps
    monkeypatch.setattr(AppointmentRepository, "lock_customer", lambda repository, customer_id: True)
    monkeypatch.setattr(AppointmentRepository, "customer_overlaps", unsafe_check)
    results = run_together(
        lambda: book(service, ONE, key="alex"),
        lambda: book(service, ONE, slot="slot_sam_0", key="sam"),
    )
    assert [result.status for result in results] == [201, 201]
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 2
    with session_factory() as session:
        appointments = session.scalars(select(Appointment)).all()
        assert appointments[0].customer_id == appointments[1].customer_id
        assert appointments[0].starts_at < appointments[1].ends_at
        assert appointments[1].starts_at < appointments[0].ends_at


def test_notice_clock_is_sampled_after_customer_lock_wait(session_factory, monkeypatch):
    seed_booking_data(session_factory)
    attempted = Event()
    sampled = Event()
    decision = [START - timedelta(hours=2)]
    original = AppointmentRepository.lock_customer
    def lock_customer(repository, customer_id):
        attempted.set()
        return original(repository, customer_id)
    def clock():
        sampled.set()
        return decision[0]
    monkeypatch.setattr(AppointmentRepository, "lock_customer", lock_customer)
    service = AppointmentService(session_factory, clock=clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session_factory.begin() as holder:
            holder.execute(select(User).where(User.id == ONE.id).with_for_update())
            future = pool.submit(book, service)
            assert attempted.wait(timeout=10)
            assert not sampled.is_set()
            decision[0] += timedelta(microseconds=1)
        result = future.result(timeout=10)
    assert sampled.is_set()
    assert result.status == 422
    assert error_code(result) == "booking_notice_violation"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 0
    assert state["outcomes"] == 1


def test_postgres_lock_timeout_is_transient_and_does_not_consume_the_key(session_factory):
    seed_booking_data(session_factory)
    def short_wait_session():
        session = session_factory()
        session.execute(text("SET SESSION lock_timeout = '100ms'"))
        session.commit()
        return session
    service = AppointmentService(short_wait_session, clock=lambda: NOW)
    with session_factory.begin() as holder:
        holder.execute(select(User).where(User.id == ONE.id).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(book, service)
            with pytest.raises(ServiceUnavailable):
                future.result(timeout=10)
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == state["outcomes"] == 0
    assert book(service).status == 201


def test_notice_clock_is_sampled_after_a_conditional_slot_update_wait(session_factory, monkeypatch):
    seed_booking_data(session_factory)
    attempted = Event()
    sampled = Event()
    decision = [START - timedelta(hours=2)]
    original = AppointmentRepository.claim_block
    def claim(repository, block):
        attempted.set()
        return original(repository, block)
    def clock():
        sampled.set()
        return decision[0]
    monkeypatch.setattr(AppointmentRepository, "claim_block", claim)
    service = AppointmentService(session_factory, clock=clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session_factory.begin() as holder:
            holder.execute(select(AvailabilityBlock).where(AvailabilityBlock.id == "slot_alex_0").with_for_update())
            future = pool.submit(book, service)
            assert attempted.wait(timeout=10)
            assert not sampled.is_set()
            decision[0] += timedelta(microseconds=1)
        result = future.result(timeout=10)
    assert sampled.is_set()
    assert result.status == 422
    assert error_code(result) == "booking_notice_violation"
    state = stored_state(session_factory)
    assert state["appointments"] == state["allocations"] == 0
    assert state["versions"]["slot_alex_0"] == 1
