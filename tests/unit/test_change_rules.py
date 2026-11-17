"""Changes compare elapsed time with a controlled aware decision instant."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.appointments import change_notice_satisfied


@pytest.mark.parametrize("delta,allowed", [(-1, True), (0, True), (1, False)])
def test_change_cutoff_is_inclusive(delta, allowed):
    start = datetime(2030, 1, 15, 15, tzinfo=UTC)
    now = start - timedelta(hours=24) + timedelta(microseconds=delta)
    assert change_notice_satisfied(start, now) is allowed


@pytest.mark.parametrize("month,day", [(3, 10), (11, 3)])
def test_change_cutoff_uses_elapsed_hours_across_dst(month, day):
    toronto = ZoneInfo("America/Toronto")
    start = datetime(2030, month, day, 12, tzinfo=toronto)
    now = start.astimezone(UTC) - timedelta(hours=24)
    assert change_notice_satisfied(start, now.astimezone(toronto))
    assert not change_notice_satisfied(start, now + timedelta(microseconds=1))


@pytest.mark.parametrize("naive_start", [True, False])
def test_change_clock_cannot_be_naive(naive_start):
    aware = datetime(2030, 1, 15, tzinfo=UTC)
    with pytest.raises(ValueError):
        change_notice_satisfied(aware.replace(tzinfo=None) if naive_start else aware,
                                aware if naive_start else aware.replace(tzinfo=None))
