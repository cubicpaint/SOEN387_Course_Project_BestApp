"""Booking queries and conditional claims in the service's transaction."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session

from app.data.models import Appointment, AvailabilityBlock, SlotAllocation, User
from app.domain.appointments import BookingBlock, utc_timestamp
from app.domain.availability import FreeBlock, service_duration_minutes
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

    def customer_overlaps(
        self, customer_id: str, starts_at: datetime, ends_at: datetime, *, exclude_id: str | None = None
    ) -> bool:
        return self.session.scalar(
            select(exists().where(
                Appointment.customer_id == customer_id,
                Appointment.status == "confirmed",
                Appointment.starts_at < ends_at,
                Appointment.ends_at > starts_at,
                Appointment.id != exclude_id if exclude_id is not None else True,
            ))
        )

    def appointment(self, appointment_id: str) -> Appointment | None:
        return self.session.get(Appointment, appointment_id)

    def lock_appointment(self, appointment_id: str, customer_id: str) -> Appointment:
        # Called after ownership is checked and the customer row is locked.
        return self.session.scalar(
            select(Appointment).where(Appointment.id == appointment_id, Appointment.customer_id == customer_id)
            .with_for_update().execution_options(populate_existing=True)
        )

    def management_snapshot(self, appointment: Appointment) -> dict:
        # A cancelled appointment has no active allocations, but its permanent
        # starting block still identifies its original barber/time interval.
        slot_id = self.session.scalar(select(AvailabilityBlock.id).where(
            AvailabilityBlock.barber_id == appointment.barber_id,
            AvailabilityBlock.starts_at == appointment.starts_at,
        ))
        if slot_id is None:
            raise RuntimeError("The appointment's permanent starting block is missing.")
        return {**appointment_snapshot(appointment, slot_id), "version": appointment.version}

    def allocated_blocks(self, appointment_id: str) -> tuple[BookingBlock, ...]:
        rows = self.session.execute(select(*self._block_columns())
            .join(SlotAllocation, SlotAllocation.slot_id == AvailabilityBlock.id)
            .where(SlotAllocation.appointment_id == appointment_id)
            .order_by(AvailabilityBlock.barber_id, AvailabilityBlock.starts_at, AvailabilityBlock.id))
        return tuple(BookingBlock(*row) for row in rows)

    def replacement_blocks(
        self, appointment_id: str, barber_id: str, starts_at: datetime, date_end: datetime | None
    ) -> tuple[FreeBlock, ...]:
        from datetime import timedelta

        other_allocation = exists().where(
            SlotAllocation.slot_id == AvailabilityBlock.id, SlotAllocation.appointment_id != appointment_id,
        )
        query = select(AvailabilityBlock.id, AvailabilityBlock.barber_id,
                       AvailabilityBlock.starts_at, AvailabilityBlock.ends_at).where(
            AvailabilityBlock.barber_id == barber_id, AvailabilityBlock.enabled.is_(True), ~other_allocation,
            AvailabilityBlock.starts_at >= starts_at,
            AvailabilityBlock.ends_at <= datetime.max.replace(tzinfo=UTC),
        )
        if date_end is not None:
            maximum = datetime.max.replace(tzinfo=UTC)
            query = query.where(AvailabilityBlock.starts_at < min(date_end, maximum - timedelta(minutes=30)) + timedelta(minutes=30))
        return tuple(FreeBlock(*row) for row in self.session.execute(query.order_by(AvailabilityBlock.starts_at)))

    def customer_intervals(self, customer_id: str, exclude_id: str) -> tuple[tuple[datetime, datetime], ...]:
        return tuple(self.session.execute(select(Appointment.starts_at, Appointment.ends_at).where(
            Appointment.customer_id == customer_id, Appointment.status == "confirmed", Appointment.id != exclude_id,
        )))

    def protect_change_block(
        self, block: BookingBlock, appointment_id: str, *, owned: bool, required: bool
    ) -> bool:
        """Protect old and new blocks before altering any active allocation."""
        allocated = exists().where(SlotAllocation.slot_id == AvailabilityBlock.id)
        self_owned = exists().where(
            SlotAllocation.slot_id == AvailabilityBlock.id, SlotAllocation.appointment_id == appointment_id,
        )
        result = self.session.execute(update(AvailabilityBlock).where(
            AvailabilityBlock.id == block.id, AvailabilityBlock.version == block.version,
            self_owned if owned else ~allocated,
            AvailabilityBlock.enabled.is_(True) if required else True,
        ).values(version=AvailabilityBlock.version + 1).execution_options(synchronize_session=False))
        return result.rowcount == 1

    def change_allocations(self, appointment_id: str, old_ids: set[str], new_ids: set[str]) -> None:
        self.session.add_all(SlotAllocation(slot_id=slot_id, appointment_id=appointment_id)
                             for slot_id in sorted(new_ids - old_ids))
        self.session.flush()
        if old_ids - new_ids:
            self.session.execute(delete(SlotAllocation).where(
                SlotAllocation.appointment_id == appointment_id, SlotAllocation.slot_id.in_(old_ids - new_ids),
            ))

    def update_appointment(
        self, appointment: Appointment, expected_version: int, destination: tuple[BookingBlock, ...] | None
    ) -> bool:
        values = {"version": Appointment.version + 1}
        if destination is None:
            values["status"] = "cancelled"
        else:
            values.update(starts_at=destination[0].starts_at, ends_at=destination[-1].ends_at)
        result = self.session.execute(update(Appointment).where(
            Appointment.id == appointment.id, Appointment.customer_id == appointment.customer_id,
            Appointment.status == "confirmed", Appointment.version == expected_version,
        ).values(**values).execution_options(synchronize_session=False))
        if result.rowcount == 1:
            self.session.refresh(appointment)
            return True
        return False

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
