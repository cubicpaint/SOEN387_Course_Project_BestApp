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
from app.interface.auth import current_caller
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
        assert authentication.authenticate(result.access_token) == result.caller


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


def test_validly_signed_tokens_cannot_identify_missing_or_deleted_accounts(authentication, session_factory):
    deleted_user = DEMO_USERS[0]
    token = authentication.login(deleted_user.email, PASSWORD).access_token
    assert authentication.authenticate(token).id == deleted_user.id

    with session_factory() as session, session.begin():
        session.execute(delete(User).where(User.id == deleted_user.id))

    for rejected in (token, authentication.tokens.issue("usr_missing_account")):
        with pytest.raises(Unauthenticated):
            authentication.authenticate(rejected)


def test_existing_token_observes_added_and_removed_staff_assignments(authentication, session_factory):
    staff = DEMO_USERS[3]
    token = authentication.login(staff.email, PASSWORD).access_token
    assert authentication.authenticate(token).managed_barber_ids == (ALEX,)

    with session_factory() as session, session.begin():
        session.add(StaffBarber(staff_id=staff.id, barber_id=SAM))

    caller = authentication.authenticate(token)
    assert caller.managed_barber_ids == tuple(sorted((ALEX, SAM)))

    with session_factory() as session, session.begin():
        session.execute(delete(StaffBarber).where(StaffBarber.staff_id == staff.id, StaffBarber.barber_id == ALEX))

    caller = authentication.authenticate(token)
    assert caller.managed_barber_ids == (SAM,)


def test_existing_token_observes_role_changes_and_customer_never_receives_staff_permissions(
    authentication, session_factory
):
    customer = DEMO_USERS[0]
    token = authentication.login(customer.email, PASSWORD).access_token
    assert authentication.authenticate(token).role == Role.CUSTOMER

    with session_factory() as session, session.begin():
        session.get(User, customer.id).role = Role.STAFF
        session.add(StaffBarber(staff_id=customer.id, barber_id=ALEX))

    staff_caller = authentication.authenticate(token)
    assert staff_caller.role == Role.STAFF
    assert staff_caller.managed_barber_ids == (ALEX,)

    with session_factory() as session, session.begin():
        session.get(User, customer.id).role = Role.CUSTOMER

    # Even a stray assignment row cannot confer staff permissions on a customer.
    customer_caller = authentication.authenticate(token)
    assert customer_caller.role == Role.CUSTOMER
    assert customer_caller.managed_barber_ids == ()




def test_http_login_and_bearer_dependency_use_the_real_database(
    authentication, postgres_engine, auth_settings
):
    with postgres_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    database_url = postgres_engine.url.update_query_dict({"options": f"-c search_path={schema}"})
    settings = Settings(DATABASE_URL=database_url.render_as_string(hide_password=False), _env_file=None)
    application = create_app(settings, auth_settings)

    # This route is registered only in this test and never enters the deployed API.
    @application.get("/test/caller")
    def test_caller(caller: Annotated[Caller, Depends(current_caller)]):
        return {"id": caller.id, "role": caller.role, "managedBarberIds": list(caller.managed_barber_ids)}

    with TestClient(application, raise_server_exceptions=False) as client:
        for user in (DEMO_USERS[0], DEMO_USERS[3]):
            response = client.post(
                "/auth/login", json={"email": user.email, "password": PASSWORD},
                headers=[("Authorization", "invalid"), ("Authorization", "Bearer invalid")],
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["user"]["id"] == user.id
            assert payload["user"]["role"] == user.role
            assert payload["tokenType"] == "Bearer"
            assert payload["expiresIn"] == 1800
            assert "password" not in response.text
            assert "$argon2" not in response.text
            UUID(response.headers["X-Correlation-ID"])
            assert response.headers["Cache-Control"] == "no-store"

            protected = client.get("/test/caller", headers={"Authorization": "Bearer " + payload["accessToken"]})
            assert protected.status_code == 200
            assert protected.json() == payload["user"]

        wrong_password = client.post(
            "/auth/login", json={"email": DEMO_USERS[0].email, "password": "a wrong password"}
        )
        unknown_email = client.post(
            "/auth/login", json={"email": "unknown.customer@example.com", "password": PASSWORD}
        )
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json() == unknown_email.json()
        assert wrong_password.headers["WWW-Authenticate"] == "Bearer"
        assert wrong_password.json()["error"]["code"] == "invalid_credentials"
