"""Readiness coordination without HTTP response concerns."""

from sqlalchemy.engine import Engine

from app.data.health import check_database_ready


class ReadinessService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def is_ready(self) -> bool:
        return check_database_ready(self.engine)
