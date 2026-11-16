"""Check barber ordering against PostgreSQL."""

import pytest
from app.data.models import Barber
from app.services.browsing import BrowsingService

pytestmark = pytest.mark.integration


def test_barbers_are_public_ordered_by_display_name_then_id(session_factory):
    service = BrowsingService(session_factory)
    assert service.list_barbers() == ()
    with session_factory() as session, session.begin():
        session.add_all([Barber(id="bar_z", display_name="Sam"), Barber(id="bar_b", display_name="Alex"), Barber(id="bar_a", display_name="Alex")])
    assert [(barber.id, barber.display_name) for barber in service.list_barbers()] == [("bar_a", "Alex"), ("bar_b", "Alex"), ("bar_z", "Sam")]
