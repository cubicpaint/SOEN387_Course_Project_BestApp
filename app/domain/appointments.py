"""Booking intervals and outcomes, independent of HTTP and database code."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.availability import BLOCK_DURATION, MINIMUM_NOTICE, TORONTO, service_duration_minutes
from app.domain.types import ServiceChoice


@dataclass(frozen=True)
class BookingBlock:
    id: str
    barber_id: str
    starts_at: datetime
    ends_at: datetime
    enabled: bool
    version: int


@dataclass(frozen=True)
class BookingOutcome:
    status: int
    body: dict


def complete_booking_blocks(
    starting: BookingBlock, blocks: Iterable[BookingBlock], service: ServiceChoice
) -> tuple[BookingBlock, ...] | None:
    """Select the complete duration on the local half-hour grid, or reject it."""
    by_start = {
        block.starts_at.astimezone(UTC): block for block in blocks if block.barber_id == starting.barber_id
    }
    selected = []
    expected = starting.starts_at.astimezone(UTC)
    for _ in range(service_duration_minutes(service) // 30):
        block = by_start.get(expected)
        if block is None or not block.enabled:
            return None
        if block.ends_at.astimezone(UTC) - block.starts_at.astimezone(UTC) != BLOCK_DURATION:
            return None
        try:
            local = block.starts_at.astimezone(TORONTO)
        except (OverflowError, ValueError):
            return None
        if local.minute not in (0, 30) or local.second or local.microsecond:
            return None
        selected.append(block)
        expected = block.ends_at.astimezone(UTC)
    if selected[0].id != starting.id:
        return None
    return tuple(selected)


def booking_notice_satisfied(starts_at: datetime, now: datetime) -> bool:
    """Compare elapsed UTC time; exactly two hours is eligible."""
    for instant in (starts_at, now):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Booking times must include a timezone.")
    return starts_at.astimezone(UTC) - now.astimezone(UTC) >= MINIMUM_NOTICE


def change_notice_satisfied(starts_at: datetime, now: datetime) -> bool:
    """Cancellation and time changes require at least 24 elapsed hours."""
    for instant in (starts_at, now):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Appointment change times must include a timezone.")
    return starts_at.astimezone(UTC) - now.astimezone(UTC) >= timedelta(hours=24)


def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open intervals allow adjacent appointments."""
    return a_start.astimezone(UTC) < b_end.astimezone(UTC) and b_start.astimezone(UTC) < a_end.astimezone(UTC)


def utc_timestamp(instant: datetime) -> str:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Appointment timestamps must include a timezone.")
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
