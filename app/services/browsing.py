"""Coordinate read-only browsing while keeping scheduling rules in the domain."""

from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.data.browsing import BrowsingRepository
from app.domain.availability import (
    AvailabilityResult,
    BarberSummary,
    eligible_starts,
    local_date_bounds,
    service_duration_minutes,
)
from app.domain.types import ServiceChoice
from app.services.errors import BarberNotFound, ServiceUnavailable


class BrowsingService:
    def __init__(self, session_factory: Callable[[], Session], *, clock: Callable[[], datetime] | None = None):
        self.session_factory = session_factory
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)

    def list_barbers(self) -> tuple[BarberSummary, ...]:
        try:
            with self.session_factory() as session:
                return BrowsingRepository(session).list_barbers()
        except SQLAlchemyError:
            raise ServiceUnavailable from None

    def list_availability(self, barber_id: str, local_date: date, service: ServiceChoice) -> AvailabilityResult:
        starts_at, ends_at = local_date_bounds(local_date)
        try:
            with self.session_factory() as session:
                repository = BrowsingRepository(session)
                if not repository.barber_exists(barber_id):
                    raise BarberNotFound
                blocks = repository.free_blocks(barber_id, starts_at, ends_at)
        except SQLAlchemyError:
            raise ServiceUnavailable from None
        slots = eligible_starts(
            blocks,
            barber_id=barber_id,
            local_date=local_date,
            service=service,
            now=self.clock(),
        )
        return AvailabilityResult(barber_id, local_date, service, service_duration_minutes(service), slots)
