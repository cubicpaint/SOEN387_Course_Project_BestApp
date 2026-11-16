"""Check account lookup and trusted caller data against PostgreSQL."""

import pytest
from app.data.users import UserRepository
from app.domain.identity import Caller
from app.domain.types import Role
from app.services.provisioning import DEMO_BARBERS, DEMO_USERS, provision_demo

pytestmark = pytest.mark.integration


def test_account_queries_preserve_exact_email_and_staff_assignments(session_factory):
    provision_demo(session_factory, 'private account query integration test password')
    alex, sam = (barber.id for barber in DEMO_BARBERS)
    expected = [(Role.CUSTOMER, ()), (Role.CUSTOMER, ()),
                (Role.STAFF, tuple(sorted((alex, sam)))), (Role.STAFF, (alex,))]
    with session_factory() as session:
        repository = UserRepository(session)
        for demo, (role, assignments) in zip(DEMO_USERS, expected):
            user = repository.by_email(demo.email)
            assert repository.by_id(demo.id) is user
            assert repository.caller(user) == Caller(demo.id, role, assignments)
            assert repository.by_email(demo.email.upper()) is None
        assert repository.by_email('unknown@example.test') is None
