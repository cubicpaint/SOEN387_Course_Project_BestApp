"""Core persistence mappings for identity, availability, bookings, and outcomes."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("id ~ '^usr_[A-Za-z0-9_-]+$'", name="id_format"),
        CheckConstraint("role IN ('customer', 'staff')", name="role"),
        CheckConstraint("length(email) > 0", name="email_not_empty"),
        CheckConstraint("length(password_hash) > 0", name="password_hash_not_empty"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))


class Barber(Base):
    __tablename__ = "barbers"
    __table_args__ = (
        CheckConstraint("id ~ '^bar_[A-Za-z0-9_-]+$'", name="id_format"),
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_empty"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))


class StaffBarber(Base):
    __tablename__ = "staff_barbers"

    staff_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    barber_id: Mapped[str] = mapped_column(ForeignKey("barbers.id"), primary_key=True)


class AvailabilityBlock(Base):
    __tablename__ = "availability_blocks"
    __table_args__ = (
        CheckConstraint("id ~ '^slot_[A-Za-z0-9_-]+$'", name="id_format"),
        CheckConstraint("ends_at = starts_at + INTERVAL '30 minutes'", name="duration"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("barber_id", "starts_at", name="uq_availability_blocks_barber_start"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    barber_id: Mapped[str] = mapped_column(ForeignKey("barbers.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("id ~ '^apt_[A-Za-z0-9_-]+$'", name="id_format"),
        CheckConstraint("service IN ('haircut', 'beard', 'both')", name="service"),
        CheckConstraint("status IN ('confirmed', 'cancelled')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(service IN ('haircut', 'beard') AND ends_at = starts_at + INTERVAL '30 minutes') "
            "OR (service = 'both' AND ends_at = starts_at + INTERVAL '60 minutes')",
            name="service_duration",
        ),
        Index("ix_appointments_customer_status_start", "customer_id", "status", "starts_at"),
        Index("ix_appointments_barber_status_start", "barber_id", "status", "starts_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    barber_id: Mapped[str] = mapped_column(ForeignKey("barbers.id"))
    service: Mapped[str] = mapped_column(String(16))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="confirmed", server_default="confirmed")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SlotAllocation(Base):
    __tablename__ = "slot_allocations"

    slot_id: Mapped[str] = mapped_column(ForeignKey("availability_blocks.id"), primary_key=True)
    appointment_id: Mapped[str] = mapped_column(ForeignKey("appointments.id"), index=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("length(key) BETWEEN 1 AND 128", name="key_length"),
        CheckConstraint("length(operation) > 0", name="operation_not_empty"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        CheckConstraint("response_status BETWEEN 100 AND 599", name="response_status"),
    )

    caller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
