"""Prove the store function (issue #113, ARCHITECTURE.md section 3.3 step 4, defaults in 3.6).

The `db`, `ollama` and `typesafe` fixtures (conftest.py) give an empty store, an embedding service
whose vector for each text the test chooses, and a judge whose answers the test chooses.
"""

import math

import psycopg
import pytest

from custom_build.memory import embedding, judge, store

STORED = "The mini's gateway runs as root."
BASE = [1.0] + [0.0] * 2559


def _at(cosine):
    """A unit vector whose cosine with BASE is `cosine`."""
    return [cosine, math.sqrt(1 - cosine ** 2)] + [0.0] * 2558


def _store(conn, ollama, typesafe, body, *, project="p1", **fields):
    fact = store.Fact(**{"body": body, "basis": "user_stated", "project": project, "source": "host-a", "session_id": "s1",
                         "source_turn": 3, **fields})
    return store.store(conn, fact, embedder=embedding.Embedder(ollama.url),
                       judge=store.Judge(url=typesafe.url, key="test-key", model="jev-1.14.0"), timeout=2)


def _live(conn):
    return conn.execute("select body from memory.facts where superseded_by is null order by created_at, body").fetchall()


@pytest.fixture
def conn(db):
    with psycopg.connect(db["agent"], autocommit=True) as connection:
        yield connection


def test_a_new_fact_is_stored_with_everything_it_records(conn, ollama, typesafe):
    outcome = _store(conn, ollama, typesafe, STORED)
    row = conn.execute(
        "select body, kind, origin, basis, project, source, session_id, source_turn, embedding_model, superseded_by"
        " from memory.facts where id = %s", (outcome["stored"],)).fetchone()
    assert row == (STORED, "fact", "explicit", "user_stated", "p1", "host-a", "s1", 3, ollama.build, None)
    assert outcome["superseded"] is None


def test_the_same_text_again_is_not_inserted_or_embedded(conn, ollama, typesafe):
    first = _store(conn, ollama, typesafe, STORED)
    again = _store(conn, ollama, typesafe, "  the MINI'S gateway   runs as root. ")
    assert again == {"already_stored": first["stored"]}
    assert len(ollama.embedded) == 1
    assert _live(conn) == [(STORED,)]


def test_a_near_identical_short_fact_supersedes_without_asking_the_judge(conn, ollama, typesafe):
    new = "The mini's gateway runs as the hermes user."
    ollama.vectors = {STORED: BASE, new: _at(0.95)}
    old = _store(conn, ollama, typesafe, STORED)["stored"]
    outcome = _store(conn, ollama, typesafe, new)
    assert outcome["superseded"] == old
    assert conn.execute("select superseded_by from memory.facts where id = %s", (old,)).fetchone() == (outcome["stored"],)
    assert _live(conn) == [(new,)]
    assert typesafe.requests == []


@pytest.mark.parametrize("answers, supersedes", [
    ({"same_subject": 0.9, "supersedes": 0.8}, True),
    ({"same_subject": 0.9, "supersedes": 0.5}, False),
    ({"same_subject": 0.4, "supersedes": 0.9}, False),
])
def test_in_the_judges_band_the_judge_decides(conn, ollama, typesafe, answers, supersedes):
    new = "The mini's gateway service user is configured in its unit."
    ollama.vectors = {STORED: BASE, new: _at(0.85)}
    typesafe.answers = answers
    old = _store(conn, ollama, typesafe, STORED)["stored"]
    outcome = _store(conn, ollama, typesafe, new)
    assert (outcome["superseded"] == old) is supersedes
    assert len(typesafe.requests) == 1
    assert typesafe.requests[0]["body"]["state"] == {"stored": STORED, "new": new}


def test_a_long_near_identical_fact_asks_the_judge(conn, ollama, typesafe):
    new = "The mini's gateway runs as root. " + "It was set up that way before the dedicated user existed. " * 6
    assert len(new) > 300
    ollama.vectors = {STORED: BASE, new: _at(0.95)}
    typesafe.answers = {"same_subject": 0.9, "supersedes": 0.2}
    _store(conn, ollama, typesafe, STORED)
    outcome = _store(conn, ollama, typesafe, new)
    assert outcome["superseded"] is None
    assert len(typesafe.requests) == 1


def test_an_unrelated_fact_is_stored_alongside_without_asking(conn, ollama, typesafe):
    new = "Telegram carries the daily report."
    ollama.vectors = {STORED: BASE, new: _at(0.5)}
    _store(conn, ollama, typesafe, STORED)
    assert _store(conn, ollama, typesafe, new)["superseded"] is None
    assert len(_live(conn)) == 2
    assert typesafe.requests == []


