"""Database outages must not be disguised as invalid passwords or tokens."""

import pytest
from sqlalchemy.exc import OperationalError

from app.config import AuthSettings
from app.services.authentication import AuthenticationService
from app.services.errors import ServiceUnavailable
from app.services.tokens import TokenService


@pytest.mark.parametrize("operation", ["login", "authenticate"])
def test_authentication_data_source_failures_are_safe_service_failures(operation):
    def unavailable_session():
        raise OperationalError("private-database-sentinel", {}, RuntimeError("private-driver-sentinel"))

    tokens = TokenService(AuthSettings(JWT_SECRET="private-service-unit-test-key-only-123456", _env_file=None))
    service = AuthenticationService(unavailable_session, tokens)
    with pytest.raises(ServiceUnavailable) as failure:
        if operation == "login":
            service.login("customer@example.test", "private-password-sentinel")
        else:
            service.authenticate(tokens.issue("usr_one"))
    assert str(failure.value) == ""
    assert failure.value.__suppress_context__ is True
