"""Prove what is embedded, and how long it may take (issue #238, decided: a cap and a longer write limit).

On a CPU-only agent host the embedding model needs about 3 seconds for 800 characters, so a long message or
digest could never finish inside the 5-second limit and was recorded as embed.unavailable. A recall query now
embeds at most its first `recall_chars` characters (its word search still uses the whole message), and a stored
fact or digest embeds at most its first `store_chars`, with a limit of its own. The `db` fixture (conftest.py)
gives each test an empty database with every migration applied.
"""

import psycopg
import pytest

from custom_build.memory import search, store, tool, write_path

LIMITS = {"recall_chars": 1200, "store_chars": 4000, "store_timeout": 30,   # decided on #238
          "hosted_recall_timeout": 2}   # decided on #239


class Recording:
    """An embedder that records each text it is asked to embed and the limit it was given."""

    build = "qwen3-embedding:4b@test"

    def __init__(self):
        self.calls = []

    def embed(self, conn, texts, *, timeout):
        self.calls.append((texts, timeout))
        return [[0.5] * 2560 for _ in texts]


def test_the_shipped_values_are_the_decided_ones():
    assert write_path.load().get("embed") == LIMITS


def test_a_recall_query_embeds_its_first_characters_and_searches_its_words_in_full(db):
    long_query = "a" * 5000 + " parserwidget"
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        owner.execute("insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding,"
                      " embedding_model) values ('The parserwidget runs nightly.', 'h1', 'fact', 'explicit',"
                      " 'user_stated', 'p1', 'host-a', %s, 'qwen3-embedding:4b@test')", ("[" + ",".join(["0.1"] * 2560) + "]",))
    embedder = Recording()
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        found = search.candidates(conn, embedder, long_query, "p1", wanted=5, timeout=5)
    [([embedded], _)] = embedder.calls
    assert embedded == long_query[:LIMITS["recall_chars"]]
    assert any("words" in result["found_by"] for result in found)   # the word only in the message's tail


def test_a_stored_fact_embeds_its_first_characters_and_keeps_its_whole_text(db):
    body = "The deploy script " + "x" * 6000
    fact = store.Fact(body=body, basis="user_stated", project="p1", source="host-a", session_id="s1", source_turn=1)
    embedder = Recording()
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        store.store(conn, fact, embedder=embedder, judge=None, timeout=30)
        assert conn.execute("select body from memory.facts").fetchone() == (body,)
    [([embedded], _)] = embedder.calls
    assert embedded == body[:LIMITS["store_chars"]]


FACT = "The parser tests run with pytest -k parser."


def test_a_store_on_the_turn_or_at_a_session_start_keeps_the_recall_limit(db):
    # #238 review: store_fact and the mirror run inside the turn, and the queue's retry inside initialize(),
    # which Hermes does not time out; 30 seconds there could hold a turn or a start while Ollama hangs.
    fact = store.Fact(body=FACT, basis="user_stated", project="p1", source="host-a", session_id="s1", source_turn=1)
    embedder = Recording()
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        tool.attempt(conn, fact, embedder=embedder, judge_at=None)
    [(_, timeout)] = embedder.calls
    assert timeout == tool.TIMEOUT == 5


def test_the_store_fact_tool_keeps_the_recall_limit(db):
    # A guard on a real caller in the turn (#238 re-review): attempt()'s default alone would not catch it.
    embedder = Recording()
    tool.store_fact({"fact": FACT, "basis": "user_stated"}, machine="host-a", session_id="s1", turn=1,
                    embedder=embedder, judge_at=None, connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    [(_, timeout)] = embedder.calls
    assert timeout == tool.TIMEOUT


@pytest.fixture
def judge_at(typesafe):
    typesafe.answers = {"worth_keeping": 0.9, "s0": 0.9, "s1": 0.9}
    return store.Judge(url=typesafe.url, key="test-key-not-a-real-one", model="jev-1.14.0")


def test_extraction_stores_with_the_store_limit(db, judge_at, monkeypatch):
    # Off the turn's path, on Hermes's background worker (#238 option 2).
    monkeypatch.setattr(tool.extraction, "propose", lambda source: [(FACT, "user_stated")])
    embedder = Recording()
    tool.extract("Run the parser tests.", [], machine="host-a", session_id="s1", turn=1, embedder=embedder,
                 judge_at=judge_at, connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    [(_, timeout)] = embedder.calls
    assert timeout == LIMITS["store_timeout"]


def test_a_digest_stores_with_the_store_limit(db, judge_at, monkeypatch, tmp_path):
    # At a session's end; a long digest is the case that never finished in 5 seconds (#238).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))   # the digest is queued before its call (#156)
    monkeypatch.setattr(tool.digest, "write", lambda transcript: "The session set up the parser tests.")
    messages = [{"role": role, "content": f"Message {n}"} for n in range(10) for role in ("user", "assistant")]
    embedder = Recording()
    tool.write_digest(messages, machine="host-a", session_id="s1", turn=10, embedder=embedder, judge_at=judge_at,
                      connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    [(_, timeout)] = embedder.calls
    assert timeout == LIMITS["store_timeout"]
