"""Real PostgreSQL management decisions use an injected server clock."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.data.models import Appointment, SlotAllocation
from app.domain.types import ServiceChoice
from app.services.appointments import AppointmentService
from app.services.errors import Forbidden
from tests.integration.booking_helpers import ONE, TWO, START, book, error_code, seed_booking_data, stored_state


pytestmark = pytest.mark.integration


@pytest.fixture
def changes(session_factory):
    seed_booking_data(session_factory, count=8)
    return AppointmentService(session_factory, clock=lambda: START - timedelta(days=2))


def test_cancel_retains_history_releases_all_blocks_and_replays_after_cutoff(changes, session_factory):
    created = book(changes, choice=ServiceChoice.BOTH)
    apt = created.body["id"]
    first = changes.cancel(ONE, apt, expected_version=1, key="cancel")
    assert first.status == 200
    assert first.body == {**created.body, "status": "cancelled", "version": 2}
    assert changes.detail(ONE, apt).body == first.body
    assert changes.list_own(ONE) == ()
    changes.clock = lambda: START + timedelta(days=1)
    assert changes.cancel(ONE, apt, expected_version=1, key="cancel") == first
    assert book(changes, choice=ServiceChoice.BOTH) == created
    state = stored_state(session_factory)
    assert state["appointments"] == 1 and state["allocations"] == 0 and state["outcomes"] == 2
    assert state["versions"]["slot_alex_0"] == state["versions"]["slot_alex_1"] == 3


@pytest.mark.parametrize("delta,status", [(-1, 200), (0, 200), (1, 422)])
def test_cancellation_exact_controlled_cutoff(changes, session_factory, delta, status):
    apt = book(changes, choice=ServiceChoice.BOTH).body["id"]
    before = stored_state(session_factory)
    calls = []
    def clock():
        calls.append(True)
        return START - timedelta(hours=24) + timedelta(microseconds=delta)
    changes.clock = clock
    result = changes.cancel(ONE, apt, expected_version=1, key="cutoff")
    assert result.status == status and len(calls) == 1
    if status == 422:
        assert error_code(result) == "change_cutoff_violation"
        after = stored_state(session_factory)
        assert after["versions"] == before["versions"] and after["allocations"] == 2


def test_cross_customer_management_does_not_consume_key(changes, session_factory):
    apt = book(changes).body["id"]
    for action in (lambda: changes.detail(TWO, apt),
                   lambda: changes.replacement_availability(TWO, apt, START.date()),
                   lambda: changes.cancel(TWO, apt, expected_version=1, key="unauthorized")):
        with pytest.raises(Forbidden):
            action()
    assert stored_state(session_factory)["outcomes"] == 1


def test_stale_or_cancelled_request_cannot_release_twice(changes, session_factory):
    apt = book(changes).body["id"]
    assert changes.cancel(ONE, apt, expected_version=1, key="first").status == 200
    before = stored_state(session_factory)
    assert changes.cancel(ONE, apt, expected_version=1, key="stale").status == 412
    assert error_code(changes.cancel(ONE, apt, expected_version=2, key="cancelled")) == "appointment_not_confirmed"
    assert stored_state(session_factory)["versions"] == before["versions"]
