"""Prove the store's level marker (issue #185, migration 0006, ARCHITECTURE.md section 4.3).

The store says which migration it is at in a one-row table, `memory.schema_level`. The plugin carries the
level it needs, and the startup check compares the two (tests/test_startup_check.py). These tests keep the
three in step: the migration files, the row, and the plugin's number.
"""

import pathlib
import re

import psycopg
import pytest

from custom_build import startup_check

MIGRATIONS = sorted((pathlib.Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))
FIRST_WITH_A_LEVEL = 6


def number(path):
    return int(path.name[:4])


def test_the_store_says_which_migration_it_is_at(db):
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select level from memory.schema_level").fetchall() == [(number(MIGRATIONS[-1]),)]


def test_the_plugin_needs_the_newest_migration():
    # A plugin is built against every migration in its commit, so a new file raises the number it needs.
    assert startup_check.STORE_LEVEL == number(MIGRATIONS[-1])


def test_every_later_migration_raises_the_level_to_its_own_number():
    # One test over every file, so it runs (and never skips) while 0006 is still the newest.
    for path in MIGRATIONS:
        if number(path) > FIRST_WITH_A_LEVEL:
            assert re.search(rf"^update memory\.schema_level set level = {number(path)};$",
                             path.read_text(encoding="utf-8"), re.MULTILINE), (
                f"{path.name} must set memory.schema_level to {number(path)}, on a line of its own")


def test_a_machine_can_read_the_level(db):
    with psycopg.connect(db["agent"]) as conn:
        assert conn.execute("select level from memory.schema_level").fetchone() == (number(MIGRATIONS[-1]),)


@pytest.mark.parametrize("statement", [
    "update memory.schema_level set level = 1",
    "delete from memory.schema_level",
    "insert into memory.schema_level (only_row, level) values (false, 99)",
])
def test_a_machine_cannot_change_the_level(db, statement):
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


@pytest.mark.parametrize("row", ["(only_row, level) values (false, 99)", "(level) values (99)"])
def test_the_level_table_holds_one_row_only(db, row):
    # The check refuses a row that is not the one row; the primary key refuses a second copy of it.
    with psycopg.connect(db["owner"]) as conn, pytest.raises(psycopg.errors.IntegrityError):
        conn.execute("insert into memory.schema_level " + row)


def test_row_level_security_is_on_for_the_level(db):
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute(
            "select relrowsecurity from pg_class where oid = 'memory.schema_level'::regclass").fetchone() == (True,)


def test_the_level_policies_and_grants_are_exactly_these(db):
    with psycopg.connect(db["owner"]) as conn:
        policies = conn.execute(
            "select policyname, cmd, roles::text[] from pg_policies"
            " where schemaname = 'memory' and tablename = 'schema_level' order by policyname").fetchall()
        grants = conn.execute(
            "select grantee, privilege_type from information_schema.role_table_grants"
            " where table_schema = 'memory' and table_name = 'schema_level' and grantee like 'memory%'"
            " order by grantee, privilege_type").fetchall()
    assert policies == [("schema_level_agent_select", "SELECT", ["memory_agent"])]
    assert grants == [("memory_agent", "SELECT")]
