"""Service durations and availability decisions, independent of HTTP and SQL."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.types import ServiceChoice


TORONTO = ZoneInfo("America/Toronto")
BLOCK_DURATION = timedelta(minutes=30)
MINIMUM_NOTICE = timedelta(hours=2)


@dataclass(frozen=True)
class BarberSummary:
    id: str
    display_name: str


@dataclass(frozen=True)
class FreeBlock:
    id: str
    barber_id: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class AvailableStart:
    slot_id: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class AvailabilityResult:
    barber_id: str
    local_date: date
    service: ServiceChoice
    duration_minutes: int
    slots: tuple[AvailableStart, ...]


def service_duration_minutes(service: ServiceChoice) -> int:
    return 60 if service == ServiceChoice.BOTH else 30


def local_date_bounds(day: date) -> tuple[datetime, datetime | None]:
    """Convert two local midnights separately; a Toronto date need not last 24h.

    Python cannot represent the midnight following date.max. None means there
    is no representable upper bound, rather than imposing a booking horizon.
    """
    starts_at = datetime.combine(day, time.min, tzinfo=TORONTO).astimezone(UTC)
    ends_at = None
    if day != date.max:
        ends_at = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TORONTO).astimezone(UTC)
    return starts_at, ends_at


def eligible_starts(
    blocks: Iterable[FreeBlock],
    *,
    barber_id: str,
    local_date: date,
    service: ServiceChoice,
    now: datetime,
) -> tuple[AvailableStart, ...]:
    """Use only complete, free base blocks and compare elapsed time in UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("The availability clock must include a timezone.")
    now = now.astimezone(UTC)
    if now > datetime.max.replace(tzinfo=UTC) - MINIMUM_NOTICE:
        return ()
    earliest = now + MINIMUM_NOTICE
    free_by_start = {}
    for block in blocks:
        starts_at = block.starts_at.astimezone(UTC)
        ends_at = block.ends_at.astimezone(UTC)
        local_start = starts_at.astimezone(TORONTO)
        if (
            block.barber_id == barber_id
            and ends_at - starts_at == BLOCK_DURATION
            and local_start.minute in (0, 30)
            and local_start.second == 0
            and local_start.microsecond == 0
        ):
            free_by_start[starts_at] = FreeBlock(block.id, block.barber_id, starts_at, ends_at)

    results = []
    for starts_at, block in sorted(free_by_start.items()):
        if starts_at < earliest or starts_at.astimezone(TORONTO).date() != local_date:
            continue
        ends_at = block.ends_at
        if service == ServiceChoice.BOTH:
            following = free_by_start.get(ends_at)
            if following is None:
                continue
            ends_at = following.ends_at
        results.append(AvailableStart(block.id, starts_at, ends_at))
    return tuple(results)
