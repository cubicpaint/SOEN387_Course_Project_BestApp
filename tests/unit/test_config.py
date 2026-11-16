"""Configuration failures must be useful without disclosing credentials."""

import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, Settings, load_settings


DATABASE_URL = "postgresql+psycopg://bestapp:configuration-test-secret@localhost:5432/bestapp"


def test_database_configuration_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    settings = Settings(_env_file=None)

    assert settings.database_url.get_secret_value() == DATABASE_URL


def test_missing_database_configuration_names_the_required_setting(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None)

    assert "DATABASE_URL" in str(failure.value)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-database-url",
        "sqlite:///bestapp.db",
        "postgresql+asyncpg://bestapp:configuration-test-secret@localhost/bestapp",
        "postgresql+psycopg://bestapp:configuration-test-secret@localhost/bestapp?port=secret-value",
        "postgresql+psycopg://bestapp:configuration-test-secret@localhost/bestapp?sslmode=invalid",
        "postgresql+psycopg://bestapp:configuration-test-secret@localhost/bestapp?options=a&options=b",
    ],
)
def test_invalid_database_configuration_is_rejected_without_echoing_secrets(url):
    with pytest.raises(ValidationError) as failure:
        Settings(DATABASE_URL=url, _env_file=None)

    message = str(failure.value)
    assert "DATABASE_URL" in message
    assert "configuration-test-secret" not in message


def test_settings_representations_hide_the_database_password():
    settings = Settings(DATABASE_URL=DATABASE_URL, _env_file=None)

    assert "configuration-test-secret" not in repr(settings)
    assert "configuration-test-secret" not in settings.model_dump_json()


def test_startup_configuration_error_reports_the_field_without_credentials(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://bestapp:configuration-test-secret@localhost/bestapp",
    )

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    assert "DATABASE_URL" in str(failure.value)
    assert "configuration-test-secret" not in str(failure.value)
    assert "postgresql" not in str(failure.value)