@pytest.mark.parametrize("status, error", [(500, judge.JudgeError), (422, judge.JudgeError)])
def test_a_judge_failure_raises_and_stores_nothing(conn, ollama, typesafe, status, error):
    new = "The mini's gateway service user is configured in its unit."
    ollama.vectors = {STORED: BASE, new: _at(0.85)}
    typesafe.status = status
    _store(conn, ollama, typesafe, STORED)
    with pytest.raises(error):
        _store(conn, ollama, typesafe, new)
    assert _live(conn) == [(STORED,)]


def test_only_live_facts_of_kind_fact_in_the_same_project_are_compared(conn, ollama, typesafe, db):
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        for kind, origin, project, superseded in (("digest", "digest", "p1", False), ("fact", "explicit", "p2", False),
                                                  ("fact", "explicit", "p1", True)):
            owner.execute(
                "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding, embedding_model)"
                " values (%s, %s, %s, %s, 'user_stated', %s, 'host-b', %s, %s)",
                (f"{kind} in {project}", f"h-{kind}-{project}-{superseded}", kind, origin, project, str(BASE), ollama.build))
            if superseded:
                owner.execute("update memory.facts set superseded_by = id where body_hash = %s",
                              (f"h-{kind}-{project}-{superseded}",))
    ollama.vectors = {STORED: BASE}
    outcome = _store(conn, ollama, typesafe, STORED)
    assert outcome["superseded"] is None
    assert typesafe.requests == []


def test_facts_with_no_project_are_compared_with_each_other(conn, ollama, typesafe):
    new = "The mini's gateway runs as the hermes user."
    ollama.vectors = {STORED: BASE, new: _at(0.95)}
    old = _store(conn, ollama, typesafe, STORED, project=None)["stored"]
    assert _store(conn, ollama, typesafe, new, project=None)["superseded"] == old


def test_an_extracted_fact_is_stored_with_its_origin(conn, ollama, typesafe):
    outcome = _store(conn, ollama, typesafe, STORED, origin="extracted", basis="tool_observed")
    row = conn.execute("select origin, basis from memory.facts where id = %s", (outcome["stored"],)).fetchone()
    assert row == ("extracted", "tool_observed")


@pytest.mark.parametrize("cosine", [0.95, 0.85])   # without the judge, and in the judge's band
def test_an_extracted_fact_never_supersedes_one_stored_on_purpose(conn, ollama, typesafe, cosine):
    # Rule 5 (ARCHITECTURE.md 3.3, #148): both stay live, and the judge is not asked.
    new = "The mini's gateway runs as the hermes user."
    ollama.vectors = {STORED: BASE, new: _at(cosine)}
    _store(conn, ollama, typesafe, STORED)
    outcome = _store(conn, ollama, typesafe, new, origin="extracted")
    assert outcome["superseded"] is None
    assert _live(conn) == [(STORED,), (new,)]
    assert typesafe.requests == []


def test_a_digest_is_stored_as_one_and_never_supersedes_a_fact(conn, ollama, typesafe):
    # #155: a digest is not compared with facts, so even a near-identical one leaves them live, unasked.
    digest = "The session moved the mini's gateway to the hermes user.\n\nOpen items:\n- Restart the gateway."
    ollama.vectors = {STORED: BASE, digest: _at(0.95)}
    _store(conn, ollama, typesafe, STORED)
    outcome = _store(conn, ollama, typesafe, digest, kind="digest", origin="digest", basis="assistant_inferred")
    assert outcome["superseded"] is None
    assert _live(conn) == [(STORED,), (digest,)]
    assert typesafe.requests == []
    row = conn.execute("select kind, origin, basis from memory.facts where id = %s", (outcome["stored"],)).fetchone()
    assert row == ("digest", "digest", "assistant_inferred")


def _digest(conn, ollama, typesafe, body, *, turn, written):
    return _store(conn, ollama, typesafe, body, kind="digest", origin="digest", basis="assistant_inferred",
                  source_turn=turn, written_at=f"2026-11-16T{written}:00+00:00")


def test_a_digest_written_earlier_never_replaces_the_sessions_later_one(conn, ollama, typesafe):
    # #156, decided B: a queued older digest must not replace the one written after it.
    later = _digest(conn, ollama, typesafe, "The digest written at 10:05.", turn=7, written="10:05")["stored"]
    outcome = _digest(conn, ollama, typesafe, "The digest written at 10:00.", turn=7, written="10:00")
    assert outcome == {"dropped": "a later digest of the session is live"}
    assert _live(conn) == [("The digest written at 10:05.",)]


def test_a_later_digest_replaces_the_sessions_earlier_one_even_at_a_lower_turn(conn, ollama, typesafe):
    # #156 review: a resumed session counts its turns again from its compressed history, so turns go down.
    earlier = _digest(conn, ollama, typesafe, "The digest at eviction, turn 42.", turn=42, written="10:00")["stored"]
    outcome = _digest(conn, ollama, typesafe, "The digest at the real end, turn 8.", turn=8, written="11:00")
    assert outcome["superseded"] == earlier
    assert _live(conn) == [("The digest at the real end, turn 8.",)]
