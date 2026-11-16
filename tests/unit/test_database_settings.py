"""Driver construction errors must not expose database credentials."""

import traceback

import pytest
from sqlalchemy.exc import ArgumentError

from app.config import ConfigurationError, Settings
from app.data.database import create_engine_from_settings


@pytest.mark.parametrize("error_type", [ArgumentError, ValueError, TypeError])
def test_engine_construction_failure_has_safe_diagnostics(monkeypatch, error_type):
    password = "credential-redaction-sentinel"
    settings = Settings(
        DATABASE_URL=f"postgresql+psycopg://demo:{password}@127.0.0.1/bestapp",
        DB_CONNECT_TIMEOUT=3,
        DB_QUERY_TIMEOUT_MS=2000,
        DB_POOL_TIMEOUT=3,
        _env_file=None,
    )

    def fail_construction(*args, **kwargs):
        raise error_type(f"Driver rejected database URL with password {password}")

    monkeypatch.setattr("app.data.database.create_engine", fail_construction)
    with pytest.raises(ConfigurationError) as caught:
        create_engine_from_settings(settings)

    diagnostic = "".join(traceback.format_exception(caught.value))
    assert password not in diagnostic
    assert "Invalid database configuration" in diagnostic
    assert "DATABASE_URL" in diagnostic
