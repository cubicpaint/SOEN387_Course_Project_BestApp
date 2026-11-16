"""Synchronous SQLAlchemy connections with bounded dependency waits."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import ConfigurationError, Settings


def create_engine_from_settings(settings: Settings) -> Engine:
    try:
        url = make_url(settings.database_url.get_secret_value())
        options = url.query.get("options", "")
        timeout_options = f"{options} -c timezone=UTC -c statement_timeout={settings.db_query_timeout_ms}".strip()
        return create_engine(
            url,
            isolation_level="READ COMMITTED",
            pool_pre_ping=True,
            pool_timeout=settings.db_pool_timeout,
            hide_parameters=True,
            connect_args={
                "connect_timeout": settings.db_connect_timeout,
                "options": timeout_options,
            },
        )
    except (SQLAlchemyError, ValueError, TypeError):
        raise ConfigurationError("Invalid database configuration. Check DATABASE_URL and database timeout settings.") from None


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Services own transactions; repositories receive their service's session."""
    return sessionmaker(bind=engine, expire_on_commit=False)
