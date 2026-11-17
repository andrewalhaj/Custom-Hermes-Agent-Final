"""Prove what is embedded, and how long it may take (issue #238, decided: a cap and a longer write limit).

On a CPU-only agent host the embedding model needs about 3 seconds for 800 characters, so a long message or
digest could never finish inside the 5-second limit and was recorded as embed.unavailable. A recall query now
embeds at most its first `recall_chars` characters (its word search still uses the whole message), and a stored
fact or digest embeds at most its first `store_chars`, with a limit of its own. The `db` fixture (conftest.py)
gives each test an empty database with every migration applied.
"""

import psycopg

from custom_build.memory import search, store, tool, write_path

LIMITS = {"recall_chars": 1200, "store_chars": 4000, "store_timeout": 30}   # decided on #238


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


def test_storing_waits_for_the_store_limit_not_the_recall_one(db):
    # The write path is off the turn's path (#238 option 2); recall keeps its 5 seconds inside Hermes's 8.
    fact = store.Fact(body="The parser tests run with pytest -k parser.", basis="user_stated", project="p1",
                      source="host-a", session_id="s1", source_turn=1)
    embedder = Recording()
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        tool.attempt(conn, fact, embedder=embedder, judge_at=None)
    [(_, timeout)] = embedder.calls
    assert timeout == LIMITS["store_timeout"]
