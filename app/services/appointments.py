"""Atomic customer bookings, durable retries, and own-appointment queries."""

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, TimeoutError
from sqlalchemy.orm import Session

from app.data.appointments import AppointmentRepository
from app.data.idempotency import BOOK_OPERATION, IdempotencyRepository, booking_fingerprint, request_fingerprint
from app.domain.appointments import (
    BookingOutcome, booking_notice_satisfied, change_notice_satisfied, complete_booking_blocks,
    intervals_overlap, utc_timestamp,
)
from app.domain.availability import eligible_starts, local_date_bounds, service_duration_minutes
from app.domain.identity import Caller
from app.domain.types import ServiceChoice
from app.services.authorization import require_customer, require_owner
from app.services.errors import ServiceUnavailable, Unauthenticated


def rejected(status: int, code: str, message: str) -> BookingOutcome:
    return BookingOutcome(status, {"error": {"code": code, "message": message, "retryable": False}})


SLOT_NOT_FOUND = rejected(404, "slot_not_found", "The starting slot was not found.")
SLOT_UNAVAILABLE = rejected(409, "slot_unavailable", "The complete appointment interval is unavailable.")
CUSTOMER_OVERLAP = rejected(409, "customer_overlap", "The appointment overlaps one of your existing appointments.")
NOTICE_VIOLATION = rejected(422, "booking_notice_violation", "Appointments require at least two hours' notice.")
KEY_CONFLICT = rejected(409, "idempotency_key_conflict", "The idempotency key is already bound to another request.")
APPOINTMENT_NOT_FOUND = rejected(404, "appointment_not_found", "The appointment was not found.")
NOT_CONFIRMED = rejected(409, "appointment_not_confirmed", "The appointment is no longer confirmed.")
STALE_VERSION = rejected(412, "stale_version", "The appointment changed. Refresh it before making a new request.")
CHANGE_CUTOFF = rejected(422, "change_cutoff_violation", "Online changes require at least 24 hours before the appointment.")
BARBER_CHANGE = rejected(409, "barber_change_forbidden", "Rescheduling must keep the original barber and service.")


class _BusinessRejection(Exception):
    def __init__(self, outcome: BookingOutcome):
        self.outcome = outcome


