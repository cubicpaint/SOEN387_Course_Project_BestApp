"""Actual database races preserve version, customer, and allocation invariants."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import select, text

from app.data.appointments import AppointmentRepository
from app.data.models import Appointment, AvailabilityBlock, SlotAllocation, StaffBarber, User
from app.data.staff import StaffRepository
from app.domain.availability import TORONTO
from app.domain.staff_availability import AvailabilityRange
from app.domain.types import ServiceChoice
from app.services.errors import ServiceUnavailable
from app.services.staff import StaffService
from tests.integration.booking_helpers import ONE, TWO, STAFF, START, book, error_code, stored_state
from tests.integration.test_appointment_changes import changes


pytestmark = pytest.mark.integration


def together(*actions):
    barrier = Barrier(len(actions))
    def run(action):
        barrier.wait(10)
        return action()
    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return [future.result(25) for future in [pool.submit(run, action) for action in actions]]


def move(service, apt, *, slot="slot_alex_2", key="move"):
    return service.reschedule(ONE, apt, slot_id=slot, expected_version=1, key=key)


def test_two_same_version_reschedules_have_one_winner(changes, session_factory):
    apt = book(changes).body["id"]
    results = together(lambda: move(changes, apt, key="first"), lambda: move(changes, apt, slot="slot_alex_4", key="second"))
    assert sorted(result.status for result in results) == [200, 412]
    assert changes.detail(ONE, apt).body["version"] == 2
    assert stored_state(session_factory)["allocations"] == 1


def test_cancel_racing_reschedule_cannot_both_change_same_version(changes, session_factory):
    apt = book(changes).body["id"]
    results = together(lambda: move(changes, apt), lambda: changes.cancel(ONE, apt, expected_version=1, key="cancel"))
    assert sorted(result.status for result in results) == [200, 412]
    current = changes.detail(ONE, apt).body
    assert current["version"] == 2
    assert stored_state(session_factory)["allocations"] == (0 if current["status"] == "cancelled" else 1)


def test_identical_concurrent_reschedule_retries_do_not_move_twice(changes, session_factory):
    apt = book(changes).body["id"]
    first, second = together(lambda: move(changes, apt), lambda: move(changes, apt))
    assert first.status == second.status == 200 and first.body == second.body
    assert first.body["version"] == 2
    assert stored_state(session_factory)["outcomes"] == 2


def test_simultaneous_conflicting_reschedules_preserve_first_key_binding(changes, session_factory):
    apt = book(changes).body["id"]
    first, second = together(
        lambda: move(changes, apt, slot="slot_alex_2", key="one-intent"),
        lambda: move(changes, apt, slot="slot_alex_4", key="one-intent"),
    )
    assert sorted(result.status for result in (first, second)) == [200, 409]
    winner = first if first.status == 200 else second
    loser = second if first.status == 200 else first
    assert error_code(loser) == "idempotency_key_conflict"
    assert changes.detail(ONE, apt).body == winner.body
    assert winner.body["version"] == 2
    assert stored_state(session_factory)["outcomes"] == 2


def test_identical_simultaneous_cancellations_release_once(changes, session_factory):
    apt = book(changes, choice=ServiceChoice.BOTH).body["id"]
    first, second = together(
        lambda: changes.cancel(ONE, apt, expected_version=1, key="one-cancellation"),
        lambda: changes.cancel(ONE, apt, expected_version=1, key="one-cancellation"),
    )
    assert first.status == second.status == 200 and first.body == second.body
    assert first.body["version"] == 2 and first.body["status"] == "cancelled"
    state = stored_state(session_factory)
    assert state["allocations"] == 0 and state["outcomes"] == 2
    assert state["versions"]["slot_alex_0"] == state["versions"]["slot_alex_1"] == 3


def test_combined_reschedule_second_block_competition_never_leaves_partial_move(changes, session_factory, monkeypatch):
    apt = book(changes, choice=ServiceChoice.BOTH).body["id"]
    before = stored_state(session_factory)
    barrier = Barrier(2)
    original_claim = AppointmentRepository.claim_block
    original_protect = AppointmentRepository.protect_change_block
    def claim(repository, block):
        if block.id == "slot_alex_3":
            barrier.wait(15)
        return original_claim(repository, block)
    def protect(repository, block, appointment_id, **kwargs):
        if block.id == "slot_alex_3":
            barrier.wait(15)
        return original_protect(repository, block, appointment_id, **kwargs)
    monkeypatch.setattr(AppointmentRepository, "claim_block", claim)
    monkeypatch.setattr(AppointmentRepository, "protect_change_block", protect)
    moved, booked = together(
        lambda: move(changes, apt),
        lambda: book(changes, TWO, slot="slot_alex_3", key="second-block-competitor"),
    )
    assert (moved.status, booked.status) in ((200, 409), (409, 201))
    with session_factory() as session:
        owned = set(session.scalars(select(SlotAllocation.slot_id).where(SlotAllocation.appointment_id == apt)))
    state = stored_state(session_factory)
    if moved.status == 200:
        assert owned == {"slot_alex_2", "slot_alex_3"}
        assert state["allocations"] == 2 and state["appointments"] == 1
    else:
        assert owned == {"slot_alex_0", "slot_alex_1"}
        assert state["allocations"] == 3 and state["appointments"] == 2
        assert all(state["versions"][slot] == before["versions"][slot] for slot in ("slot_alex_0", "slot_alex_1", "slot_alex_2"))


def test_reschedule_competes_with_new_booking_for_destination(changes, session_factory, monkeypatch):
    apt = book(changes).body["id"]
    barrier = Barrier(2)
    original_claim = AppointmentRepository.claim_block
    original_protect = AppointmentRepository.protect_change_block
    def claim(repository, block):
        if block.id == "slot_alex_2":
            barrier.wait(15)
        return original_claim(repository, block)
    def protect(repository, block, appointment_id, **kwargs):
        if block.id == "slot_alex_2":
            barrier.wait(15)
        return original_protect(repository, block, appointment_id, **kwargs)
    monkeypatch.setattr(AppointmentRepository, "claim_block", claim)
    monkeypatch.setattr(AppointmentRepository, "protect_change_block", protect)
    moved, booked = together(lambda: move(changes, apt), lambda: book(changes, TWO, slot="slot_alex_2", key="contender"))
    assert (moved.status, booked.status) in ((200, 409), (409, 201))
    with session_factory() as session:
        allocated = session.get(SlotAllocation, "slot_alex_2")
        assert allocated is not None
        assert allocated.appointment_id == (apt if moved.status == 200 else booked.body["id"])
        assert (session.get(SlotAllocation, "slot_alex_0") is None) == (moved.status == 200)


def test_same_customer_reschedule_and_cross_barber_booking_do_not_overlap(changes, session_factory):
    apt = book(changes).body["id"]
    moved, booked = together(lambda: move(changes, apt), lambda: book(changes, ONE, slot="slot_sam_2", key="cross-barber"))
    assert (moved.status, booked.status) in ((200, 409), (409, 201))
    loser = booked if moved.status == 200 else moved
    assert error_code(loser) == "customer_overlap"
    with session_factory() as session:
        appointments = session.scalars(select(Appointment).where(Appointment.customer_id == ONE.id, Appointment.status == "confirmed").order_by(Appointment.starts_at)).all()
        assert all(left.ends_at <= right.starts_at for left, right in zip(appointments, appointments[1:]))


def test_cancellation_and_booking_released_slot_remain_consistent(changes, session_factory):
    apt = book(changes).body["id"]
    cancelled, booked = together(
        lambda: changes.cancel(ONE, apt, expected_version=1, key="cancel"),
        lambda: book(changes, TWO, key="during-cancel"),
    )
    assert cancelled.status == 200 and booked.status in (201, 409)
    if booked.status == 409:
        booked = book(changes, TWO, key="after-cancel")
        assert booked.status == 201
    with session_factory() as session:
        assert session.get(Appointment, apt).status == "cancelled"
        assert session.get(SlotAllocation, "slot_alex_0").appointment_id == booked.body["id"]
        assert list(session.scalars(select(SlotAllocation).where(SlotAllocation.appointment_id == apt))) == []


@pytest.mark.parametrize("resource", ["customer", "block"])
def test_change_clock_follows_customer_and_block_waits(changes, session_factory, monkeypatch, resource):
    apt = book(changes).body["id"]
    attempted, sampled = Event(), Event()
    decision = [START - timedelta(hours=24)]
    before = stored_state(session_factory)
    def clock():
        sampled.set()
        return decision[0]
    changes.clock = clock
    if resource == "customer":
        original = AppointmentRepository.lock_customer
        def lock(repository, customer_id):
            attempted.set()
            return original(repository, customer_id)
        monkeypatch.setattr(AppointmentRepository, "lock_customer", lock)
        query = select(User).where(User.id == ONE.id).with_for_update()
    else:
        original = AppointmentRepository.protect_change_block
        def protect(repository, block, appointment_id, **kwargs):
            attempted.set()
            return original(repository, block, appointment_id, **kwargs)
        monkeypatch.setattr(AppointmentRepository, "protect_change_block", protect)
        query = select(AvailabilityBlock).where(AvailabilityBlock.id == "slot_alex_0").with_for_update()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session_factory.begin() as holder:
            holder.execute(query)
            future = pool.submit(changes.cancel, ONE, apt, expected_version=1, key="cutoff-wait")
            assert attempted.wait(15) and not sampled.is_set()
            decision[0] += timedelta(microseconds=1)
        result = future.result(15)
    assert sampled.is_set() and result.status == 422 and error_code(result) == "change_cutoff_violation"
    after = stored_state(session_factory)
    assert after["versions"] == before["versions"] and after["allocations"] == 1


def test_change_lock_timeout_leaves_key_unbound(changes, session_factory):
    apt = book(changes).body["id"]
    def short_wait():
        session = session_factory()
        session.execute(text("SET SESSION lock_timeout = '100ms'"))
        session.commit()
        return session
    changes.session_factory = short_wait
    with session_factory.begin() as holder:
        holder.execute(select(User).where(User.id == ONE.id).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(move, changes, apt)
            with pytest.raises(ServiceUnavailable):
                future.result(15)
    assert stored_state(session_factory)["outcomes"] == 1
    assert move(changes, apt).status == 200


def test_change_clock_follows_actual_appointment_row_wait(changes, session_factory, monkeypatch):
    apt = book(changes).body["id"]
    attempted, sampled = Event(), Event()
    decision = [START - timedelta(hours=24)]
    before = stored_state(session_factory)
    original = AppointmentRepository.lock_appointment
    def lock_appointment(repository, appointment_id, customer_id):
        attempted.set()
        return original(repository, appointment_id, customer_id)
    def clock():
        sampled.set()
        return decision[0]
    monkeypatch.setattr(AppointmentRepository, "lock_appointment", lock_appointment)
    changes.clock = clock
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session_factory.begin() as holder:
            holder.execute(select(Appointment).where(Appointment.id == apt).with_for_update())
            future = pool.submit(changes.cancel, ONE, apt, expected_version=1, key="appointment-wait")
            assert attempted.wait(15) and not sampled.is_set()
            decision[0] += timedelta(microseconds=1)
        rejected = future.result(15)
    assert sampled.is_set() and rejected.status == 422
    assert error_code(rejected) == "change_cutoff_violation"
    state = stored_state(session_factory)
    assert state["allocations"] == 1 and state["versions"] == before["versions"]


def test_staff_disable_and_reschedule_destination_have_one_winner(changes, session_factory, monkeypatch):
    apt = book(changes).body["id"]
    with session_factory.begin() as session:
        session.add(StaffBarber(staff_id=STAFF.id, barber_id="bar_alex"))
    staff = StaffService(session_factory, clock=lambda: START - timedelta(days=2))
    revision = staff.availability(STAFF, "bar_alex", START.date())["revision"]
    barrier = Barrier(2)
    original_change = StaffRepository.change_block
    original_protect = AppointmentRepository.protect_change_block
    def change(repository, block, *, enabled):
        if block.id == "slot_alex_2":
            barrier.wait(15)
        return original_change(repository, block, enabled=enabled)
    def protect(repository, block, appointment_id, **kwargs):
        if block.id == "slot_alex_2":
            barrier.wait(15)
        return original_protect(repository, block, appointment_id, **kwargs)
    monkeypatch.setattr(StaffRepository, "change_block", change)
    monkeypatch.setattr(AppointmentRepository, "protect_change_block", protect)
    interval = AvailabilityRange(START.astimezone(TORONTO), (START + timedelta(hours=1)).astimezone(TORONTO))
    moved, edited = together(
        lambda: move(changes, apt),
        lambda: staff.replace_availability(STAFF, "bar_alex", START.date(), (interval,), revision=revision, key="staff-disable"),
    )
    assert (moved.status, edited.status) in ((200, 409), (409, 200))
    with session_factory() as session:
        allocation = session.get(SlotAllocation, "slot_alex_2")
        if moved.status == 200:
            assert allocation is not None and allocation.appointment_id == apt
            assert session.get(AvailabilityBlock, "slot_alex_2").enabled
        else:
            assert allocation is None and session.get(SlotAllocation, "slot_alex_0").appointment_id == apt
            assert not session.get(AvailabilityBlock, "slot_alex_2").enabled
