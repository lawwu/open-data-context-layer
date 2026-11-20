"""Every test gets its own schema in the one test database, migrated and dropped again afterwards."""
import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from odcl import migrate


@pytest.fixture
def empty_database():
    """A database URL whose search path is a fresh schema with no catalog tables in it yet."""
    base = os.environ.get("ODCL_TEST_DATABASE_URL")
    if not base:
        if os.environ.get("CI"):
            pytest.fail("ODCL_TEST_DATABASE_URL is not set")
        pytest.skip("set ODCL_TEST_DATABASE_URL to a PostgreSQL database the tests may create schemas in")
    schema = "test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(base, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def database(empty_database):
    migrate(empty_database)
    return empty_database
