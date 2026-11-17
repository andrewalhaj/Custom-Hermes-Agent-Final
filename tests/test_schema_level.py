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


@pytest.mark.parametrize("path", [m for m in MIGRATIONS if number(m) > FIRST_WITH_A_LEVEL], ids=lambda m: m.name)
def test_every_later_migration_raises_the_level_to_its_own_number(path):
    assert re.search(rf"^update memory\.schema_level set level = {number(path)};$", path.read_text(encoding="utf-8"),
                     re.MULTILINE), f"{path.name} must end by setting memory.schema_level to {number(path)}"


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


def test_the_level_table_holds_one_row_only(db):
    with psycopg.connect(db["owner"]) as conn, pytest.raises(psycopg.errors.IntegrityError):
        conn.execute("insert into memory.schema_level (only_row, level) values (false, 99)")
