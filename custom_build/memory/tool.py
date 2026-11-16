"""The memory tool the agent stores a fact with on purpose (ARCHITECTURE.md sections 2 and 3.3).

A failure the tool expects is recorded with its code and returned in an `error` field, never as a
stored fact (CONTRIBUTING.md rule C3); Hermes then reports the call with the status `error`.
"""

import psycopg
from agent.redact import redact_sensitive_text
from agent.runtime_cwd import resolve_agent_cwd

from .. import failures
from . import embedding, judge, project, queue, store, write_path

BASES = ("user_stated", "tool_observed", "assistant_inferred")
TIMEOUT = 5   # seconds for each answer from Ollama while a fact is stored

SCHEMA = {
    "name": "store_fact",
    "description": ("Store one fact in long-term memory, for this session and later ones: a plain statement of "
                    "something the user stated, a tool showed, or you inferred, with `basis` saying which. "
                    "Not a plan, a step you are about to take, or how something felt."),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact, as one plain statement."},
            "basis": {"type": "string", "enum": list(BASES),
                      "description": "What the fact rests on: the user stated it, a tool showed it, or you inferred it."},
        },
        "required": ["fact", "basis"],
    },
}


def store_fact(args, *, machine, session_id, turn, embedder, judge_at, connect):
    """Run the tool: {"refused": why}, store.store()'s outcome, or {"error": code}."""
    body = redact_sensitive_text(str(args.get("fact", "")), force=True)   # before anything sees it (rule L4)
    if args.get("basis") not in BASES:
        return {"refused": f"basis must be one of {', '.join(BASES)}"}
    if reason := write_path.refusal(body):
        return {"refused": reason}
    fact = store.Fact(body=body, basis=args["basis"], project=project.key(resolve_agent_cwd()), source=machine,
                      session_id=session_id, source_turn=turn)
    return _with_store(machine, session_id, connect, lambda conn: _save(conn, fact, embedder=embedder, judge_at=judge_at))


def mirror(action, content, metadata, *, machine, session_id, turn, embedder, judge_at, connect):
    """Mirror one committed built-in memory write (ARCHITECTURE.md section 2): an added entry is stored as a
    fact the user stated, a replaced one supersedes the old entry's fact, a removed one retires it.

    A person approved the entry (`memory.write_approval`), so the stored-on-purpose filter does not apply
    (#142); redaction and the supersede check do. Failures are recorded with their codes; Hermes has
    already committed the write, so nothing is returned to it.
    """
    body = redact_sensitive_text(str(content), force=True)
    key = project.key(resolve_agent_cwd())

    def work(conn):
        if action == "remove":
            store.retire(conn, key, body, "removed from built-in memory")
            return
        fact = store.Fact(body=body, basis="user_stated", project=key, source=machine, session_id=session_id,
                          source_turn=turn)
        outcome = _save(conn, fact, embedder=embedder, judge_at=judge_at)
        new = outcome.get("stored") or outcome.get("already_stored")
        if action == "replace" and new and metadata.get("previous_content"):
            store.supersede(conn, key, redact_sensitive_text(str(metadata["previous_content"]), force=True), new)

    _with_store(machine, session_id, connect, work)


def _with_store(machine, session_id, connect, work):
    """work(conn) on a new connection, or {"error": code} when it cannot open, recorded as db.auth or db.unreachable."""
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        if failures.refused_login(exc):
            failures.record(None, "db.auth", "failed", source=machine, reason=str(exc), session_id=session_id)
            return {"error": "db.auth"}
        failures.record(None, "db.unreachable", "failed", source=machine, reason=str(exc), session_id=session_id)
        return {"error": "db.unreachable"}
    with conn:
        return work(conn)


def _save(conn, fact, *, embedder, judge_at):
    """attempt(), with a fact the judge could not decide on queued for retry (store.gate_error)."""
    outcome = attempt(conn, fact, embedder=embedder, judge_at=judge_at)
    if outcome.get("error") in ("judge.error", "judge.timeout"):
        queue.add(fact)
        failures.record(conn, "store.gate_error", "failed", source=fact.source, session_id=fact.session_id,
                        reason=f"{outcome['error']}; the fact is queued for retry")
        return {"error": "store.gate_error", "queued": True}
    return outcome


def attempt(conn, fact, *, embedder, judge_at):
    """store.store(), with each failure it expects recorded under its code and returned as {"error": code}."""
    fields = {"source": fact.source, "project": fact.project, "session_id": fact.session_id, "turn": fact.source_turn}
    try:
        return store.store(conn, fact, embedder=embedder, judge=judge_at, timeout=TIMEOUT)
    except embedding.EmbedUnavailable as exc:
        failures.record(conn, "embed.unavailable", "failed", reason=str(exc), **fields)
        return {"error": "embed.unavailable"}
    except embedding.ModelMismatch as exc:
        failures.record(conn, "embed.model_mismatch", "failed", reason=str(exc), **fields)
        return {"error": "embed.model_mismatch"}
    except judge.JudgeTimeout as exc:
        failures.record(conn, "judge.timeout", "failed", reason=str(exc), **fields)
        return {"error": "judge.timeout"}
    except judge.JudgeError as exc:
        failures.record(conn, "judge.error", "failed", reason=str(exc), **fields)
        return {"error": "judge.error"}
    except psycopg.OperationalError as exc:   # the store went away mid-write
        failures.record(conn, "db.unreachable", "failed", reason=str(exc), **fields)
        return {"error": "db.unreachable"}
    except psycopg.Error as exc:
        failures.record(conn, "db.query", "failed", reason=str(exc), **fields)
        return {"error": "db.query"}


def retry_queued(conn, *, embedder, judge_at):
    """Store each queued fact the judge can now decide on; one stored, or found already stored, is recorded
    as degraded. The first judge failure ends the pass, so a judge still down costs one request a start."""
    for row_id, fact, attempts in queue.waiting():
        outcome = attempt(conn, fact, embedder=embedder, judge_at=judge_at)
        if "error" in outcome:
            queue.again(row_id)   # still waiting; its failure is recorded under its own code
            if outcome["error"] in ("judge.error", "judge.timeout"):
                return
            continue
        queue.done(row_id)
        failures.record(conn, "store.gate_error", "degraded", source=fact.source, project=fact.project,
                        session_id=fact.session_id, turn=fact.source_turn, reason="resolved on retry",
                        detail={"attempts": attempts + 1})
