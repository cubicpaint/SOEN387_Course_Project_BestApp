"""Run explicit migrations using the same settings as the application."""

from alembic import context
from alembic.util import CommandError
from sqlalchemy.exc import SQLAlchemyError

from app.config import ConfigurationError, load_settings
from app.data.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    settings = load_settings()
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate_connection(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        migrate_connection(supplied_connection)
        return

    from app.data.database import create_engine_from_settings

    engine = create_engine_from_settings(load_settings())
    try:
        with engine.connect() as connection:
            migrate_connection(connection)
    finally:
        engine.dispose()


try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
except SQLAlchemyError:
    raise CommandError("Database migration failed. Check the database connection and migration state.") from None
except ConfigurationError as error:
    raise CommandError(str(error)) from None
