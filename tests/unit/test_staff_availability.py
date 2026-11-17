"""Staff write rules are tested with fixed dates, including both DST changes."""

from datetime import date, datetime

import pytest

from app.domain.staff_availability import AvailabilityRange, date_blocks, enabled_starts, normalize_ranges


def interval(start, end):
    return AvailabilityRange(datetime.fromisoformat(start), datetime.fromisoformat(end))


@pytest.mark.parametrize("day,count", [(date(2026, 3, 8), 46), (date(2026, 11, 1), 50), (date(2030, 1, 15), 48)])
def test_complete_day_uses_elapsed_half_hours(day, count):
    blocks = date_blocks(day)
    assert len(blocks) == count
    assert all(left.ends_at == right.starts_at for left, right in zip(blocks, blocks[1:]))


def test_ranges_sort_merge_adjacent_and_select_complete_blocks():
    ranges = normalize_ranges(date(2030, 1, 15), (
        interval("2030-01-15T10:00:00-05:00", "2030-01-15T11:00:00-05:00"),
        interval("2030-01-15T09:00:00-05:00", "2030-01-15T10:00:00-05:00"),
    ))
    assert len(ranges) == 1
    assert len(enabled_starts(ranges)) == 4


@pytest.mark.parametrize("start,end", [
    ("2030-01-15T09:00:00", "2030-01-15T10:00:00"),
    ("2030-01-15T14:00:00+00:00", "2030-01-15T15:00:00+00:00"),
    ("2030-01-15T09:15:00-05:00", "2030-01-15T10:00:00-05:00"),
    ("2030-01-15T09:00:01-05:00", "2030-01-15T10:00:00-05:00"),
    ("2030-01-15T09:00:00-05:00", "2030-01-15T09:00:00-05:00"),
    ("2030-01-15T10:00:00-05:00", "2030-01-15T09:00:00-05:00"),
    ("2030-01-14T23:30:00-05:00", "2030-01-15T09:00:00-05:00"),
    ("2030-01-15T23:30:00-05:00", "2030-01-16T00:30:00-05:00"),
])
def test_invalid_ranges_are_rejected(start, end):
    with pytest.raises(ValueError):
        normalize_ranges(date(2030, 1, 15), (interval(start, end),))


def test_nonexistent_spring_boundary_is_rejected():
    with pytest.raises(ValueError):
        normalize_ranges(date(2026, 3, 8), (interval("2026-03-08T02:00:00-05:00", "2026-03-08T03:30:00-04:00"),))


def test_fallback_offsets_distinguish_repeated_hours():
    ranges = normalize_ranges(date(2026, 11, 1), (interval("2026-11-01T01:00:00-04:00", "2026-11-01T02:00:00-05:00"),))
    assert len(enabled_starts(ranges)) == 4


@pytest.mark.parametrize("second", [
    interval("2030-01-15T09:30:00-05:00", "2030-01-15T10:30:00-05:00"),
    interval("2030-01-15T09:00:00-05:00", "2030-01-15T10:00:00-05:00"),
])
def test_overlapping_and_duplicate_ranges_are_rejected(second):
    with pytest.raises(ValueError):
        normalize_ranges(date(2030, 1, 15), (interval("2030-01-15T09:00:00-05:00", "2030-01-15T10:00:00-05:00"), second))


def test_empty_ranges_close_the_date_and_last_midnight_is_permitted():
    assert normalize_ranges(date(2030, 1, 15), ()) == ()
    ranges = normalize_ranges(date(2030, 1, 15), (interval("2030-01-15T23:30:00-05:00", "2030-01-16T00:00:00-05:00"),))
    assert len(enabled_starts(ranges)) == 1


def test_unrepresentable_calendar_endpoint_is_rejected():
    with pytest.raises(ValueError):
        date_blocks(date.max)
