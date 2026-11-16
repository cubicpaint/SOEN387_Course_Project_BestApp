"""Reusable service-layer permission checks, also callable without HTTP."""

from app.domain.identity import Caller
from app.domain.types import Role
from app.services.errors import Forbidden, Unauthenticated


def require_customer(caller: Caller | None) -> Caller:
    if caller is None:
        raise Unauthenticated()
    if caller.role != Role.CUSTOMER:
        raise Forbidden()
    return caller


def require_owner(caller: Caller | None, customer_id: str) -> Caller:
    customer = require_customer(caller)
    if customer.id != customer_id:
        raise Forbidden()
    return customer


def require_staff_barber(caller: Caller | None, barber_id: str) -> Caller:
    if caller is None:
        raise Unauthenticated()
    if caller.role != Role.STAFF or barber_id not in caller.managed_barber_ids:
        raise Forbidden()
    return caller
