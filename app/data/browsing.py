"""Read public barber details and free availability blocks."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.data.models import AvailabilityBlock, Barber, SlotAllocation
from app.domain.availability import BarberSummary, FreeBlock


class BrowsingRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_barbers(self) -> tuple[BarberSummary, ...]:
        rows = self.session.execute(select(Barber.id, Barber.display_name).order_by(Barber.display_name, Barber.id))
        return tuple(BarberSummary(row.id, row.display_name) for row in rows)

    def barber_exists(self, barber_id: str) -> bool:
        return self.session.scalar(select(exists().where(Barber.id == barber_id)))

    def free_blocks(
        self, barber_id: str, starts_at: datetime, date_end: datetime | None
    ) -> tuple[FreeBlock, ...]:
        allocated = exists().where(SlotAllocation.slot_id == AvailabilityBlock.id)
        query = select(
            AvailabilityBlock.id,
            AvailabilityBlock.barber_id,
            AvailabilityBlock.starts_at,
            AvailabilityBlock.ends_at,
        ).where(
            AvailabilityBlock.barber_id == barber_id,
            AvailabilityBlock.enabled.is_(True),
            ~allocated,
            AvailabilityBlock.starts_at >= starts_at,
            # PostgreSQL supports years Python cannot decode into datetime.
            AvailabilityBlock.ends_at <= datetime.max.replace(tzinfo=UTC),
        )
        # The final start can need the first block of the following local date.
        if date_end is not None:
            maximum = datetime.max.replace(tzinfo=UTC)
            fetch_end = min(date_end, maximum - timedelta(minutes=30)) + timedelta(minutes=30)
            query = query.where(AvailabilityBlock.starts_at < fetch_end)
        rows = self.session.execute(query.order_by(AvailabilityBlock.starts_at))
        return tuple(FreeBlock(row.id, row.barber_id, row.starts_at, row.ends_at) for row in rows)
