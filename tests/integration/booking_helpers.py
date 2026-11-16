"""Small deterministic fixtures shared by the real PostgreSQL booking tests."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.data.models import Appointment, AvailabilityBlock, Barber, IdempotencyRecord, SlotAllocation, User
from app.domain.identity import Caller
from app.domain.types import Role, ServiceChoice


START = datetime(2030, 1, 15, 15, tzinfo=UTC)
NOW = START - timedelta(hours=3)
ONE = Caller("usr_one", Role.CUSTOMER)
TWO = Caller("usr_two", Role.CUSTOMER)
STAFF = Caller("usr_staff", Role.STAFF, ("bar_alex",))


def seed_booking_data(session_factory, *, count=6):
    with session_factory.begin() as session:
        session.add_all([
            User(id=caller.id, email=f"{caller.id}@example.test", role=caller.role, password_hash="unused by service tests")
            for caller in (ONE, TWO, STAFF)
        ])
        session.add_all([Barber(id="bar_alex", display_name="Alex"), Barber(id="bar_sam", display_name="Sam")])
        session.flush()
        for barber in ("alex", "sam"):
            for index in range(count):
                starts_at = START + timedelta(minutes=30 * index)
                session.add(AvailabilityBlock(
                    id=f"slot_{barber}_{index}", barber_id=f"bar_{barber}",
                    starts_at=starts_at, ends_at=starts_at + timedelta(minutes=30),
                ))


def book(service, caller=ONE, *, slot="slot_alex_0", choice=ServiceChoice.HAIRCUT, key="booking-one"):
    return service.book(caller, slot_id=slot, service=choice, key=key)


def stored_state(session_factory):
    """Read committed state through a fresh connection, outside the booking unit."""
    with session_factory() as session:
        return {
            "appointments": session.scalar(select(func.count()).select_from(Appointment)),
            "allocations": session.scalar(select(func.count()).select_from(SlotAllocation)),
            "outcomes": session.scalar(select(func.count()).select_from(IdempotencyRecord)),
            "versions": dict(session.execute(select(AvailabilityBlock.id, AvailabilityBlock.version)).all()),
        }


def error_code(outcome):
    return outcome.body["error"]["code"]
