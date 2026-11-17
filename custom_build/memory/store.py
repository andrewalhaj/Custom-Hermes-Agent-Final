"""The one store function the write path's entrances go through (ARCHITECTURE.md section 3.3, step 4).

It skips a fact whose text is already stored, embeds the rest, and compares each fact with the nearest live
fact of the same project: close enough, the new fact supersedes it, in the same transaction as its
insert. A digest is never compared with facts; it supersedes its session's earlier digest (#156). The
thresholds and the judge's questions are in `write_path.toml`.
"""

import dataclasses
import datetime
import hashlib

import psycopg

from . import judge as judge_api
from . import write_path

LIVE = "superseded_by is null and retired_at is null"   # the facts index's predicate, repeated exactly


@dataclasses.dataclass(frozen=True)
class Fact:
    """A fact to store, already redacted: stored on purpose (`explicit`), `extracted`, or a session's `digest`."""
    body: str
    basis: str
    project: str | None
    source: str
    session_id: str | None
    source_turn: int | None
    origin: str = "explicit"
    kind: str = "fact"   # or "digest", whose origin is "digest" (the table's check)
    written_at: str | None = None   # a digest's ISO time of writing, kept through the queue; orders its session's digests
    gate: dict | None = None   # an extracted fact's source turn, which the judge checks it against first


@dataclasses.dataclass(frozen=True)
class Judge:
    """Where the judge is and how to reach it: TypeSafe's URL, the profile's key and the pinned model."""
    url: str
    key: str
    model: str


def body_hash(body):
    """The identical-text check's key: the text with its whitespace collapsed and its case folded."""
    return hashlib.sha256(" ".join(body.split()).casefold().encode()).hexdigest()


def store(conn, fact, *, embedder, judge, timeout):
    """Store `fact`: {"already_stored": id}, or {"stored": id, "superseded": the replaced id or None}.

    Raises, storing nothing: embedding's EmbedUnavailable and ModelMismatch, the judge's JudgeError and
    JudgeTimeout, and psycopg's errors. `timeout` bounds each answer from Ollama; the judge has its own
    deadline (3.6).
    """
    digest = body_hash(fact.body)
    if existing := _identical(conn, fact.project, digest):
        return {"already_stored": existing}
    live = live_digest(conn, fact.session_id) if fact.kind == "digest" else None
    # By time of writing, not turn: a resumed session counts its turns again from its compressed history.
    if live and fact.written_at and live[1] > datetime.datetime.fromisoformat(fact.written_at):
        return {"dropped": "a later digest of the session is live"}
    [vector] = embedder.embed(conn, [fact.body], timeout=timeout)
    if fact.kind == "digest":
        # A digest is never compared with facts; it replaces only its session's earlier digest (#155, #156).
        old = live[0] if live else None
    else:
        nearest = _nearest(conn, fact.project, str(vector))
        # Rule 5 (3.3): an extracted fact never supersedes one stored on purpose.
        protected = nearest and fact.origin == "extracted" and nearest[3] == "explicit"
        old = _replaced(nearest[:3], fact.body, judge) if nearest and not protected else None
    try:
        with conn.transaction():
            row = conn.execute(
                "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, session_id,"
                " source_turn, embedding, embedding_model, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()))"
                f" on conflict (project, body_hash) where {LIVE} do nothing returning id",
                (fact.body, digest, fact.kind, fact.origin, fact.basis, fact.project, fact.source, fact.session_id,
                 fact.source_turn, str(vector), embedder.build, fact.written_at),
            ).fetchone()
            if row is None:   # the same text was stored since the check above
                return {"already_stored": _identical(conn, fact.project, digest)}
            if old and not conn.execute("update memory.facts set superseded_by = %s where id = %s"
                                        " and superseded_by is null", (row[0], old)).rowcount:
                old = None   # superseded by something else since it was read
    except psycopg.errors.ExclusionViolation:
        # Migration 0008's one live digest per session, checked at commit: another end of the session stored its
        # digest between our read and our commit, so this one is dropped and theirs stays (#156 item 4).
        return {"dropped": "another digest of the session was stored at the same time"}
    return {"stored": row[0], "superseded": old}


def live_digest(conn, session_id):
    """The session's live digest as (id, created_at), or None. A digest written later replaces it (#156)."""
    return conn.execute(f"select id, created_at from memory.facts where session_id = %s and kind = 'digest' and {LIVE}"
                        " order by created_at desc limit 1", (session_id,)).fetchone()


def identical(conn, project, body):
    """The id of the live fact in the project with the same text as `body`, or None."""
    return _identical(conn, project, body_hash(body))


def _identical(conn, project, digest):
    row = conn.execute(f"select id from memory.facts where project is not distinct from %s and body_hash = %s and {LIVE}",
                       (project, digest)).fetchone()
    return row[0] if row else None


def _nearest(conn, project, vector):
    """The nearest live fact of kind `fact` in the project: (id, body, cosine similarity, origin), or None."""
    with conn.transaction():   # `set local` lasts for this transaction only
        # Filters apply after the index scan; keep scanning until a row passes them (pgvector 0.8.0).
        conn.execute("set local hnsw.iterative_scan = strict_order")
        return conn.execute(
            f"select id, body, 1 - (embedding <=> %s), origin from memory.facts where project is not distinct from %s"
            f" and kind = 'fact' and {LIVE} order by embedding <=> %s limit 1", (vector, project, vector),
        ).fetchone()


def _replaced(nearest, body, judge):
    """The id of the fact the new one supersedes, or None (3.3 step 4)."""
    old_id, old_body, similarity = nearest
    limits = write_path.load()["supersede"]
    if similarity >= limits["without_judge"] and max(len(body), len(old_body)) <= limits["short_text"]:
        return old_id
    if similarity >= limits["ask_judge_from"]:
        answers = judge_api.supersede_answers(old_body, body, url=judge.url, key=judge.key, model=judge.model,
                                              timeout=judge_api.DEADLINE)
        return old_id if judge_api.supersedes(answers) else None
    return None


def supersede(conn, project, old_body, new_id):
    """Point the live fact holding `old_body` in the project at `new_id`, the fact that replaced it."""
    conn.execute(f"update memory.facts set superseded_by = %s where project is not distinct from %s and body_hash = %s"
                 f" and id <> %s and {LIVE}", (new_id, project, body_hash(old_body), new_id))


def retire(conn, project, body, reason):
    """Retire the live fact holding `body` in the project, with the reason; the row stays (4.1)."""
    conn.execute(f"update memory.facts set retired_at = now(), retired_reason = %s where project is not distinct from %s"
                 f" and body_hash = %s and {LIVE}", (reason, project, body_hash(body)))
