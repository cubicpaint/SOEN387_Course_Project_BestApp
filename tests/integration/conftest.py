"""Real PostgreSQL tests opt into an isolated schema in a dedicated test DB."""

import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.data.database import create_session_factory

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def postgres_engine():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test")
    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg" or not (url.database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must use postgresql+psycopg and a database name ending in _test")

    # Names are generated here, never supplied by the caller. Only this schema
    # is removed; existing application or test schemas are left intact.
    schema = f"bestapp_test_{uuid4().hex}"
    admin_engine = create_engine(
        url,
        hide_parameters=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000"},
    )
    test_engine = None
    created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        test_engine = create_engine(
            url,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 3,
                "options": f"-c search_path={schema} -c timezone=UTC -c statement_timeout=5000",
            },
        )
        config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
        with test_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield test_engine
    finally:
        if test_engine is not None:
            test_engine.dispose()
        try:
            if created:
                with admin_engine.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            admin_engine.dispose()


@pytest.fixture
def session_factory(postgres_engine):
    return create_session_factory(postgres_engine)
