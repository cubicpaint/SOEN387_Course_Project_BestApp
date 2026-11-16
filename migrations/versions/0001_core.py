"""Create the core users, availability, and booking persistence schema.

Revision ID: 0001_core
Revises:
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.CheckConstraint("id ~ '^usr_[A-Za-z0-9_-]+$'", name=op.f("ck_users_id_format")),
        sa.CheckConstraint("role IN ('customer', 'staff')", name=op.f("ck_users_role")),
        sa.CheckConstraint("length(email) > 0", name=op.f("ck_users_email_not_empty")),
        sa.CheckConstraint("length(password_hash) > 0", name=op.f("ck_users_password_hash_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "barbers",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.CheckConstraint("id ~ '^bar_[A-Za-z0-9_-]+$'", name=op.f("ck_barbers_id_format")),
        sa.CheckConstraint("length(trim(display_name)) > 0", name=op.f("ck_barbers_display_name_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_barbers")),
    )
    op.create_table(
        "staff_barbers",
        sa.Column("staff_id", sa.String(64), nullable=False),
        sa.Column("barber_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["staff_id"], ["users.id"], name=op.f("fk_staff_barbers_staff_id_users")),
        sa.ForeignKeyConstraint(["barber_id"], ["barbers.id"], name=op.f("fk_staff_barbers_barber_id_barbers")),
        sa.PrimaryKeyConstraint("staff_id", "barber_id", name=op.f("pk_staff_barbers")),
    )
    op.create_table(
        "availability_blocks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("barber_id", sa.String(64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("id ~ '^slot_[A-Za-z0-9_-]+$'", name=op.f("ck_availability_blocks_id_format")),
        sa.CheckConstraint("ends_at = starts_at + INTERVAL '30 minutes'", name=op.f("ck_availability_blocks_duration")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_availability_blocks_version_positive")),
        sa.ForeignKeyConstraint(["barber_id"], ["barbers.id"], name=op.f("fk_availability_blocks_barber_id_barbers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_availability_blocks")),
        sa.UniqueConstraint("barber_id", "starts_at", name="uq_availability_blocks_barber_start"),
    )
    op.create_table(
        "appointments",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("barber_id", sa.String(64), nullable=False),
        sa.Column("service", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), server_default="confirmed", nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id ~ '^apt_[A-Za-z0-9_-]+$'", name=op.f("ck_appointments_id_format")),
        sa.CheckConstraint("service IN ('haircut', 'beard', 'both')", name=op.f("ck_appointments_service")),
        sa.CheckConstraint("status IN ('confirmed', 'cancelled')", name=op.f("ck_appointments_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_appointments_version_positive")),
        sa.CheckConstraint(
            "(service IN ('haircut', 'beard') AND ends_at = starts_at + INTERVAL '30 minutes') "
            "OR (service = 'both' AND ends_at = starts_at + INTERVAL '60 minutes')",
            name=op.f("ck_appointments_service_duration"),
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], name=op.f("fk_appointments_customer_id_users")),
        sa.ForeignKeyConstraint(["barber_id"], ["barbers.id"], name=op.f("fk_appointments_barber_id_barbers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
    )
    op.create_index("ix_appointments_customer_status_start", "appointments", ["customer_id", "status", "starts_at"])
    op.create_index("ix_appointments_barber_status_start", "appointments", ["barber_id", "status", "starts_at"])
    op.create_table(
        "slot_allocations",
        sa.Column("slot_id", sa.String(64), nullable=False),
        sa.Column("appointment_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["slot_id"], ["availability_blocks.id"], name=op.f("fk_slot_allocations_slot_id_availability_blocks")),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], name=op.f("fk_slot_allocations_appointment_id_appointments")),
        sa.PrimaryKeyConstraint("slot_id", name=op.f("pk_slot_allocations")),
    )
    op.create_index("ix_slot_allocations_appointment_id", "slot_allocations", ["appointment_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("caller_id", sa.String(64), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(key) BETWEEN 1 AND 128", name=op.f("ck_idempotency_records_key_length")),
        sa.CheckConstraint("length(operation) > 0", name=op.f("ck_idempotency_records_operation_not_empty")),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("ck_idempotency_records_fingerprint_format")),
        sa.CheckConstraint("response_status BETWEEN 100 AND 599", name=op.f("ck_idempotency_records_response_status")),
        sa.ForeignKeyConstraint(["caller_id"], ["users.id"], name=op.f("fk_idempotency_records_caller_id_users")),
        sa.PrimaryKeyConstraint("caller_id", "key", name=op.f("pk_idempotency_records")),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("slot_allocations")
    op.drop_table("appointments")
    op.drop_table("availability_blocks")
    op.drop_table("staff_barbers")
    op.drop_table("barbers")
    op.drop_table("users")
