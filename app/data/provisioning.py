"""Focused persistence operations for explicit demonstration provisioning."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import AvailabilityBlock, Barber, StaffBarber, User


class DemoRepository:
    """Read and write through the transaction owned by the provisioning service."""

    def __init__(self, session: Session):
        self.session = session

    def user_by_email(self, email: str) -> User | None:
        return self.session.scalar(select(User).where(User.email == email))

    def add_user(self, *, user_id: str, email: str, password_hash: str, role: str) -> User:
        user = User(id=user_id, email=email, password_hash=password_hash, role=role)
        self.session.add(user)
        self.session.flush()
        return user

    def barber_by_id(self, barber_id: str) -> Barber | None:
        return self.session.get(Barber, barber_id)

    def add_barber(self, *, barber_id: str, display_name: str) -> Barber:
        barber = Barber(id=barber_id, display_name=display_name)
        self.session.add(barber)
        self.session.flush()
        return barber

    def all_barber_ids(self) -> list[str]:
        return list(self.session.scalars(select(Barber.id)))

    def lock_barbers(self, barber_ids: list[str]) -> None:
        # Staff date replacements use this same row before scanning or
        # inserting blocks, including dates with no existing availability.
        self.session.execute(select(Barber.id).where(Barber.id.in_(barber_ids)).order_by(Barber.id).with_for_update()).all()

    def ensure_staff_assignment(self, staff_id: str, barber_id: str) -> bool:
        if self.session.get(StaffBarber, (staff_id, barber_id)) is not None:
            return False
        self.session.add(StaffBarber(staff_id=staff_id, barber_id=barber_id))
        self.session.flush()
        return True

    def block_at(self, barber_id: str, starts_at: datetime) -> AvailabilityBlock | None:
        return self.session.scalar(
            select(AvailabilityBlock).where(
                AvailabilityBlock.barber_id == barber_id,
                AvailabilityBlock.starts_at == starts_at,
            )
        )

    def add_block(
        self,
        *,
        slot_id: str,
        barber_id: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> AvailabilityBlock:
        block = AvailabilityBlock(
            id=slot_id,
            barber_id=barber_id,
            starts_at=starts_at,
            ends_at=ends_at,
            enabled=True,
            version=1,
        )
        self.session.add(block)
        self.session.flush()
        return block
