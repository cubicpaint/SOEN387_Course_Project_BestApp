"""Booking queries and conditional claims in the service's transaction."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.data.models import Appointment, AvailabilityBlock, SlotAllocation, User
from app.domain.appointments import BookingBlock, utc_timestamp
from app.domain.availability import service_duration_minutes
from app.domain.types import Role, ServiceChoice


def appointment_snapshot(appointment: Appointment, slot_id: str) -> dict:
    return {
        "id": appointment.id,
        "barberId": appointment.barber_id,
        "slotId": slot_id,
        "service": appointment.service,
        "durationMinutes": service_duration_minutes(ServiceChoice(appointment.service)),
        "startTime": utc_timestamp(appointment.starts_at),
        "endTime": utc_timestamp(appointment.ends_at),
        "status": appointment.status,
        "createdAt": utc_timestamp(appointment.created_at),
    }


class AppointmentRepository:
    def __init__(self, session: Session):
        self.session = session

    def lock_customer(self, customer_id: str) -> bool:
        # A common customer lock protects intervals across different barbers
        # and coordinates all of this caller's concurrent idempotency keys.
        return self.session.scalar(
            select(User.id).where(User.id == customer_id, User.role == Role.CUSTOMER).with_for_update()
        ) is not None

    @staticmethod
    def _block_columns():
        return (
            AvailabilityBlock.id,
            AvailabilityBlock.barber_id,
            AvailabilityBlock.starts_at,
            AvailabilityBlock.ends_at,
            AvailabilityBlock.enabled,
            AvailabilityBlock.version,
        )

    def starting_block(self, slot_id: str) -> BookingBlock | None:
        row = self.session.execute(
            select(*self._block_columns()).where(
                AvailabilityBlock.id == slot_id,
                AvailabilityBlock.starts_at >= datetime.min.replace(tzinfo=UTC),
                AvailabilityBlock.ends_at <= datetime.max.replace(tzinfo=UTC),
            )
        ).first()
        return BookingBlock(*row) if row is not None else None

    def interval_blocks(self, barber_id: str, starts_at: datetime, ends_at: datetime) -> tuple[BookingBlock, ...]:
        rows = self.session.execute(
            select(*self._block_columns())
            .where(
                AvailabilityBlock.barber_id == barber_id,
                AvailabilityBlock.starts_at >= starts_at,
                AvailabilityBlock.starts_at < ends_at,
                AvailabilityBlock.ends_at <= datetime.max.replace(tzinfo=UTC),
            )
            .order_by(AvailabilityBlock.starts_at, AvailabilityBlock.id)
        )
        return tuple(BookingBlock(*row) for row in rows)

    def claim_block(self, block: BookingBlock) -> bool:
        allocated = exists().where(SlotAllocation.slot_id == AvailabilityBlock.id)
        result = self.session.execute(
            update(AvailabilityBlock)
            .where(
                AvailabilityBlock.id == block.id,
                AvailabilityBlock.version == block.version,
                AvailabilityBlock.enabled.is_(True),
                ~allocated,
            )
            .values(version=AvailabilityBlock.version + 1)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def customer_overlaps(self, customer_id: str, starts_at: datetime, ends_at: datetime) -> bool:
        return self.session.scalar(
            select(exists().where(
                Appointment.customer_id == customer_id,
                Appointment.status == "confirmed",
                Appointment.starts_at < ends_at,
                Appointment.ends_at > starts_at,
            ))
        )

    def add_appointment(
        self, *, customer_id: str, blocks: tuple[BookingBlock, ...], service: ServiceChoice
    ) -> dict:
        appointment = Appointment(
            id="apt_" + uuid4().hex,
            customer_id=customer_id,
            barber_id=blocks[0].barber_id,
            service=service.value,
            starts_at=blocks[0].starts_at,
            ends_at=blocks[-1].ends_at,
        )
        self.session.add(appointment)
        self.session.flush()
        self.session.add_all(SlotAllocation(slot_id=block.id, appointment_id=appointment.id) for block in blocks)
        self.session.flush()
        return appointment_snapshot(appointment, blocks[0].id)

    def list_upcoming(self, customer_id: str, now: datetime) -> tuple[dict, ...]:
        rows = self.session.execute(
            select(Appointment, AvailabilityBlock.id)
            .join(SlotAllocation, SlotAllocation.appointment_id == Appointment.id)
            .join(AvailabilityBlock, AvailabilityBlock.id == SlotAllocation.slot_id)
            .where(
                Appointment.customer_id == customer_id,
                Appointment.status == "confirmed",
                Appointment.starts_at >= now,
                AvailabilityBlock.barber_id == Appointment.barber_id,
                AvailabilityBlock.starts_at == Appointment.starts_at,
            )
            .order_by(Appointment.starts_at, Appointment.id)
        )
        return tuple(appointment_snapshot(appointment, slot_id) for appointment, slot_id in rows)
