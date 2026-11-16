"""Prove retention, `memory.retain_events()` (issue #95, ARCHITECTURE.md 4.3, OPERATIONS.md section 4).

The `db` fixture (conftest.py) applies every migration as a superuser, as Supabase's own role applies
them. pg_cron, which runs the function daily on the project, is not in the test server; the tests call
the function the way its job does.
"""

import psycopg
import pytest
from psycopg import sql


def seed(db, *events):
    """Insert (code, days ago) rows."""
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        for code, days in events:
            conn.execute("insert into memory.events (at, source, code, result)"
                         " values (now() - make_interval(days => %s), 'host-a', %s, 'failed')", (days, code))


def codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select code, source, detail from memory.events order by at").fetchall()


def test_retention_deletes_events_older_than_90_days_and_records_its_run(db):
    seed(db, ("db.query", 120), ("job.failed", 91), ("guard.bypassed", 200), ("db.query", 89), ("job.failed", 1))
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        conn.execute("select memory.retain_events()")
    assert codes(db) == [
        ("guard.bypassed", "host-a", {}),   # kept: the heartbeat would record a live session again (#95)
        ("db.query", "host-a", {}),
        ("job.failed", "host-a", {}),
        ("job.succeeded", "database", {"job": "retention", "input": 2, "output": 2}),
    ]


def test_a_machine_login_cannot_run_retention(db):
    with psycopg.connect(db["agent"], autocommit=True) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select memory.retain_events()")


def test_retention_runs_with_the_maintenance_role_alone(db):
    # Run by a role with no rights on events, it still deletes and records: it runs as memory_maintenance,
    # with its search_path fixed, as the PostgreSQL documentation advises for security definer functions.
    seed(db, ("db.query", 120))
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        conn.execute(sql.SQL("create role {} login password 'x'").format(sql.Identifier("cron_runner")))
        try:
            conn.execute("grant usage on schema memory to cron_runner")
            conn.execute("grant execute on function memory.retain_events() to cron_runner")
            conn.execute("set role cron_runner")
            conn.execute("select memory.retain_events()")
            conn.execute("reset role")
            owner, config = conn.execute("select pg_get_userbyid(proowner), proconfig from pg_proc"
                                         " where proname = 'retain_events'").fetchone()
        finally:
            conn.execute("reset role")
            conn.execute("drop owned by cron_runner")
            conn.execute("drop role cron_runner")
    assert codes(db) == [("job.succeeded", "database", {"job": "retention", "input": 1, "output": 1})]
    assert owner == "memory_maintenance" and config == ["search_path=pg_catalog, pg_temp"]
