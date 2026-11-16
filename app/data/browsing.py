"""Read public barber details and free availability blocks."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.data.models import AvailabilityBlock, Barber, SlotAllocation
from app.domain.availability import BarberSummary


class BrowsingRepository:
    def __init__(self, session: Session):
        self.session = session

    def list_barbers(self) -> tuple[BarberSummary, ...]:
        rows = self.session.execute(select(Barber.id, Barber.display_name).order_by(Barber.display_name, Barber.id))
        return tuple(BarberSummary(row.id, row.display_name) for row in rows)
