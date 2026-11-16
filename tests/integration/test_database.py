"""Exercise migrations, database invariants, and transaction behavior on PostgreSQL."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.data.health import check_database_ready
from app.data.models import (
    Appointment,
    AvailabilityBlock,
    Barber,
    Base,
    IdempotencyRecord,
    SlotAllocation,
    User,
)

pytestmark = pytest.mark.integration

START = datetime(2030, 1, 15, 15, tzinfo=timezone.utc)


def insert_people(session):
    session.add_all(
        [
            User(id="usr_one", email="one@example.test", password_hash="test-hash", role="customer"),
            User(id="usr_two", email="two@example.test", password_hash="test-hash", role="customer"),
            Barber(id="bar_one", display_name="Demo barber"),
        ]
    )
    session.flush()


def appointment(identifier, customer_id="usr_one", service="haircut", minutes=30):
    return Appointment(
        id=identifier,
        customer_id=customer_id,
        barber_id="bar_one",
        service=service,
        starts_at=START,
        ends_at=START + timedelta(minutes=minutes),
    )


def test_initial_migration_matches_models_and_can_be_repeated(postgres_engine):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with postgres_engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_core"
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}


def test_transactions_commit_and_rollback_without_partial_data(session_factory):
    with session_factory.begin() as session:
        insert_people(session)
    with pytest.raises(RuntimeError, match="simulated failure"):
        with session_factory.begin() as session:
            session.add(Barber(id="bar_rollback", display_name="Not committed"))
            session.flush()
            raise RuntimeError("simulated failure")
    with session_factory() as session:
        assert session.get(User, "usr_one") is not None
        assert session.get(Barber, "bar_rollback") is None


def test_one_slot_cannot_be_allocated_to_two_appointments(session_factory):
    with session_factory.begin() as session:
        insert_people(session)
        session.add(AvailabilityBlock(id="slot_one", barber_id="bar_one", starts_at=START, ends_at=START + timedelta(minutes=30)))
        session.add_all([appointment("apt_one"), appointment("apt_two", customer_id="usr_two")])
        session.flush()
        session.add(SlotAllocation(slot_id="slot_one", appointment_id="apt_one"))
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(SlotAllocation(slot_id="slot_one", appointment_id="apt_two"))
    with session_factory() as session:
        assert session.get(SlotAllocation, "slot_one").appointment_id == "apt_one"


@pytest.mark.parametrize(
    "invalid_row",
    [
        lambda: AvailabilityBlock(id="slot_bad", barber_id="bar_one", starts_at=START, ends_at=START + timedelta(minutes=60)),
        lambda: AvailabilityBlock(id="slot_bad", barber_id="bar_one", starts_at=START, ends_at=START + timedelta(minutes=30), version=0),
        lambda: appointment("apt_bad", service="both", minutes=30),
        lambda: appointment("apt_bad", customer_id="usr_missing"),
        lambda: User(id="invalid", email="invalid@example.test", password_hash="test-hash", role="customer"),
    ],
    ids=["block-duration", "version", "service-duration", "customer-fk", "id-format"],
)
def test_database_rejects_invalid_core_records(session_factory, invalid_row):
    with session_factory.begin() as session:
        insert_people(session)
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(invalid_row())


def test_barber_start_identity_is_unique_and_timezone_is_retained(session_factory):
    with session_factory.begin() as session:
        insert_people(session)
        session.add(AvailabilityBlock(id="slot_one", barber_id="bar_one", starts_at=START, ends_at=START + timedelta(minutes=30)))
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(AvailabilityBlock(id="slot_duplicate", barber_id="bar_one", starts_at=START, ends_at=START + timedelta(minutes=30)))
    with session_factory() as session:
        stored = session.get(AvailabilityBlock, "slot_one")
        assert stored.starts_at.tzinfo is not None
        assert stored.starts_at == START


def test_idempotency_keys_are_caller_scoped_and_not_operation_scoped(session_factory):
    def record(caller, operation):
        return IdempotencyRecord(
            caller_id=caller,
            key="same-key",
            operation=operation,
            fingerprint="a" * 64,
            response_status=404,
            response_body={"error": {"code": "slot_not_found", "message": "Slot not found", "retryable": False}},
        )

    with session_factory.begin() as session:
        insert_people(session)
        session.add_all([record("usr_one", "bookAppointment"), record("usr_two", "bookAppointment")])
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(record("usr_one", "differentOperation"))
    with session_factory() as session:
        stored = session.get(IdempotencyRecord, ("usr_one", "same-key"))
        assert stored.operation == "bookAppointment"
        assert stored.response_body["error"]["code"] == "slot_not_found"
        assert len(session.scalars(select(IdempotencyRecord)).all()) == 2


def test_readiness_checks_real_database_schema(postgres_engine):
    assert check_database_ready(postgres_engine)


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE alembic_version",
        "UPDATE alembic_version SET version_num = 'obsolete'",
        "DROP TABLE slot_allocations",
        "ALTER TABLE appointments DROP COLUMN version",
    ],
    ids=["unmigrated", "wrong-revision", "missing-table", "missing-column"],
)
def test_readiness_rejects_unusable_schema(postgres_engine, mutation):
    with postgres_engine.begin() as connection:
        connection.execute(text(mutation))
    assert not check_database_ready(postgres_engine)
