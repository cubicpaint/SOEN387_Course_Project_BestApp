"""Staff reads and guarded date edits inside the service-owned transaction."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.data.models import Appointment, AvailabilityBlock, Barber, SlotAllocation, StaffBarber, User
from app.domain.appointments import utc_timestamp
from app.domain.availability import TORONTO, service_duration_minutes
from app.domain.staff_availability import date_blocks
from app.domain.types import Role, ServiceChoice


@dataclass(frozen=True)
class StaffBlock:
    id: str
    starts_at: datetime
    ends_at: datetime
    enabled: bool
    version: int
    occupied: bool


def schedule_snapshot(barber_id: str, day: date, blocks: tuple[StaffBlock, ...]) -> dict:
    encoded = json.dumps({
        "barberId": barber_id, "date": day.isoformat(),
        "blocks": [[block.id, utc_timestamp(block.starts_at), block.enabled, block.version] for block in blocks],
    }, sort_keys=True, separators=(",", ":")).encode()
    ranges = []
    for block in blocks:
        if not block.enabled:
            continue
        if ranges and ranges[-1][1] == block.starts_at:
            ranges[-1] = (ranges[-1][0], block.ends_at)
        else:
            ranges.append((block.starts_at, block.ends_at))
    return {
        "barberId": barber_id, "date": day.isoformat(), "timeZone": "America/Toronto",
        "revision": sha256(encoded).hexdigest(),
        "ranges": [{"startTime": start.astimezone(TORONTO).isoformat(), "endTime": end.astimezone(TORONTO).isoformat()}
                   for start, end in ranges],
        "blocks": [{"slotId": block.id, "startTime": utc_timestamp(block.starts_at), "endTime": utc_timestamp(block.ends_at),
                    "enabled": block.enabled, "occupied": block.occupied} for block in blocks],
    }


class StaffRepository:
    def __init__(self, session: Session):
        self.session = session

    def lock_staff(self, staff_id: str) -> bool:
        return self.session.scalar(select(User.id).where(User.id == staff_id, User.role == Role.STAFF).with_for_update()) is not None

    def has_assignment(self, staff_id: str, barber_id: str) -> bool:
        return self.session.scalar(select(exists().where(StaffBarber.staff_id == staff_id, StaffBarber.barber_id == barber_id)))

    def barber_exists(self, barber_id: str) -> bool:
        return self.session.scalar(select(exists().where(Barber.id == barber_id)))

    def lock_barber(self, barber_id: str) -> bool:
        # NO KEY UPDATE serializes schedule writers without blocking the
        # foreign-key KEY SHARE check made after booking claims its blocks.
        return self.session.scalar(select(Barber.id).where(Barber.id == barber_id).with_for_update(key_share=True)) is not None

    def blocks(self, barber_id: str, day: date) -> tuple[StaffBlock, ...]:
        intervals = date_blocks(day)
        occupied = exists().where(SlotAllocation.slot_id == AvailabilityBlock.id)
        rows = self.session.execute(select(
            AvailabilityBlock.id, AvailabilityBlock.starts_at, AvailabilityBlock.ends_at,
            AvailabilityBlock.enabled, AvailabilityBlock.version, occupied,
        ).where(
            AvailabilityBlock.barber_id == barber_id,
            AvailabilityBlock.starts_at >= intervals[0].starts_at,
            AvailabilityBlock.starts_at < intervals[-1].ends_at,
        ).order_by(AvailabilityBlock.starts_at, AvailabilityBlock.id))
        return tuple(StaffBlock(*row) for row in rows)

    def change_block(self, block: StaffBlock, *, enabled: bool) -> bool:
        occupied = exists().where(SlotAllocation.slot_id == AvailabilityBlock.id)
        changed = self.session.execute(update(AvailabilityBlock).where(
            AvailabilityBlock.id == block.id,
            AvailabilityBlock.version == block.version,
            ~occupied,
        ).values(enabled=enabled, version=AvailabilityBlock.version + 1).execution_options(synchronize_session=False))
        return changed.rowcount == 1

    def add_block(self, barber_id: str, starts_at: datetime, ends_at: datetime, *, enabled: bool) -> None:
        self.session.add(AvailabilityBlock(
            id="slot_" + uuid4().hex, barber_id=barber_id, starts_at=starts_at, ends_at=ends_at,
            enabled=enabled, version=1,
        ))
        self.session.flush()

    def list_upcoming(self, barber_id: str, now: datetime) -> tuple[dict, ...]:
        rows = self.session.execute(select(Appointment, User.email).join(User, User.id == Appointment.customer_id).where(
            Appointment.barber_id == barber_id, Appointment.status == "confirmed", Appointment.starts_at >= now.astimezone(UTC),
        ).order_by(Appointment.starts_at, Appointment.id))
        return tuple({
            "id": appointment.id, "barberId": appointment.barber_id, "service": appointment.service,
            "durationMinutes": service_duration_minutes(ServiceChoice(appointment.service)),
            "startTime": utc_timestamp(appointment.starts_at), "endTime": utc_timestamp(appointment.ends_at),
            "status": appointment.status, "createdAt": utc_timestamp(appointment.created_at), "customerEmail": email,
        } for appointment, email in rows)
