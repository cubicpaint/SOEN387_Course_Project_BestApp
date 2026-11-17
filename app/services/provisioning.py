"""Create opt-in demonstration data without replacing existing shop state."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from pwdlib import PasswordHash
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.data.provisioning import DemoRepository
from app.domain.types import Role


TORONTO = ZoneInfo("America/Toronto")


class DemoProvisioningError(Exception):
    """A safe diagnostic that may be displayed by the administrative CLI."""


@dataclass(frozen=True)
class DemoUser:
    id: str
    email: str
    role: str


@dataclass(frozen=True)
class DemoBarber:
    id: str
    display_name: str


@dataclass(frozen=True)
class BlockWindow:
    starts_at: datetime
    ends_at: datetime


DEMO_USERS = (
    DemoUser("usr_demo_customer_1", "customer.one@example.com", Role.CUSTOMER),
    DemoUser("usr_demo_customer_2", "customer.two@example.com", Role.CUSTOMER),
    DemoUser("usr_demo_shop_staff", "shop.staff@example.com", Role.STAFF),
    DemoUser("usr_demo_barber_staff", "barber.staff@example.com", Role.STAFF),
)
DEMO_BARBERS = (
    DemoBarber("bar_demo_alex", "Alex (demo)"),
    DemoBarber("bar_demo_sam", "Sam (demo)"),
)


@dataclass(frozen=True)
class ProvisioningSummary:
    users: tuple[DemoUser, ...]
    barbers: tuple[DemoBarber, ...]
    users_created: int
    barbers_created: int
    assignments_created: int
    blocks_created: int
    blocks_reused: int


def validate_demo_password(password: str) -> None:
    """Require an explicitly supplied password, never an example placeholder."""
    placeholders = ("placeholder", "change-me", "changeme", "replace-me", "replace_me", "your-demo-password")
    if not 12 <= len(password) <= 1024 or any(value in password.casefold() for value in placeholders):
        raise DemoProvisioningError(
            "The demo password must contain 12 to 1024 characters and must not be a placeholder."
        )


def demo_block_windows(now: datetime) -> tuple[BlockWindow, ...]:
    """Return seven future Toronto dates, each with 09:00–17:00 base blocks."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise DemoProvisioningError("The demonstration clock must include a timezone.")
    first_date = now.astimezone(TORONTO).date() + timedelta(days=1)
    windows = []
    for day_offset in range(7):
        day = first_date + timedelta(days=day_offset)
        opens_at = datetime.combine(day, time(9), tzinfo=TORONTO)
        for block_number in range(16):
            starts_at = (opens_at + timedelta(minutes=30 * block_number)).astimezone(UTC)
            windows.append(BlockWindow(starts_at, starts_at + timedelta(minutes=30)))
    return tuple(windows)


def _block_id(barber_id: str, starts_at: datetime) -> str:
    # A retained ID always denotes this barber and UTC interval, even on a later run.
    identity = f"{barber_id}:{starts_at.astimezone(UTC).isoformat()}".encode("utf-8")
    return "slot_demo_" + sha256(identity).hexdigest()[:32]


def provision_demo(
    session_factory: Callable[[], Session],
    password: str,
    *,
    now: datetime | None = None,
) -> ProvisioningSummary:
    """Provision one transaction; existing passwords, slots and bookings stay intact."""
    validate_demo_password(password)
    windows = demo_block_windows(now if now is not None else datetime.now(UTC))
    users = []
    barbers = []
    users_created = barbers_created = assignments_created = blocks_created = blocks_reused = 0
    password_hash = PasswordHash.recommended()

    try:
        with session_factory() as session, session.begin():
            repository = DemoRepository(session)
            for demo in DEMO_USERS:
                user = repository.user_by_email(demo.email)
                if user is None:
                    user = repository.add_user(
                        user_id=demo.id,
                        email=demo.email,
                        role=demo.role,
                        password_hash=password_hash.hash(password),
                    )
                    users_created += 1
                elif user.role != demo.role:
                    raise DemoProvisioningError(
                        "A demo email already belongs to a different account role; no data was changed."
                    )
                users.append(DemoUser(user.id, user.email, user.role))

            for demo in DEMO_BARBERS:
                barber = repository.barber_by_id(demo.id)
                if barber is None:
                    barber = repository.add_barber(barber_id=demo.id, display_name=demo.display_name)
                    barbers_created += 1
                barbers.append(DemoBarber(barber.id, barber.display_name))

            all_barber_ids = repository.all_barber_ids()
            staff_assignments = [(users[2].id, barber_id) for barber_id in all_barber_ids]
            staff_assignments.append((users[3].id, barbers[0].id))
            for staff_id, barber_id in staff_assignments:
                if repository.ensure_staff_assignment(staff_id, barber_id):
                    assignments_created += 1

            repository.lock_barbers([barber.id for barber in barbers])
            for barber in sorted(barbers, key=lambda value: value.id):
                for window in windows:
                    block = repository.block_at(barber.id, window.starts_at)
                    if block is not None:
                        if block.ends_at != window.ends_at:
                            raise DemoProvisioningError(
                                "An existing availability interval does not match a demo base block; no data was changed."
                            )
                        blocks_reused += 1
                        continue
                    repository.add_block(
                        slot_id=_block_id(barber.id, window.starts_at),
                        barber_id=barber.id,
                        starts_at=window.starts_at,
                        ends_at=window.ends_at,
                    )
                    blocks_created += 1
    except SQLAlchemyError:
        # Driver messages can include connection strings, SQL values or credentials.
        raise DemoProvisioningError(
            "Demo provisioning failed; its transaction was rolled back. Check the database, migrations and existing IDs."
        ) from None

    return ProvisioningSummary(
        tuple(users), tuple(barbers), users_created, barbers_created, assignments_created, blocks_created, blocks_reused
    )
