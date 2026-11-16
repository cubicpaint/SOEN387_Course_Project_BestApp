"""Authenticate provisioned accounts and enforce current PostgreSQL permissions."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, text

from app.config import AuthSettings, Settings
from app.data.models import StaffBarber, User
from app.domain.identity import Caller
from app.domain.types import Role
from app.main import create_app
from app.services.authentication import AuthenticationService
from app.services.errors import Forbidden, InvalidCredentials, Unauthenticated
from app.services.provisioning import DEMO_BARBERS, DEMO_USERS, provision_demo
from app.services.tokens import TokenService


pytestmark = pytest.mark.integration
PASSWORD = "private authentication integration test password"
TEST_SECRET = "private-authentication-integration-signing-key-1234567890"
ALEX, SAM = (barber.id for barber in DEMO_BARBERS)


@pytest.fixture
def auth_settings():
    return AuthSettings(JWT_SECRET=TEST_SECRET, _env_file=None)


@pytest.fixture
def authentication(session_factory, auth_settings):
    provision_demo(session_factory, PASSWORD)
    return AuthenticationService(session_factory, TokenService(auth_settings))


def test_all_predefined_accounts_log_in_with_correct_roles_and_assignments(authentication):
    expected = (
        (DEMO_USERS[0], Role.CUSTOMER, ()),
        (DEMO_USERS[1], Role.CUSTOMER, ()),
        (DEMO_USERS[2], Role.STAFF, tuple(sorted((ALEX, SAM)))),
        (DEMO_USERS[3], Role.STAFF, (ALEX,)),
    )
    for user, role, assignments in expected:
        result = authentication.login(user.email, PASSWORD)

        assert result.caller == Caller(user.id, role, assignments)
        assert result.expires_in == 1800
        assert authentication.tokens.verify(result.access_token) == result.caller.id


def test_unknown_email_wrong_password_and_changed_email_case_are_rejected(authentication):
    invalid_credentials = (
        (DEMO_USERS[0].email, "an incorrect private test password"),
        ("unknown.customer@example.com", PASSWORD),
        (DEMO_USERS[0].email.upper(), PASSWORD),
        (" " + DEMO_USERS[0].email + " ", PASSWORD),
    )
    for email, password in invalid_credentials:
        with pytest.raises(InvalidCredentials):
            authentication.login(email, password)
