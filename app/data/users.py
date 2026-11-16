"""Queries for predefined accounts and their current staff assignments."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import StaffBarber, User
from app.domain.identity import Caller
from app.domain.types import Role


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def by_email(self, email: str) -> User | None:
        # Email matching is deliberately exact; do not normalize login input.
        return self.session.scalar(select(User).where(User.email == email))

    def by_id(self, user_id: str) -> User | None:
        return self.session.get(User, user_id)

    def caller(self, user: User) -> Caller:
        role = Role(user.role)
        barbers = ()
        if role == Role.STAFF:
            barbers = tuple(self.session.scalars(
                select(StaffBarber.barber_id).where(StaffBarber.staff_id == user.id).order_by(StaffBarber.barber_id)
            ))
        return Caller(user.id, role, barbers)
