"""Reject token forgery and invalid claims, including the exact expiry boundary."""

from datetime import UTC, datetime

import jwt
import pytest

from app.config import AuthSettings
from app.services.errors import Unauthenticated
from app.services.tokens import TokenService


TEST_SECRET = "private-signing-key-only-for-token-tests-with-sufficient-length-1234567890"
NOW = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)


@pytest.fixture
def settings():
    return AuthSettings(JWT_SECRET=TEST_SECRET, _env_file=None)


@pytest.fixture(autouse=True)
def frozen_library_clock(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(jwt.api_jwt, "datetime", FrozenDateTime)


@pytest.fixture
def tokens(settings):
    return TokenService(settings, clock=lambda: NOW)


def claims(**changes):
    values = {
        "sub": "usr_customer_1", "iss": "bestapp", "aud": "bestapp-api",
        "iat": int(NOW.timestamp()), "exp": int(NOW.timestamp()) + 1800,
    }
    values.update(changes)
    return values


def signed(values):
    return jwt.encode(values, TEST_SECRET, algorithm="HS256")


def test_issued_token_round_trips_and_contains_only_identity_metadata(tokens):
    token = tokens.issue("usr_customer_1")

    assert tokens.verify(token) == "usr_customer_1"
    verified = jwt.decode(token, TEST_SECRET, algorithms=["HS256"], issuer="bestapp", audience="bestapp-api")
    assert verified == claims()
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


@pytest.mark.parametrize("field", ["sub", "iss", "aud", "iat", "exp"])
def test_missing_required_claim_is_rejected(tokens, field):
    values = claims()
    del values[field]

    with pytest.raises(Unauthenticated):
        tokens.verify(signed(values))


@pytest.mark.parametrize("changes", [
    {"iss": "another-issuer"}, {"aud": "another-service"}, {"aud": ["bestapp-api"]},
    {"sub": "another_user"}, {"sub": "usr_"}, {"sub": "usr_" + "x" * 61}, {"sub": 7},
    {"iat": int(NOW.timestamp()) + 1}, {"iat": str(int(NOW.timestamp()))}, {"iat": True},
    {"exp": str(int(NOW.timestamp()) + 1800)}, {"exp": float("inf")}, {"exp": None},
    {"exp": int(NOW.timestamp()) + 3601}, {"nbf": int(NOW.timestamp()) + 1},
])
def test_invalid_claim_values_are_rejected(tokens, changes):
    with pytest.raises(Unauthenticated):
        tokens.verify(signed(claims(**changes)))


@pytest.mark.parametrize("offset", [-1, 0])
def test_token_is_invalid_at_and_after_its_exact_expiry(tokens, offset):
    values = claims(iat=int(NOW.timestamp()) - 1800, exp=int(NOW.timestamp()) + offset)

    with pytest.raises(Unauthenticated):
        tokens.verify(signed(values))


def test_token_is_valid_immediately_before_expiry(tokens):
    token = signed(claims(iat=int(NOW.timestamp()) - 1799, exp=int(NOW.timestamp()) + 1))

    assert tokens.verify(token) == "usr_customer_1"


def test_modified_payload_is_rejected(tokens):
    original = tokens.issue("usr_customer_1").split(".")
    altered = signed(claims(sub="usr_customer_2")).split(".")
    forged = ".".join((original[0], altered[1], original[2]))

    with pytest.raises(Unauthenticated):
        tokens.verify(forged)


def test_wrong_signing_key_is_rejected(tokens):
    token = jwt.encode(claims(), "another-private-test-key-at-least-32-bytes", algorithm="HS256")

    with pytest.raises(Unauthenticated):
        tokens.verify(token)


@pytest.mark.parametrize("algorithm", ["none", "HS384"])
def test_token_cannot_select_another_algorithm(tokens, algorithm):
    token = jwt.encode(claims(), "" if algorithm == "none" else TEST_SECRET, algorithm=algorithm)

    with pytest.raises(Unauthenticated):
        tokens.verify(token)


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c", "Bearer a.b.c"])
def test_malformed_token_is_rejected(tokens, token):
    with pytest.raises(Unauthenticated):
        tokens.verify(token)


def test_configured_lifetime_is_used(settings):
    tokens = TokenService(settings.model_copy(update={"jwt_ttl_seconds": 60}), clock=lambda: NOW)
    values = jwt.decode(tokens.issue("usr_customer_1"), TEST_SECRET, algorithms=["HS256"], audience="bestapp-api")

    assert values["exp"] - values["iat"] == 60


def test_naive_issue_clock_is_rejected(settings):
    tokens = TokenService(settings, clock=lambda: NOW.replace(tzinfo=None))

    with pytest.raises(ValueError, match="timezone"):
        tokens.issue("usr_customer_1")
