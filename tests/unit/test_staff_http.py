"""Only explicitly safe staff input errors may be returned to a caller."""

from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.config import AuthSettings, Settings
from app.domain.identity import Caller
from app.domain.staff_availability import InvalidAvailability
from app.domain.types import Role
from app.main import create_app


@pytest.mark.parametrize("error,status", [
    (InvalidAvailability("Availability ranges cannot overlap or be duplicated."), 422),
    (ValueError("private unexpected implementation detail"), 500),
])
def test_staff_failure_does_not_disclose_unexpected_value_error(error, status):
    app = create_app(
        Settings(DATABASE_URL="postgresql+psycopg://test:staff-unit-only@localhost/bestapp", _env_file=None),
        AuthSettings(JWT_SECRET="private-staff-unit-signing-key-only-12345", _env_file=None),
    )
    app.state.authentication = Mock()
    app.state.authentication.authenticate.return_value = Caller("usr_staff", Role.STAFF, ("bar_alex",))
    app.state.staff = Mock()
    app.state.staff.replace_availability.side_effect = error
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.put("/barbers/bar_alex/availability?date=2030-01-15", json={"ranges": []}, headers={
            "Authorization": "Bearer signed.token.value", "Idempotency-Key": "staff-error", "If-Match": '"' + "0" * 64 + '"',
        })
    assert result.status_code == status
    assert "private unexpected" not in result.text
    assert result.json()["error"]["code"] == ("validation_error" if status == 422 else "internal_error")
