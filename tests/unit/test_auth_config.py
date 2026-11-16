"""Authentication configuration is private and independent of database tools."""

import pytest
from pydantic import ValidationError

from app.config import AuthSettings, ConfigurationError, Settings, load_auth_settings


TEST_SECRET = "private-signing-key-only-for-unit-tests-12345"


def test_auth_settings_read_environment_and_use_bounded_defaults(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", TEST_SECRET)
    for name in ("JWT_ISSUER", "JWT_AUDIENCE", "JWT_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    settings = AuthSettings(_env_file=None)

    assert settings.jwt_secret.get_secret_value() == TEST_SECRET
    assert settings.jwt_issuer == "bestapp"
    assert settings.jwt_audience == "bestapp-api"
    assert settings.jwt_ttl_seconds == 1800


def test_database_only_settings_do_not_require_jwt(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://bestapp:database-test-secret@localhost/bestapp",
        _env_file=None,
    )

    assert settings.database_url.get_secret_value().endswith("/bestapp")


@pytest.mark.parametrize("secret", ["", "short-private-value", " " * 40, "replace_me_with_a_private_random_signing_secret"])
def test_short_empty_or_placeholder_signing_secrets_fail_without_disclosure(secret):
    with pytest.raises(ValidationError) as failure:
        AuthSettings(JWT_SECRET=secret, _env_file=None)

    assert "JWT_SECRET" in str(failure.value)
    if secret:
        assert secret not in str(failure.value)


def test_missing_signing_secret_is_rejected(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        AuthSettings(_env_file=None)


def test_settings_representations_hide_the_signing_secret():
    settings = AuthSettings(JWT_SECRET=TEST_SECRET, _env_file=None)

    assert TEST_SECRET not in repr(settings)
    assert TEST_SECRET not in settings.model_dump_json()


@pytest.mark.parametrize("lifetime", [0, -1, 3601])
def test_token_lifetime_cannot_exceed_the_contract(lifetime):
    with pytest.raises(ValidationError, match="JWT_TTL_SECONDS"):
        AuthSettings(JWT_SECRET=TEST_SECRET, JWT_TTL_SECONDS=lifetime, _env_file=None)


@pytest.mark.parametrize("lifetime", [1, 3600])
def test_token_lifetime_includes_both_allowed_boundaries(lifetime):
    settings = AuthSettings(JWT_SECRET=TEST_SECRET, JWT_TTL_SECONDS=lifetime, _env_file=None)

    assert settings.jwt_ttl_seconds == lifetime


@pytest.mark.parametrize("field", ["JWT_ISSUER", "JWT_AUDIENCE"])
@pytest.mark.parametrize("value", ["", " ", " surrounding-space "])
def test_issuer_and_audience_must_be_deliberate_nonempty_values(field, value):
    with pytest.raises(ValidationError, match=field):
        AuthSettings(JWT_SECRET=TEST_SECRET, **{field: value}, _env_file=None)


def test_loader_reports_only_the_invalid_setting_name(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "secret-redaction-sentinel")

    with pytest.raises(ConfigurationError) as failure:
        load_auth_settings()

    assert str(failure.value) == "Missing or invalid configuration: JWT_SECRET"
    assert "secret-redaction-sentinel" not in str(failure.value)
