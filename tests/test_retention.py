"""Prove retention, `memory.retain_events()` (issue #95, ARCHITECTURE.md 4.3, OPERATIONS.md section 4).

The `db` fixture (conftest.py) applies every migration as a superuser. Supabase's `postgres` role is not one,
so one case applies the events and retention migrations as a role with CREATEROLE alone, as Supabase's
documentation describes `postgres`. pg_cron, which runs the function daily on the project, is not in the
test server; the tests call the function the way its job does, as the role that scheduled it.
"""

import pathlib
import uuid

import psycopg
import pytest
from psycopg import sql

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def seed(db, *events):
    """Insert (code, days ago) rows."""
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        for code, days in events:
            conn.execute("insert into memory.events (at, source, code, result)"
                         " values (now() - %s * interval '1 day', 'host-a', %s, 'failed')", (days, code))


def codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select code, source, detail from memory.events order by at").fetchall()


def test_retention_deletes_events_older_than_90_days_and_records_its_run(db):
    seed(db, ("db.query", 120), ("job.failed", 90.05), ("guard.bypassed", 200), ("db.query", 89.95), ("job.failed", 1))
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


def test_a_role_like_supabases_postgres_can_apply_retention_then_run_it(db):
    # Supabase's postgres has CREATEROLE but is no superuser: it may not SET ROLE to a role it created, and
    # cannot run a function another role owns once PUBLIC's execute is revoked (Supabase knowledge base,
    # "Can't revoke a role membership granted by supabase_admin"). The migrations are renamed, because the
    # fixture's roles and schema already exist on this server.
    tag = uuid.uuid4().hex[:8]
    applier, agent, maint, schema = f"apply_{tag}", f"agent_{tag}", f"maint_{tag}", f"mem_{tag}"

    def renamed(name):
        text = (MIGRATIONS / name).read_text(encoding="utf-8")
        for old, new in (("memory_agent", agent), ("memory_maintenance", maint), ("memory.", schema + "."),
                         ("schema memory", "schema " + schema)):
            text = text.replace(old, new)
        return text

    dbname = psycopg.conninfo.conninfo_to_dict(db["owner"])["dbname"]
    with psycopg.connect(db["owner"], autocommit=True) as admin:
        admin.execute(sql.SQL("create role {} login createrole password 'x'").format(sql.Identifier(applier)))
        admin.execute(sql.SQL("grant create on database {} to {}").format(sql.Identifier(dbname), sql.Identifier(applier)))
        try:
            with psycopg.connect(psycopg.conninfo.make_conninfo(db["owner"], user=applier, password="x"),
                                 autocommit=True) as conn:
                conn.execute(renamed("0001_memory_events.sql"))
                conn.execute(renamed("0004_memory_events_retention.sql"))
                conn.execute(sql.SQL("select {}.retain_events()").format(sql.Identifier(schema)))
                ran = conn.execute(sql.SQL("select detail from {}.events where code = 'job.succeeded'").format(
                    sql.Identifier(schema))).fetchall()
        finally:
            admin.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))
            admin.execute(sql.SQL("revoke create on database {} from {}").format(sql.Identifier(dbname),
                                                                                sql.Identifier(applier)))
            for role in (agent, maint, applier):
                admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role)))
    assert ran == [({"job": "retention", "input": 0, "output": 0},)]
