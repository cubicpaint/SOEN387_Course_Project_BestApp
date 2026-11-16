"""Readiness checks connectivity and the schema this application requires."""

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.data.models import Base

REQUIRED_REVISION = "0001_core"


def check_database_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            if not inspector.has_table("alembic_version"):
                return False
            revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
            if revisions != {REQUIRED_REVISION}:
                return False
            for table in Base.metadata.sorted_tables:
                if not inspector.has_table(table.name):
                    return False
                columns = {column["name"] for column in inspector.get_columns(table.name)}
                if not set(table.columns.keys()).issubset(columns):
                    return False
            return True
    except SQLAlchemyError:
        return False
