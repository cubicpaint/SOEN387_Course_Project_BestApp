"""Browsing service coordination and safe data-source failures."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.domain.availability import FreeBlock
from app.domain.types import ServiceChoice
from app.services.browsing import BrowsingService
from app.services.errors import ServiceUnavailable


def test_availability_samples_the_clock_once_after_reading_free_blocks(monkeypatch):
    start = datetime(2026, 11, 17, 15, tzinfo=UTC)
    events = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Repository:
        def __init__(self, session):
            pass

        def barber_exists(self, barber_id):
            return True

        def free_blocks(self, *args):
            events.append("read")
            return (FreeBlock("slot_test", "bar_test", start, start + timedelta(minutes=30)),)

    def clock():
        events.append("clock")
        return start - timedelta(hours=2)

    monkeypatch.setattr("app.services.browsing.BrowsingRepository", Repository)
    result = BrowsingService(Session, clock=clock).list_availability("bar_test", date(2026, 11, 17), ServiceChoice.HAIRCUT)
    assert events == ["read", "clock"]
    assert len(result.slots) == 1


@pytest.mark.parametrize("operation", ["list_barbers", "list_availability"])
def test_data_source_failure_is_classified_without_exposing_driver_details(operation):
    def unavailable_session():
        raise OperationalError("private SQL", {}, Exception("private database credentials"))

    service = BrowsingService(unavailable_session)
    with pytest.raises(ServiceUnavailable) as failure:
        if operation == "list_barbers":
            service.list_barbers()
        else:
            service.list_availability("bar_test", date(2026, 11, 17), ServiceChoice.HAIRCUT)
    assert "private" not in str(failure.value)
    assert failure.value.__suppress_context__
