"""The throwaway database the store tests run against (issue #33).

`db` gives each test an empty database with every migration applied in order, and two login roles
that join the migration's group roles, the way a maintainer adds a machine. The tests need a
Postgres 17 server named by TEST_DATABASE_URL, a superuser connection; they fail, never skip,
without it.
"""

import os
import pathlib
import uuid

import psycopg
import pytest
from psycopg import sql

MIGRATIONS = sorted((pathlib.Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))
GROUP_ROLES = ("memory_agent", "memory_maintenance")


def _drop_roles(admin, names):
    for name in names:
        admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(name)))


@pytest.fixture
def db():
    """Connection strings for the database's owner, an agent login and a maintenance login."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set: these tests need a Postgres 17 superuser connection")
    if not MIGRATIONS:
        pytest.fail("no migration files found under migrations/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    agent, maint = f"{name}_agent", f"{name}_maint"
    info = psycopg.conninfo.conninfo_to_dict(url)

    def dsn(**kw):
        return psycopg.conninfo.make_conninfo(**{**info, "dbname": name, **kw})

    with psycopg.connect(url, autocommit=True) as admin:
        _drop_roles(admin, GROUP_ROLES)   # left over from an interrupted run; group roles are server-wide
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(dsn(), autocommit=True) as owner:
                for migration in MIGRATIONS:
                    owner.execute(migration.read_text(encoding="utf-8"))
                for login, group in ((agent, "memory_agent"), (maint, "memory_maintenance")):
                    owner.execute(sql.SQL("create role {} login in role {}").format(
                        sql.Identifier(login), sql.Identifier(group)))
            yield {"owner": dsn(), "agent": dsn(user=agent), "maint": dsn(user=maint)}
        finally:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
            _drop_roles(admin, (agent, maint, *GROUP_ROLES))
