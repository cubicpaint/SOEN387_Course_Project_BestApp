"""Dated staff ranges and complete Toronto days, without HTTP or SQL."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.domain.availability import BLOCK_DURATION, TORONTO


@dataclass(frozen=True)
class AvailabilityRange:
    starts_at: datetime
    ends_at: datetime


def date_blocks(day: date) -> tuple[AvailabilityRange, ...]:
    """Walk elapsed time between local midnights, including DST's short/long day."""
    try:
        start = datetime.combine(day, datetime.min.time(), TORONTO).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), TORONTO).astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError("The availability date is outside the supported calendar.") from None
    blocks = []
    while start < end:
        blocks.append(AvailabilityRange(start, start + BLOCK_DURATION))
        start += BLOCK_DURATION
    return tuple(blocks)


def normalize_ranges(day: date, ranges: Iterable[AvailabilityRange]) -> tuple[AvailabilityRange, ...]:
    """Require actual Toronto offsets and half-hour boundaries; merge adjacency."""
    day_blocks = date_blocks(day)
    first, last = day_blocks[0].starts_at, day_blocks[-1].ends_at
    ordered = []
    for interval in ranges:
        for boundary in (interval.starts_at, interval.ends_at):
            if boundary.tzinfo is None or boundary.utcoffset() is None:
                raise ValueError("Availability boundaries require explicit Toronto offsets.")
            local = boundary.astimezone(TORONTO)
            if boundary.utcoffset() != local.utcoffset():
                raise ValueError("Availability offsets must match America/Toronto.")
            if local.minute not in (0, 30) or local.second or local.microsecond:
                raise ValueError("Availability boundaries must follow the half-hour grid.")
        start, end = interval.starts_at.astimezone(UTC), interval.ends_at.astimezone(UTC)
        if not first <= start < end <= last:
            raise ValueError("Ranges must be positive and stay within the selected Toronto date.")
        ordered.append(AvailabilityRange(start, end))
    merged = []
    for interval in sorted(ordered, key=lambda value: value.starts_at):
        if merged and interval.starts_at < merged[-1].ends_at:
            raise ValueError("Availability ranges cannot overlap or be duplicated.")
        if merged and interval.starts_at == merged[-1].ends_at:
            merged[-1] = AvailabilityRange(merged[-1].starts_at, interval.ends_at)
        else:
            merged.append(interval)
    return tuple(merged)


def enabled_starts(ranges: Iterable[AvailabilityRange]) -> frozenset[datetime]:
    starts = set()
    for interval in ranges:
        start = interval.starts_at
        while start < interval.ends_at:
            starts.add(start)
            start += BLOCK_DURATION
    return frozenset(starts)