class AppointmentService:
    def __init__(self, session_factory: Callable[[], Session], *, clock: Callable[[], datetime] | None = None):
        self.session_factory = session_factory
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)

    @staticmethod
    def _owned(repository: AppointmentRepository, caller: Caller, appointment_id: str, *, lock: bool = False):
        appointment = repository.appointment(appointment_id)
        if appointment is None:
            raise _BusinessRejection(APPOINTMENT_NOT_FOUND)
        require_owner(caller, appointment.customer_id)
        return repository.lock_appointment(appointment_id, caller.id) if lock else appointment

    def detail(self, caller: Caller | None, appointment_id: str) -> BookingOutcome:
        customer = require_customer(caller)
        try:
            with self.session_factory() as session:
                repository = AppointmentRepository(session)
                appointment = self._owned(repository, customer, appointment_id)
                return BookingOutcome(200, repository.management_snapshot(appointment))
        except _BusinessRejection as error:
            return deepcopy(error.outcome)
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    def replacement_availability(self, caller: Caller | None, appointment_id: str, day: date) -> BookingOutcome:
        customer = require_customer(caller)
        try:
            with self.session_factory() as session:
                repository = AppointmentRepository(session)
                appointment = self._owned(repository, customer, appointment_id)
                if appointment.status != "confirmed":
                    raise _BusinessRejection(NOT_CONFIRMED)
                now = self.clock()
                if not change_notice_satisfied(appointment.starts_at, now):
                    raise _BusinessRejection(CHANGE_CUTOFF)
                starts_at, date_end = local_date_bounds(day)
                service = ServiceChoice(appointment.service)
                blocks = repository.replacement_blocks(appointment_id, appointment.barber_id, starts_at, date_end)
                slots = eligible_starts(blocks, barber_id=appointment.barber_id, local_date=day, service=service, now=now)
                conflicts = repository.customer_intervals(customer.id, appointment_id)
                return BookingOutcome(200, {
                    "barberId": appointment.barber_id, "date": day.isoformat(), "timeZone": "America/Toronto",
                    "service": service.value, "durationMinutes": service_duration_minutes(service),
                    "slots": [{"slotId": slot.slot_id, "startTime": utc_timestamp(slot.starts_at),
                               "endTime": utc_timestamp(slot.ends_at)} for slot in slots
                              if not any(intervals_overlap(slot.starts_at, slot.ends_at, a, b) for a, b in conflicts)],
                })
        except _BusinessRejection as error:
            return deepcopy(error.outcome)
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    def cancel(self, caller: Caller | None, appointment_id: str, *, expected_version: int, key: str) -> BookingOutcome:
        return self._change(caller, appointment_id, expected_version=expected_version, key=key)

    def reschedule(
        self, caller: Caller | None, appointment_id: str, *, slot_id: str, expected_version: int, key: str
    ) -> BookingOutcome:
        return self._change(caller, appointment_id, expected_version=expected_version, key=key, slot_id=slot_id)

    def _change(
        self, caller: Caller | None, appointment_id: str, *, expected_version: int, key: str, slot_id: str | None = None
    ) -> BookingOutcome:
        customer = require_customer(caller)
        operation = ("DELETE" if slot_id is None else "PUT") + " /appointments/{appointmentId}"
        content = {} if slot_id is None else {"slotId": slot_id}
        fingerprint = request_fingerprint(operation, appointment_id, content, f'"{expected_version}"')
        try:
            with self.session_factory() as session, session.begin():
                repository = AppointmentRepository(session)
                if not repository.lock_customer(customer.id):
                    raise Unauthenticated()
                idempotency = IdempotencyRepository(session)
                stored = idempotency.find(customer.id, key)
                if stored is not None:
                    if stored.operation != operation or stored.fingerprint != fingerprint:
                        return deepcopy(KEY_CONFLICT)
                    return BookingOutcome(stored.response_status, deepcopy(stored.response_body))
                try:
                    with session.begin_nested():
                        outcome = self._decide_change(repository, customer, appointment_id, expected_version, slot_id)
                except _BusinessRejection as error:
                    outcome = deepcopy(error.outcome)
                except IntegrityError as error:
                    diagnostic = getattr(error.orig, "diag", None)
                    if getattr(diagnostic, "constraint_name", None) != "pk_slot_allocations":
                        raise
                    outcome = deepcopy(SLOT_UNAVAILABLE)
                idempotency.store(caller_id=customer.id, key=key, operation=operation,
                                  fingerprint=fingerprint, outcome=outcome)
            return outcome
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    def _decide_change(
        self, repository: AppointmentRepository, customer: Caller, appointment_id: str,
        expected_version: int, slot_id: str | None,
    ):
        appointment = self._owned(repository, customer, appointment_id, lock=True)
        if appointment.version != expected_version:
            raise _BusinessRejection(STALE_VERSION)
        if appointment.status != "confirmed":
            raise _BusinessRejection(NOT_CONFIRMED)
        original = repository.allocated_blocks(appointment_id)
        if len(original) != service_duration_minutes(ServiceChoice(appointment.service)) // 30:
            raise RuntimeError("The confirmed appointment does not have its complete allocation.")
        destination = None
        if slot_id is not None:
            starting = repository.starting_block(slot_id)
            if starting is None:
                raise _BusinessRejection(SLOT_NOT_FOUND)
            if starting.barber_id != appointment.barber_id:
                raise _BusinessRejection(BARBER_CHANGE)
            try:
                ends_at = starting.starts_at.astimezone(UTC) + timedelta(minutes=service_duration_minutes(ServiceChoice(appointment.service)))
            except OverflowError:
                raise _BusinessRejection(SLOT_UNAVAILABLE) from None
            candidates = repository.interval_blocks(appointment.barber_id, starting.starts_at, ends_at)
            destination = complete_booking_blocks(starting, candidates, ServiceChoice(appointment.service))
            if destination is None:
                raise _BusinessRejection(SLOT_UNAVAILABLE)
        old_ids = {block.id for block in original}
        new_ids = {block.id for block in destination} if destination else set()
        union = {block.id: block for block in original}
        union.update({block.id: block for block in destination or ()})
        for block in sorted(union.values(), key=lambda value: (value.barber_id, value.starts_at, value.id)):
            if not repository.protect_change_block(block, appointment_id, owned=block.id in old_ids, required=block.id in new_ids):
                raise _BusinessRejection(SLOT_UNAVAILABLE)
        decision_time = self.clock()
        if not change_notice_satisfied(appointment.starts_at, decision_time):
            raise _BusinessRejection(CHANGE_CUTOFF)
        if destination:
            if not booking_notice_satisfied(destination[0].starts_at, decision_time):
                raise _BusinessRejection(NOTICE_VIOLATION)
            if repository.customer_overlaps(customer.id, destination[0].starts_at, destination[-1].ends_at, exclude_id=appointment_id):
                raise _BusinessRejection(CUSTOMER_OVERLAP)
        repository.change_allocations(appointment_id, old_ids, new_ids)
        if not repository.update_appointment(appointment, expected_version, destination):
            raise _BusinessRejection(STALE_VERSION)
        return BookingOutcome(200, repository.management_snapshot(appointment))

    def book(self, caller: Caller | None, *, slot_id: str, service: ServiceChoice, key: str) -> BookingOutcome:
        customer = require_customer(caller)
        fingerprint = booking_fingerprint(slot_id, service)
        try:
            with self.session_factory() as session, session.begin():
                repository = AppointmentRepository(session)
                if not repository.lock_customer(customer.id):
                    raise Unauthenticated()
                idempotency = IdempotencyRepository(session)
                stored = idempotency.find(customer.id, key)
                if stored is not None:
                    if stored.operation != BOOK_OPERATION or stored.fingerprint != fingerprint:
                        return deepcopy(KEY_CONFLICT)
                    return BookingOutcome(stored.response_status, deepcopy(stored.response_body))

                try:
                    # Roll back all booking writes on a business rejection,
                    # while the outer transaction retains its customer lock
                    # and can commit that rejection as a completed outcome.
                    with session.begin_nested():
                        outcome = self._decide(repository, customer.id, slot_id, service)
                except _BusinessRejection as error:
                    outcome = deepcopy(error.outcome)
                except IntegrityError as error:
                    # Only the known active-allocation guard is a slot conflict.
                    # Other integrity errors are programming/data failures.
                    diagnostic = getattr(error.orig, "diag", None)
                    if getattr(diagnostic, "constraint_name", None) != "pk_slot_allocations":
                        raise
                    outcome = deepcopy(SLOT_UNAVAILABLE)

                idempotency.store(
                    caller_id=customer.id,
                    key=key,
                    operation=BOOK_OPERATION,
                    fingerprint=fingerprint,
                    outcome=outcome,
                )
            # Returning outside the transaction ensures a commit failure never
            # becomes a success response or a separately persisted outcome.
            return outcome
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise

    def _decide(
        self, repository: AppointmentRepository, customer_id: str, slot_id: str, service: ServiceChoice
    ) -> BookingOutcome:
        starting = repository.starting_block(slot_id)
        if starting is None:
            raise _BusinessRejection(SLOT_NOT_FOUND)
        try:
            ends_at = starting.starts_at.astimezone(UTC) + timedelta(minutes=service_duration_minutes(service))
        except OverflowError:
            raise _BusinessRejection(SLOT_UNAVAILABLE) from None
        candidates = repository.interval_blocks(starting.barber_id, starting.starts_at, ends_at)
        blocks = complete_booking_blocks(starting, candidates, service)
        if blocks is None:
            raise _BusinessRejection(SLOT_UNAVAILABLE)
        for block in sorted(blocks, key=lambda value: (value.starts_at, value.id)):
            if not repository.claim_block(block):
                raise _BusinessRejection(SLOT_UNAVAILABLE)

        # Conditional updates can wait on competing slot writers. Take the
        # decision time only after those waits and the customer lock wait.
        decision_time = self.clock()
        if not booking_notice_satisfied(blocks[0].starts_at, decision_time):
            raise _BusinessRejection(NOTICE_VIOLATION)
        if repository.customer_overlaps(customer_id, blocks[0].starts_at, blocks[-1].ends_at):
            raise _BusinessRejection(CUSTOMER_OVERLAP)
        body = repository.add_appointment(customer_id=customer_id, blocks=blocks, service=service)
        return BookingOutcome(201, body)

    def list_own(self, caller: Caller | None) -> tuple[dict, ...]:
        customer = require_customer(caller)
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("The appointment clock must include a timezone.")
        try:
            with self.session_factory() as session:
                return AppointmentRepository(session).list_upcoming(customer.id, now.astimezone(UTC))
        except (OperationalError, TimeoutError):
            raise ServiceUnavailable() from None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise ServiceUnavailable() from None
            raise
