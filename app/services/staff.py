"""Authorized staff views and atomic, safely retryable availability changes."""

from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import UTC, date, datetime

from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError
from sqlalchemy.orm import Session

from app.data.idempotency import IdempotencyRepository, request_fingerprint
from app.data.staff import StaffRepository, schedule_snapshot
from app.domain.appointments import BookingOutcome, utc_timestamp
from app.domain.identity import Caller
from app.domain.staff_availability import AvailabilityRange, date_blocks, enabled_starts, normalize_ranges
from app.services.appointments import KEY_CONFLICT, rejected
from app.services.authorization import require_staff_barber
from app.services.errors import BarberNotFound, Forbidden, ServiceUnavailable, Unauthenticated


AVAILABILITY_OPERATION = "PUT /barbers/{barberId}/availability"
STALE_SCHEDULE = rejected(412, "stale_version", "The availability changed; reload the date before editing it.")
OCCUPIED = rejected(409, "availability_occupied", "An existing appointment occupies part of this change; no availability was changed.")
CONFLICT = rejected(409, "availability_conflict", "Availability changed concurrently; reload the date before editing it.")


class _ScheduleRejection(Exception):
    def __init__(self, outcome: BookingOutcome):
        self.outcome = outcome


class StaffService:
    def __init__(self, session_factory: Callable[[], Session], *, clock: Callable[[], datetime] | None = None):
        self.session_factory = session_factory
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)

    def availability(self, caller: Caller | None, barber_id: str, day: date) -> dict:
        require_staff_barber(caller, barber_id)
        date_blocks(day)
        try:
            with self.session_factory() as session:
                repository = StaffRepository(session)
                if not repository.barber_exists(barber_id):
                    raise BarberNotFound()
                return schedule_snapshot(barber_id, day, repository.blocks(barber_id, day))
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    def list_appointments(self, caller: Caller | None, barber_id: str) -> tuple[dict, ...]:
        require_staff_barber(caller, barber_id)
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("The staff appointment clock must include a timezone.")
        try:
            with self.session_factory() as session:
                repository = StaffRepository(session)
                if not repository.barber_exists(barber_id):
                    raise BarberNotFound()
                return repository.list_upcoming(barber_id, now)
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    def replace_availability(
        self, caller: Caller | None, barber_id: str, day: date, ranges: Iterable[AvailabilityRange], *, revision: str, key: str
    ) -> BookingOutcome:
        staff = require_staff_barber(caller, barber_id)
        normalized = normalize_ranges(day, ranges)
        fingerprint = request_fingerprint(
            AVAILABILITY_OPERATION, f"{barber_id}:{day.isoformat()}",
            {"ranges": [{"startTime": utc_timestamp(value.starts_at), "endTime": utc_timestamp(value.ends_at)} for value in normalized]},
            revision,
        )
        try:
            with self.session_factory() as session, session.begin():
                repository = StaffRepository(session)
                if not repository.lock_staff(staff.id):
                    raise Unauthenticated()
                if not repository.has_assignment(staff.id, barber_id):
                    raise Forbidden()
                idempotency = IdempotencyRepository(session)
                stored = idempotency.find(staff.id, key)
                if stored is not None:
                    if stored.operation != AVAILABILITY_OPERATION or stored.fingerprint != fingerprint:
                        return deepcopy(KEY_CONFLICT)
                    return BookingOutcome(stored.response_status, deepcopy(stored.response_body))
                try:
                    with session.begin_nested():
                        if not repository.lock_barber(barber_id):
                            raise BarberNotFound()
                        outcome = self._replace(repository, barber_id, day, normalized, revision)
                except _ScheduleRejection as error:
                    outcome = deepcopy(error.outcome)
                idempotency.store(caller_id=staff.id, key=key, operation=AVAILABILITY_OPERATION, fingerprint=fingerprint, outcome=outcome)
            return outcome
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    @staticmethod
    def _replace(repository: StaffRepository, barber_id: str, day: date, ranges: tuple[AvailabilityRange, ...], revision: str) -> BookingOutcome:
        blocks = repository.blocks(barber_id, day)
        if schedule_snapshot(barber_id, day, blocks)["revision"] != revision:
            raise _ScheduleRejection(STALE_SCHEDULE)
        desired = enabled_starts(ranges)
        if any(block.occupied and block.starts_at not in desired for block in blocks):
            raise _ScheduleRejection(OCCUPIED)
        existing = {block.starts_at: block for block in blocks}
        # All writers traverse one barber's blocks in UTC time order. Never
        # remove an allocation or change an existing block's identity.
        for interval in date_blocks(day):
            enabled = interval.starts_at in desired
            block = existing.get(interval.starts_at)
            if block is None:
                repository.add_block(barber_id, interval.starts_at, interval.ends_at, enabled=enabled)
            elif block.enabled != enabled and not repository.change_block(block, enabled=enabled):
                raise _ScheduleRejection(CONFLICT)
        return BookingOutcome(200, schedule_snapshot(barber_id, day, repository.blocks(barber_id, day)))
