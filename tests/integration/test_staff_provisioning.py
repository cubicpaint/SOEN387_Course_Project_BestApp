"""Explicit staff closures survive the supported rerunnable demo setup."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.data.models import AvailabilityBlock
from app.domain.identity import Caller
from app.domain.staff_availability import AvailabilityRange
from app.domain.types import Role
from app.services.provisioning import DEMO_BARBERS, DEMO_USERS, provision_demo
from app.services.staff import StaffService


pytestmark = pytest.mark.integration


@pytest.mark.parametrize("open_range", [False, True])
def test_provisioning_does_not_reopen_staff_managed_empty_or_partial_date(session_factory, open_range):
    now = datetime(2030, 1, 10, 17, tzinfo=UTC)
    password = "private staff provisioning integration password"
    provision_demo(session_factory, password, now=now)
    barber_id = DEMO_BARBERS[0].id
    staff = Caller(DEMO_USERS[2].id, Role.STAFF, (barber_id,))
    service = StaffService(session_factory, clock=lambda: now)
    day = date(2030, 1, 18)  # Outside the initial seven provisioned dates.
    current = service.availability(staff, barber_id, day)
    assert current["blocks"] == []
    intervals = (AvailabilityRange(datetime.fromisoformat("2030-01-18T09:00:00-05:00"), datetime.fromisoformat("2030-01-18T11:00:00-05:00")),) if open_range else ()
    changed = service.replace_availability(staff, barber_id, day, intervals, revision=current["revision"], key="custom-date")
    assert changed.status == 200
    assert len(changed.body["blocks"]) == 48
    provision_demo(session_factory, password, now=now + timedelta(days=1))
    after = service.availability(staff, barber_id, day)
    assert after == changed.body
    assert sum(block["enabled"] for block in after["blocks"]) == (4 if open_range else 0)
    with session_factory() as session:
        assert session.scalar(select(AvailabilityBlock.id).where(AvailabilityBlock.id == after["blocks"][0]["slotId"])) is not None
