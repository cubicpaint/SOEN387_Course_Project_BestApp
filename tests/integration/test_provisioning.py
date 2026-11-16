from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

from pwdlib import PasswordHash
import pytest
from sqlalchemy import func, select, text

from app.data.models import Appointment, AvailabilityBlock, Barber, SlotAllocation, StaffBarber, User
from app.services.provisioning import DEMO_BARBERS, DEMO_USERS, DemoProvisioningError, demo_block_windows, provision_demo


pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 15, 17, tzinfo=UTC)
PASSWORD = "an integration test password only"


def test_provisioning_rerun_preserves_passwords_slots_and_booked_data(session_factory):
    first = provision_demo(session_factory, PASSWORD, now=NOW)
    assert (first.users_created, first.barbers_created, first.assignments_created, first.blocks_created) == (4, 2, 3, 224)

    with session_factory() as session, session.begin():
        users = session.scalars(select(User).order_by(User.id)).all()
        original_hashes = {user.id: user.password_hash for user in users}
        assert all(PasswordHash.recommended().verify(PASSWORD, user.password_hash) for user in users)
        block = session.scalar(select(AvailabilityBlock).order_by(AvailabilityBlock.id))
        slot_id = block.id
        block.version = 3
        # Preserve a disabled existing block too; provisioning must not re-enable it.
        another_block = session.scalar(select(AvailabilityBlock).where(AvailabilityBlock.id != slot_id))
        disabled_slot_id = another_block.id
        another_block.enabled = False
        another_block.version = 7
        session.add(
            Appointment(
                id="apt_provisioning_preserved",
                customer_id=first.users[0].id,
                barber_id=block.barber_id,
                service="haircut",
                starts_at=block.starts_at,
                ends_at=block.ends_at,
                status="confirmed",
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(SlotAllocation(slot_id=slot_id, appointment_id="apt_provisioning_preserved"))

    second = provision_demo(session_factory, "a different explicit test password", now=NOW)
    assert (second.users_created, second.barbers_created, second.assignments_created, second.blocks_created) == (0, 0, 0, 0)
    assert second.blocks_reused == 224
    assert first.users == second.users
    assert first.barbers == second.barbers
    with session_factory() as session:
        assert {user.id: user.password_hash for user in session.scalars(select(User))} == original_hashes
        assert session.get(AvailabilityBlock, slot_id).version == 3
        assert session.get(AvailabilityBlock, disabled_slot_id).enabled is False
        assert session.get(AvailabilityBlock, disabled_slot_id).version == 7
        assert session.get(SlotAllocation, slot_id).appointment_id == "apt_provisioning_preserved"
        assert session.get(Appointment, "apt_provisioning_preserved").status == "confirmed"
        assert session.scalar(select(func.count()).select_from(AvailabilityBlock)) == 224
        assert session.scalar(select(func.count()).select_from(StaffBarber)) == 3


def test_provisioning_failure_rolls_back_earlier_inserts(session_factory):
    with session_factory() as session, session.begin():
        session.add(User(id=DEMO_USERS[1].id, email="existing@example.com", role="customer", password_hash="existing hash"))

    with pytest.raises(DemoProvisioningError, match="rolled back") as error:
        provision_demo(session_factory, PASSWORD, now=NOW)
    assert PASSWORD not in str(error.value)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(Barber)) == 0
        assert session.scalar(select(func.count()).select_from(AvailabilityBlock)) == 0


def test_provisioning_reuses_matching_email_and_existing_slot_identity(session_factory):
    window = demo_block_windows(NOW)[0]
    with session_factory() as session, session.begin():
        session.add(User(id="usr_existing_demo", email=DEMO_USERS[0].email, role="customer", password_hash="existing hash"))
        session.add(Barber(id=DEMO_BARBERS[0].id, display_name="Existing display name"))
        session.flush()
        session.add(
            AvailabilityBlock(
                id="slot_existing_demo",
                barber_id=DEMO_BARBERS[0].id,
                starts_at=window.starts_at,
                ends_at=window.ends_at,
                enabled=False,
                version=9,
            )
        )

    summary = provision_demo(session_factory, PASSWORD, now=NOW)
    assert summary.users_created == 3
    assert summary.users[0].id == "usr_existing_demo"
    assert summary.barbers_created == 1
    assert summary.barbers[0].display_name == "Existing display name"
    assert summary.blocks_created == 223
    assert summary.blocks_reused == 1
    with session_factory() as session:
        assert session.get(User, "usr_existing_demo").password_hash == "existing hash"
        block = session.get(AvailabilityBlock, "slot_existing_demo")
        assert block.enabled is False
        assert block.version == 9
        assert block.starts_at == window.starts_at


def test_later_provisioning_extends_dates_without_rebinding_existing_slots(session_factory):
    provision_demo(session_factory, PASSWORD, now=NOW)
    with session_factory() as session:
        original_slots = {
            block.id: (block.barber_id, block.starts_at, block.ends_at)
            for block in session.scalars(select(AvailabilityBlock))
        }

    later = provision_demo(session_factory, PASSWORD, now=NOW + timedelta(days=1))
    assert later.blocks_created == 32
    assert later.blocks_reused == 192
    with session_factory() as session:
        for slot_id, interval in original_slots.items():
            block = session.get(AvailabilityBlock, slot_id)
            assert (block.barber_id, block.starts_at, block.ends_at) == interval
        assert session.scalar(select(func.count()).select_from(AvailabilityBlock)) == 256


def test_cli_provisions_and_reuses_persisted_data_end_to_end(postgres_engine, session_factory):
    with postgres_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    database_url = postgres_engine.url.update_query_dict({"options": f"-c search_path={schema}"})
    password = "a private CLI integration test password"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    environment["BESTAPP_DEMO_PASSWORD"] = password
    repository_root = Path(__file__).resolve().parents[2]

    def run_command():
        result = subprocess.run(
            [sys.executable, "-B", "-m", "scripts.provision_demo"],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert password not in result.stdout + result.stderr
        assert "$argon2" not in result.stdout + result.stderr
        assert "password_hash" not in result.stdout + result.stderr
        return json.loads(result.stdout)

    first = run_command()
    assert first["users_created"] == 4
    assert first["barbers_created"] == 2
    assert first["blocks_created"] == 224
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 4
        assert session.scalar(select(func.count()).select_from(AvailabilityBlock)) == 224
        slot_ids = set(session.scalars(select(AvailabilityBlock.id)))

    second = run_command()
    assert second["users_created"] == 0
    assert second["barbers_created"] == 0
    assert second["blocks_created"] == 0
    assert second["blocks_reused"] == 224
    assert second["users"] == first["users"]
    assert second["barbers"] == first["barbers"]
    with session_factory() as session:
        assert set(session.scalars(select(AvailabilityBlock.id))) == slot_ids
