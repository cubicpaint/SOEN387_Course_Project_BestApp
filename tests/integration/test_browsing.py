"""Verify public browsing queries against the migrated PostgreSQL schema."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.data.models import Appointment, AvailabilityBlock, Barber, SlotAllocation, User
from app.domain.types import ServiceChoice
from app.services.browsing import BrowsingService
from app.services.errors import BarberNotFound


pytestmark = pytest.mark.integration
DAY = date(2026, 11, 17)
START = datetime(2026, 11, 17, 15, tzinfo=UTC)


def seed_blocks(session_factory, *, start=START, count=5):
    with session_factory() as session, session.begin():
        session.add_all([Barber(id="bar_alex", display_name="Alex"), Barber(id="bar_sam", display_name="Sam")])
        session.flush()
        for index in range(count):
            begins = start + timedelta(minutes=30 * index)
            session.add(AvailabilityBlock(id=f"slot_{index}", barber_id="bar_alex", starts_at=begins, ends_at=begins + timedelta(minutes=30)))


def test_barbers_are_public_ordered_by_display_name_then_id(session_factory):
    service = BrowsingService(session_factory)
    assert service.list_barbers() == ()
    with session_factory() as session, session.begin():
        session.add_all([Barber(id="bar_z", display_name="Sam"), Barber(id="bar_b", display_name="Alex"), Barber(id="bar_a", display_name="Alex")])
    assert [(barber.id, barber.display_name) for barber in service.list_barbers()] == [("bar_a", "Alex"), ("bar_b", "Alex"), ("bar_z", "Sam")]


@pytest.mark.parametrize("service_choice,expected", [(ServiceChoice.HAIRCUT, ["slot_2", "slot_3", "slot_4"]), (ServiceChoice.BEARD, ["slot_2", "slot_3", "slot_4"]), (ServiceChoice.BOTH, ["slot_2", "slot_3"])])
def test_occupied_and_disabled_blocks_do_not_complete_a_service(session_factory, service_choice, expected):
    seed_blocks(session_factory)
    with session_factory() as session, session.begin():
        session.add(User(id="usr_customer", email="customer@example.com", role="customer", password_hash="unused by browsing"))
        session.get(AvailabilityBlock, "slot_1").enabled = False
        session.flush()
        session.add(Appointment(id="apt_existing", customer_id="usr_customer", barber_id="bar_alex", service="haircut", starts_at=START, ends_at=START + timedelta(minutes=30)))
        session.flush()
        session.add(SlotAllocation(slot_id="slot_0", appointment_id="apt_existing"))
    service = BrowsingService(session_factory, clock=lambda: START - timedelta(hours=2))
    result = service.list_availability("bar_alex", DAY, service_choice)
    assert [slot.slot_id for slot in result.slots] == expected
    assert all(slot.ends_at - slot.starts_at == timedelta(minutes=result.duration_minutes) for slot in result.slots)


def test_other_barbers_continuation_cannot_complete_combined_service(session_factory):
    seed_blocks(session_factory, count=2)
    with session_factory() as session, session.begin():
        session.get(AvailabilityBlock, "slot_1").barber_id = "bar_sam"
    service = BrowsingService(session_factory, clock=lambda: START - timedelta(hours=2))
    assert service.list_availability("bar_alex", DAY, ServiceChoice.BOTH).slots == ()


def test_occupied_continuation_excludes_only_the_incomplete_combined_start(session_factory):
    seed_blocks(session_factory, count=2)
    occupied_start = START + timedelta(minutes=30)
    with session_factory() as session, session.begin():
        session.add(User(id="usr_customer", email="customer@example.com", role="customer", password_hash="unused by browsing"))
        session.flush()
        session.add(Appointment(id="apt_existing", customer_id="usr_customer", barber_id="bar_alex", service="haircut", starts_at=occupied_start, ends_at=occupied_start + timedelta(minutes=30)))
        session.flush()
        session.add(SlotAllocation(slot_id="slot_1", appointment_id="apt_existing"))
    service = BrowsingService(session_factory, clock=lambda: START - timedelta(hours=2))
    assert [slot.slot_id for slot in service.list_availability("bar_alex", DAY, ServiceChoice.HAIRCUT).slots] == ["slot_0"]
    assert service.list_availability("bar_alex", DAY, ServiceChoice.BOTH).slots == ()


def test_query_fetches_combined_continuation_after_local_midnight(session_factory):
    start = datetime(2026, 11, 18, 4, 30, tzinfo=UTC)
    seed_blocks(session_factory, start=start, count=2)
    service = BrowsingService(session_factory, clock=lambda: START)
    result = service.list_availability("bar_alex", DAY, ServiceChoice.BOTH)
    assert [slot.slot_id for slot in result.slots] == ["slot_0"]
    assert result.slots[0].ends_at == start + timedelta(hours=1)


def test_known_empty_past_and_calendar_limit_dates_are_valid(session_factory):
    seed_blocks(session_factory)
    service = BrowsingService(session_factory, clock=lambda: START)
    for day in [date(2026, 11, 1), date.min, date.max]:
        assert service.list_availability("bar_alex", day, ServiceChoice.HAIRCUT).slots == ()
    with pytest.raises(BarberNotFound):
        service.list_availability("bar_missing", DAY, ServiceChoice.HAIRCUT)


@pytest.mark.parametrize("day,first", [(date(2026, 3, 8), datetime(2026, 3, 8, 6, 30, tzinfo=UTC)), (date(2026, 11, 1), datetime(2026, 11, 1, 5, 30, tzinfo=UTC))])
def test_postgres_queries_preserve_elapsed_time_across_dst(session_factory, day, first):
    seed_blocks(session_factory, start=first, count=2)
    service = BrowsingService(session_factory, clock=lambda: first - timedelta(hours=3))
    result = service.list_availability("bar_alex", day, ServiceChoice.BOTH)
    assert len(result.slots) == 1
    assert result.slots[0].starts_at == first
    assert result.slots[0].ends_at == first + timedelta(hours=1)
