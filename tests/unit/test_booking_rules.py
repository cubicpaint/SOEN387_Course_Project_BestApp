"""Booking boundaries and complete durations without HTTP or a database."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.appointments import (
    BookingBlock,
    booking_notice_satisfied,
    complete_booking_blocks,
    intervals_overlap,
    utc_timestamp,
)
from app.domain.types import ServiceChoice


START = datetime(2030, 1, 15, 15, tzinfo=UTC)
FIRST = BookingBlock("slot_first", "bar_test", START, START + timedelta(minutes=30), True, 1)
SECOND = BookingBlock("slot_second", "bar_test", FIRST.ends_at, START + timedelta(hours=1), True, 1)


@pytest.mark.parametrize("service", [ServiceChoice.HAIRCUT, ServiceChoice.BEARD])
def test_short_service_requires_one_complete_block(service):
    assert complete_booking_blocks(FIRST, [SECOND, FIRST], service) == (FIRST,)


def test_combined_service_requires_both_consecutive_blocks_of_same_barber():
    assert complete_booking_blocks(FIRST, [SECOND, FIRST], ServiceChoice.BOTH) == (FIRST, SECOND)
    for invalid in ([], [replace(SECOND, enabled=False)], [replace(SECOND, barber_id="bar_other")]):
        assert complete_booking_blocks(FIRST, [FIRST, *invalid], ServiceChoice.BOTH) is None


@pytest.mark.parametrize(
    "block",
    [
        replace(FIRST, enabled=False),
        replace(FIRST, ends_at=START + timedelta(hours=1)),
        replace(FIRST, starts_at=START + timedelta(minutes=15), ends_at=START + timedelta(minutes=45)),
    ],
)
def test_disabled_or_malformed_block_cannot_be_booked(block):
    assert complete_booking_blocks(block, [block], ServiceChoice.HAIRCUT) is None


def test_missing_middle_block_does_not_join_separated_blocks():
    later = replace(SECOND, starts_at=START + timedelta(hours=1), ends_at=START + timedelta(minutes=90))
    assert complete_booking_blocks(FIRST, [FIRST, later], ServiceChoice.BOTH) is None


def test_exact_notice_boundary_and_one_microsecond_inside():
    now = START - timedelta(hours=2)
    assert booking_notice_satisfied(START, now)
    assert not booking_notice_satisfied(START, now + timedelta(microseconds=1))
    assert booking_notice_satisfied(START, now - timedelta(microseconds=1))


def test_notice_uses_elapsed_time_across_fall_back():
    toronto = ZoneInfo("America/Toronto")
    now = datetime(2026, 11, 1, 1, 30, tzinfo=toronto, fold=0)
    start = datetime(2026, 11, 1, 2, 30, tzinfo=toronto)
    assert booking_notice_satisfied(start, now)


def test_back_to_back_intervals_do_not_overlap_but_one_microsecond_does():
    end = START + timedelta(minutes=30)
    assert not intervals_overlap(START, end, end, end + timedelta(minutes=30))
    assert intervals_overlap(START, end, end - timedelta(microseconds=1), end + timedelta(minutes=30))


def test_utc_timestamp_preserves_fraction_and_rejects_naive_clock():
    assert utc_timestamp(START.replace(microsecond=123)) == "2030-01-15T15:00:00.000123Z"
    with pytest.raises(ValueError):
        utc_timestamp(START.replace(tzinfo=None))
    with pytest.raises(ValueError):
        booking_notice_satisfied(START, START.replace(tzinfo=None))
