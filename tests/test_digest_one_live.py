"""Prove one live digest per session, enforced by the store (issue #156 item 4; migration 0008).

Two ends of one session at once (an eviction and the gateway stopping) could each read "no live digest" and
each store one. Migration 0008 adds a constraint checked at commit, so the plugin's own write, which inserts
the new digest and then supersedes the old in one transaction, still works (rule C10). The `db` fixture
(conftest.py) gives each test an empty database with every migration applied.
"""

import datetime
import pathlib
import uuid

import psycopg
import pytest
from psycopg import sql

from custom_build.memory import store

ROOT = pathlib.Path(__file__).resolve().parent.parent
ONE_LIVE = ROOT / "migrations" / "0008_memory_facts_one_live_digest.sql"
VECTOR = "[" + ",".join(["0.5"] * 2560) + "]"


def _digest(conn, session="s1", *, at=None, kind="digest"):
    """A live row of the session: a digest, or with `kind="fact"` a fact stored on purpose."""
    return conn.execute(
        "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, session_id, embedding,"
        " embedding_model, created_at) values ('A digest of the session.', %s, %s, %s,"
        " 'assistant_inferred', 'p1', 'host-a', %s, %s, 'qwen3-embedding:4b@test', coalesce(%s, now()))"
        " returning id", (uuid.uuid4().hex, kind, "digest" if kind == "digest" else "explicit", session, VECTOR,
                          at)).fetchone()[0]


def _live_digests(conn, session="s1"):
    return [row[0] for row in conn.execute(
        "select id from memory.facts where kind = 'digest' and session_id = %s"
        " and superseded_by is null and retired_at is null", (session,))]


def test_two_transactions_cannot_both_leave_a_live_digest_for_a_session(db):
    with psycopg.connect(db["owner"]) as first, psycopg.connect(db["owner"]) as second:
        _digest(first)
        _digest(second)
        first.commit()
        with pytest.raises(psycopg.errors.ExclusionViolation):
            second.commit()


def test_a_new_digest_inserted_before_its_predecessor_is_superseded_is_accepted(db):
    # Rule C10: the plugin deployed before 0008 inserts the new digest, then supersedes the old, in one
    # transaction (store.store). Checked at commit, the constraint never sees two live digests there.
    with psycopg.connect(db["owner"]) as conn:
        old = _digest(conn)
        conn.commit()
        new = _digest(conn)
        conn.execute("update memory.facts set superseded_by = %s where id = %s", (new, old))
        conn.commit()
        assert _live_digests(conn) == [new]


def test_only_one_sessions_live_digests_are_limited(db):
    # A guard on the constraint's reach: other sessions, digests with no session, and facts are untouched.
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        _digest(conn, "s1")
        _digest(conn, "s2")
        _digest(conn, None)
        _digest(conn, None)
        _digest(conn, "s1", kind="fact")
        _digest(conn, "s1", kind="fact")
        assert len(_live_digests(conn, "s1")) == 1 and len(_live_digests(conn, "s2")) == 1


@pytest.fixture
def before_0008(tmp_path):
    """A database with every migration before 0008 applied, and the owner's URL; dropped afterwards."""
    import os
    url = os.environ["TEST_DATABASE_URL"]
    name = f"t_{uuid.uuid4().hex[:12]}"
    info = psycopg.conninfo.conninfo_to_dict(url)
    owner = psycopg.conninfo.make_conninfo(**{**info, "dbname": name})
    earlier = [path for path in sorted((ROOT / "migrations").glob("*.sql")) if int(path.name[:4]) < 8]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(owner, autocommit=True) as conn:
                for path in earlier:
                    conn.execute(path.read_text(encoding="utf-8"))
            yield owner
        finally:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
            for role in ("memory_agent", "memory_maintenance"):
                admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role)))


def test_the_migration_keeps_only_the_newest_live_digest_of_each_session(before_0008):
    # A store that already holds the race's result: 0008 supersedes all but the newest, then adds the constraint.
    now = datetime.datetime.now(datetime.UTC)
    with psycopg.connect(before_0008, autocommit=True) as conn:
        older = [_digest(conn, "s1", at=now - datetime.timedelta(hours=hours)) for hours in (3, 2)]
        newest = _digest(conn, "s1", at=now)
        other = _digest(conn, "s2")
        conn.execute(ONE_LIVE.read_text(encoding="utf-8"))
        assert _live_digests(conn, "s1") == [newest] and _live_digests(conn, "s2") == [other]
        assert conn.execute("select array_agg(superseded_by) from memory.facts where id = any(%s)",
                            (older,)).fetchone()[0] == [newest, newest]


class _Racing:
    """An embedder whose embed() first lets another writer commit a digest of the same session: the moment
    between store.store reading the live digest and inserting its own."""

    build = "qwen3-embedding:4b@test"

    def __init__(self, owner):
        self.owner = owner

    def embed(self, conn, texts, *, timeout):
        with psycopg.connect(self.owner, autocommit=True) as other:
            _digest(other)
        return [[0.5] * 2560]


def test_a_digest_that_loses_the_race_is_dropped_not_raised(db):
    fact = store.Fact(body="The session set up the parser tests.", basis="assistant_inferred", project="p1",
                      source="host-a", session_id="s1", source_turn=3, origin="digest", kind="digest")
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        outcome = store.store(conn, fact, embedder=_Racing(db["owner"]), judge=None, timeout=5)
        assert outcome == {"dropped": "another digest of the session was stored at the same time"}
        assert len(_live_digests(conn)) == 1
