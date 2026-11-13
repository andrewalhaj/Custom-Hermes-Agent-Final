"""Prove migrations/0001_memory_events.sql (issue #33, ARCHITECTURE.md sections 4.2 and 4.3).

The `db` fixture (conftest.py) gives each test an empty database with the migration applied.
"""

import psycopg
import pytest

GROUP_ROLES = ("memory_agent", "memory_maintenance")


def _insert(conn, code="job.failed"):
    return conn.execute(
        "insert into memory.events (source, code, result) values ('test-host', %s, 'failed') returning id",
        (code,),
    ).fetchone()[0]


def test_creates_the_events_table_as_planned(db):
    with psycopg.connect(db["owner"]) as conn:
        columns = conn.execute(
            "select column_name, data_type, is_nullable from information_schema.columns"
            " where table_schema = 'memory' and table_name = 'events' order by ordinal_position"
        ).fetchall()
        tables = conn.execute("select table_name from information_schema.tables where table_schema = 'memory'").fetchall()
    assert columns == [
        ("id", "bigint", "NO"),
        ("at", "timestamp with time zone", "NO"),
        ("source", "text", "NO"),
        ("project", "text", "YES"),
        ("session_id", "text", "YES"),
        ("turn", "integer", "YES"),
        ("code", "text", "NO"),
        ("result", "text", "NO"),
        ("reason", "text", "YES"),
        ("detail", "jsonb", "NO"),
    ]
    assert tables == [("events",)]


def test_creates_the_two_indexes(db):
    with psycopg.connect(db["owner"]) as conn:
        indexes = conn.execute(
            "select indexname, indexdef from pg_indexes where schemaname = 'memory' and indexname <> 'events_pkey'"
            " order by indexname"
        ).fetchall()
    assert [(n, d.split(" USING ")[1]) for n, d in indexes] == [
        ("events_at_idx", "btree (at)"),
        ("events_code_idx", "btree (code, at)"),
    ]


def test_row_level_security_is_on(db):
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select relrowsecurity from pg_class where oid = 'memory.events'::regclass").fetchone() == (True,)


def test_group_roles_cannot_log_in(db):
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute(
            "select rolname, rolcanlogin from pg_roles where rolname = any(%s) order by rolname", (list(GROUP_ROLES),)
        ).fetchall()
    assert rows == [("memory_agent", False), ("memory_maintenance", False)]


def test_result_must_be_one_of_the_four_outcomes(db):
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into memory.events (source, code, result) values ('test-host', 'job.failed', 'error')")


def test_agent_can_insert_and_read_an_event(db):
    with psycopg.connect(db["agent"]) as conn:
        row_id = _insert(conn)
        assert conn.execute("select code, detail from memory.events where id = %s", (row_id,)).fetchone() == ("job.failed", {})


@pytest.mark.parametrize("statement", [
    "delete from memory.events",
    "update memory.events set reason = 'x'",
    "alter table memory.events add column extra text",
    "drop table memory.events",
    "create table memory.extra (id int)",
    "truncate memory.events",
])
def test_agent_is_refused(db, statement):
    with psycopg.connect(db["owner"]) as owner:
        _insert(owner)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_maintenance_can_delete_an_event(db):
    with psycopg.connect(db["owner"]) as owner:
        row_id = _insert(owner)
    with psycopg.connect(db["maint"]) as conn:
        assert conn.execute("delete from memory.events where id = %s", (row_id,)).rowcount == 1


def test_the_policies_are_exactly_these(db):
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute(
            "select policyname, cmd, roles::text[] from pg_policies where schemaname = 'memory' order by policyname"
        ).fetchall()
    assert rows == [
        ("events_agent_insert", "INSERT", ["memory_agent"]),
        ("events_agent_select", "SELECT", ["memory_agent"]),
        ("events_maintenance_delete", "DELETE", ["memory_maintenance"]),
        ("events_maintenance_select", "SELECT", ["memory_maintenance"]),
    ]


@pytest.mark.parametrize("statement", [
    "insert into memory.events (source, code, result) values ('test-host', 'job.failed', 'ok')",
    "update memory.events set reason = 'x'",
    "alter table memory.events add column extra text",
    "truncate memory.events",
    "drop table memory.events",
    "create table memory.extra (id int)",
])
def test_maintenance_is_refused(db, statement):
    with psycopg.connect(db["maint"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)
