"""Identity established by authentication, independent of HTTP and SQLAlchemy."""

from dataclasses import dataclass

from app.domain.types import Role


@dataclass(frozen=True)
class Caller:
    id: str
    role: Role
    managed_barber_ids: tuple[str, ...] = ()
