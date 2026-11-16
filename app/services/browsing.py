"""Coordinate public barber browsing."""

from collections.abc import Callable
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.data.browsing import BrowsingRepository
from app.domain.availability import BarberSummary
from app.services.errors import ServiceUnavailable


class BrowsingService:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def list_barbers(self) -> tuple[BarberSummary, ...]:
        try:
            with self.session_factory() as session:
                return BrowsingRepository(session).list_barbers()
        except SQLAlchemyError:
            raise ServiceUnavailable from None
