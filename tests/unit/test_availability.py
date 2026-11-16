"""Exercise duration, notice, local-date and daylight-saving boundaries."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.availability import FreeBlock, eligible_starts, local_date_bounds, service_duration_minutes
from app.domain.types import ServiceChoice


DAY = date(2026, 11, 17)
START = datetime(2026, 11, 17, 15, tzinfo=UTC)
NOW = START - timedelta(hours=2)


def block(number=0, *, start=None, barber="bar_alex", duration=30):
    start = start if start is not None else START + timedelta(minutes=number * 30)
    return FreeBlock(f"slot_{number}", barber, start, start + timedelta(minutes=duration))


def browse(blocks, *, day=DAY, service=ServiceChoice.HAIRCUT, now=NOW):
    return eligible_starts(blocks, barber_id="bar_alex", local_date=day, service=service, now=now)


@pytest.mark.parametrize("service,minutes", [(ServiceChoice.HAIRCUT, 30), (ServiceChoice.BEARD, 30), (ServiceChoice.BOTH, 60)])
def test_service_duration_and_complete_end(service, minutes):
    results = browse([block(0), block(1)], service=service)
    assert service_duration_minutes(service) == minutes
    assert results[0].slot_id == "slot_0"
    assert results[0].ends_at - results[0].starts_at == timedelta(minutes=minutes)
    assert len(results) == (1 if service == ServiceChoice.BOTH else 2)


@pytest.mark.parametrize("offset,eligible", [(-1, True), (0, True), (1, False)])
def test_exact_two_hour_boundary_uses_elapsed_time(offset, eligible):
    results = browse([block()], now=NOW + timedelta(microseconds=offset))
    assert bool(results) is eligible


def test_past_and_too_soon_starts_are_excluded():
    assert browse([block()], now=START + timedelta(hours=1)) == ()
    assert browse([block()], now=START - timedelta(hours=1)) == ()


@pytest.mark.parametrize("blocks", [[block()], [block(0), block(2)], [block(0), block(1, barber="bar_sam")]])
def test_combined_service_requires_two_adjacent_blocks_for_same_barber(blocks):
    assert browse(blocks, service=ServiceChoice.BOTH) == ()


@pytest.mark.parametrize("start", [START + timedelta(minutes=1), START + timedelta(seconds=1), START + timedelta(microseconds=1)])
def test_stored_blocks_off_the_local_grid_are_excluded(start):
    assert browse([block(start=start)]) == ()


def test_incomplete_base_block_is_excluded():
    assert browse([block(duration=15)]) == ()


def test_results_are_sorted_and_keep_original_slot_identity():
    results = browse([block(2), block(0), block(1)])
    assert [result.slot_id for result in results] == ["slot_0", "slot_1", "slot_2"]


def test_local_date_selects_starts_and_allows_continuation_after_midnight():
    start = datetime(2026, 11, 18, 4, 30, tzinfo=UTC)
    blocks = [block(0, start=start), block(1, start=start + timedelta(minutes=30))]
    results = browse(blocks, service=ServiceChoice.BOTH)
    assert len(results) == 1
    assert results[0].starts_at == start
    assert results[0].ends_at == datetime(2026, 11, 18, 5, 30, tzinfo=UTC)
    assert [result.slot_id for result in browse(blocks)] == ["slot_0"]


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_local_date_bounds_respect_daylight_saving(day, hours):
    start, end = local_date_bounds(day)
    assert end - start == timedelta(hours=hours)


def test_repeated_fallback_clock_times_remain_distinct_instants():
    day = date(2026, 11, 1)
    first = datetime(2026, 11, 1, 5, tzinfo=UTC)
    blocks = [block(i, start=first + timedelta(minutes=30 * i)) for i in range(4)]
    results = browse(blocks, day=day, now=first - timedelta(hours=3))
    assert len(results) == 4
    assert len({result.slot_id for result in results}) == 4
    assert [result.starts_at for result in results] == [first + timedelta(minutes=30 * i) for i in range(4)]
    combined = browse(blocks, day=day, service=ServiceChoice.BOTH, now=first - timedelta(hours=3))
    assert combined[1].ends_at - combined[1].starts_at == timedelta(hours=1)


def test_spring_forward_combined_service_is_sixty_elapsed_minutes():
    first = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    results = browse(
        [block(0, start=first), block(1, start=first + timedelta(minutes=30))],
        day=date(2026, 3, 8), service=ServiceChoice.BOTH, now=first - timedelta(hours=3),
    )
    assert len(results) == 1
    assert results[0].ends_at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


def test_supported_calendar_limits_do_not_overflow():
    earliest, end = local_date_bounds(date.min)
    assert earliest.date() == date.min
    assert end.date() == date(1, 1, 2)
    latest, end = local_date_bounds(date.max)
    assert latest.date() == date.max
    assert end is None
    assert browse([], day=date.max) == ()
    assert browse([], now=datetime.max.replace(tzinfo=UTC)) == ()


def test_clock_requires_an_unambiguous_instant():
    with pytest.raises(ValueError, match="timezone"):
        browse([], now=NOW.replace(tzinfo=None))
