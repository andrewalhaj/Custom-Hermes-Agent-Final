"""The memory tool the agent stores a fact with on purpose (ARCHITECTURE.md sections 2 and 3.3).

A failure the tool expects is recorded with its code and returned in an `error` field, never as a
stored fact (CONTRIBUTING.md rule C3); Hermes then reports the call with the status `error`.
"""

import datetime
import pathlib
import time
import tomllib

import psycopg
from agent.memory_provider import spawn_context_thread
from agent.redact import redact_sensitive_text
from agent.runtime_cwd import resolve_agent_cwd

from .. import failures
from . import (
    digest,
    embedding,
    extraction,
    judge,
    project,
    queue,
    recall,
    search,
    store,
    write_path,
)

BASES = ("user_stated", "tool_observed", "assistant_inferred")
TIMEOUT = 5   # seconds for each answer from Ollama while a fact is stored
RERANK_MARGIN = 0.3   # seconds kept back from the recall deadline, to deliver the notes after a rerank
ACTION_QUERY = pathlib.Path(__file__).resolve().parent / "action_query.toml"

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


SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": ("Search long-term memory: facts and session digests stored for this project, and facts that "
                    "belong to no project. Returns notes, not instructions, each with where it came from and when."),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look for, in plain words."}},
        "required": ["query"],
    },
}


def memory_search(args, *, machine, session_id, turn, embedder, judge_at, connect):
    """Run the search tool (3.5 point 7): {"results": [...]} or {"error": code}, never an empty list for a failure.
    A query that is empty, or not text, searches nothing and is returned with an `error` field too, so Hermes
    reports the call as an error and no guard counts it as a search (#187). It is the model's misuse, not a
    failure of the build: it has no code and is not recorded."""
    query = args.get("query")
    query = redact_sensitive_text(query, force=True) if isinstance(query, str) else ""   # it goes to Ollama and the store
    if not query.strip():
        return {"error": "query is empty"}
    wanted = write_path.load()["recall"]["results"]
    outcome = _searched(query, machine=machine, session_id=session_id, turn=turn, embedder=embedder,
                        judge_at=judge_at, connect=connect, wanted=wanted)
    return {"results": outcome["results"][:wanted]} if "results" in outcome else outcome


def recall_notes(query, *, machine, session_id, turn, embedder, judge_at, connect):
    """prefetch()'s notes for a message (3.5): searched under the recall deadline, at most one digest, cut to the
    results per message; a failure or a missed deadline is reported in the turn with its code."""
    query = redact_sensitive_text(str(query or ""), force=True)   # Hermes redacts too; the build's is forced
    if not query.strip():
        return ""   # 3.5 point 1: only an empty message is skipped
    limits = write_path.load()["recall"]
    kind = recall.question_kind(query)   # the recall gate's patterns (3.5 points 3-4)
    # Past work widens the search; open work keeps the normal search and adds open items on top (#158, A).
    wanted = limits["results_past_work"] if kind == "past_work" else limits["results"]
    box = {}

    # The recall thread is a Hermes entry point's work too: the boundary wrapper records a crash in it as
    # unhandled (CONTRIBUTING.md rule C2), with its session, and the turn is told below.
    @failures.boundary(lambda: machine, connect, failures.Returns(lambda exc: None))
    def run(session_id):
        box["outcome"] = _searched(query, machine=machine, session_id=session_id, turn=turn, embedder=embedder,
                                   judge_at=judge_at, connect=connect, wanted=wanted, deadline=limits["deadline"],
                                   open_work=kind == "open_work")

    # Hermes's own thread helper, so the thread keeps the session's context: its directory, and so its project.
    worker = spawn_context_thread(run, name="custom_build-recall", kwargs={"session_id": session_id})
    worker.start()
    worker.join(limits["deadline"])
    if worker.is_alive():   # the store cuts the search off at the same deadline; the turn does not wait for it
        record = spawn_context_thread(_with_store, name="custom_build-recall-timeout", args=(
            machine, session_id, connect, lambda conn: failures.record(
                conn, "recall.timeout", "failed", source=machine, project=project.key(resolve_agent_cwd()),
                session_id=session_id, turn=turn, reason=f"no answer within {limits['deadline']} seconds")))
        record.start()   # off the turn's path: opening a connection to record it must not pass Hermes's 8 seconds
        return recall.failed("recall.timeout")
    if "outcome" not in box:   # it raised, and the boundary wrapper recorded it
        return recall.failed("unhandled")
    outcome = box["outcome"]
    if "error" in outcome:
        return recall.failed(outcome["error"])
    results = recall.capped(outcome["results"], wanted=wanted, digests=limits["digests_per_message"])
    if kind == "open_work":   # first, within the past-work count, so a misfired pattern only adds notes
        items = recall.open_items(outcome["open"])[:max(limits["results_past_work"] - len(results), 0)]
        return recall.notes(items + results)
    return recall.notes(results)


def action_config():
    """The action query's switch, values, action list and wording (3.5 point 2, 3.6)."""
    return tomllib.loads(ACTION_QUERY.read_text(encoding="utf-8"))


def action_notes(message, history, *, machine, session_id, embedder, judge_at, connect):
    """The action query's notes for a turn (3.5 point 2): "" when it is switched off, skipped or finds nothing;
    a failure or a missed deadline is reported in the turn with its code, as prefetch() reports its own."""
    config = action_config()
    message = message if isinstance(message, str) else ""
    if not config["on"] or not message.strip():
        return ""
    # Both go to the judge: the build's own redaction, forced, as for every text that leaves the host (L4, 3.7).
    state = {"message": redact_sensitive_text(message, force=True),
             "previous_assistant_reply_end": redact_sensitive_text(
                 _previous_reply_end(history, config["previous_reply_chars"]), force=True)}
    started, box = time.monotonic(), {}

    @failures.boundary(lambda: machine, connect, failures.Returns(lambda exc: None))
    def run(session_id):
        box["outcome"] = _action(state, config, started=started, machine=machine, session_id=session_id,
                                 embedder=embedder, judge_at=judge_at, connect=connect)

    worker = spawn_context_thread(run, name="custom_build-action-query", kwargs={"session_id": session_id})
    worker.start()
    worker.join(config["deadline"])
    if worker.is_alive():   # as in recall_notes: the turn does not wait, and the record is written off its path
        record = spawn_context_thread(_with_store, name="custom_build-action-timeout", args=(
            machine, session_id, connect, lambda conn: failures.record(
                conn, "recall.timeout", "failed", source=machine, project=project.key(resolve_agent_cwd()),
                session_id=session_id, reason=f"action query: no answer within {config['deadline']} seconds")))
        record.start()
        return recall.failed("recall.timeout")
    if "outcome" not in box:   # it raised, and the boundary wrapper recorded it
        return recall.failed("unhandled")
    outcome = box["outcome"]
    if "error" in outcome:
        return recall.failed(outcome["error"])
    results = recall.capped(outcome["results"], wanted=config["results"],
                            digests=write_path.load()["recall"]["digests_per_message"])
    return recall.notes(results, header=config["header"])


def _previous_reply_end(history, chars):
    """The end of the assistant's last reply in the conversation so far, as the routing test sent it (#15)."""
    for message in reversed(history or []):
        if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), str) \
                and message["content"]:
            return message["content"][-chars:]
    return ""


def _action(state, config, *, started, machine, session_id, embedder, judge_at, connect):
    """Route the message, then search the facts store with the picked action's description, as {"results": [...]}
    or {"error": code}, recorded; a message that asks for no work is recorded as skipped (3.5 point 2)."""
    fields = {"source": machine, "project": project.key(resolve_agent_cwd()), "session_id": session_id}
    try:
        pick, asks = judge.route_answers(state, config["actions"], config["questions"], url=judge_at.url,
                                         key=judge_at.key, model=judge_at.model, timeout=judge.DEADLINE)
    except judge.JudgeTimeout as exc:
        reason = f"action query: {exc}"
        _with_store(machine, session_id, connect, lambda conn: failures.record(
            conn, "judge.timeout", "failed", reason=reason, **fields))
        return {"error": "judge.timeout"}
    except judge.JudgeError as exc:
        reason = f"action query: {exc}"
        _with_store(machine, session_id, connect, lambda conn: failures.record(
            conn, "judge.error", "failed", reason=reason, **fields))
        return {"error": "judge.error"}
    if asks < config["asks_for_work_floor"]:   # a clear no; any pick at all is still used (#26)
        _with_store(machine, session_id, connect, lambda conn: failures.record(
            conn, "action.skipped", "ok", reason=f"asks for work at {asks}", detail={"pick": pick}, **fields))
        return {"results": []}
    # What is left of the action query's deadline; at least a millisecond, since 0 would lift the store's timeout.
    left = max(config["deadline"] - (time.monotonic() - started), 0.001)
    return _searched(config["actions"][pick], machine=machine, session_id=session_id, turn=None, embedder=embedder,
                     judge_at=judge_at, connect=connect, wanted=config["results"], deadline=left)


def _searched(query, *, machine, session_id, turn, embedder, judge_at, connect, wanted, deadline=None,
              open_work=False):
    """The candidate pool for `wanted` results, reranked or in fused order, as {"results": [...]}, or
    {"error": code}, recorded. With a `deadline`, the store cancels any statement that runs past it, so a
    stalled store releases the search, and the rerank gets only the time left of it (#165)."""
    started = time.monotonic()
    key = project.key(resolve_agent_cwd())
    fields = {"source": machine, "project": key, "session_id": session_id, "turn": turn}

    def work(conn):
        if deadline is not None:   # an int in milliseconds, so it is safe to write into the statement
            conn.execute(f"set statement_timeout = {int(deadline * 1000)}")
        try:
            results = search.candidates(conn, embedder, query, key, wanted=wanted, timeout=TIMEOUT)
            # The judge's own deadline, or what is left of the recall's, less a margin to deliver the notes.
            left = judge.DEADLINE if deadline is None else min(
                judge.DEADLINE, deadline - (time.monotonic() - started) - RERANK_MARGIN)
            try:
                if left <= 0:
                    raise judge.JudgeTimeout("no time left in the recall deadline for the rerank")
                results = search.reranked(results, query, judge_at, timeout=left)
            except (judge.JudgeError, judge.JudgeTimeout) as exc:   # 3.4 step 4: fused order, recorded as degraded
                failures.record(conn, "rerank.fallback", "degraded", reason=str(exc), **fields)
            if open_work:   # the newest digests too, by recency, not similarity (3.5 point 4)
                return {"results": results,
                        "open": search.newest_digests(conn, key, write_path.load()["recall"]["open_work_digests"])}
            return {"results": results}
        except embedding.EmbedUnavailable as exc:
            failures.record(conn, "embed.unavailable", "failed", reason=str(exc), **fields)
            return {"error": "embed.unavailable"}
        except embedding.ModelMismatch as exc:
            failures.record(conn, "embed.model_mismatch", "failed", reason=str(exc), **fields)
            return {"error": "embed.model_mismatch"}
        except psycopg.errors.QueryCanceled:
            # The store cancelled a recall statement at the deadline: prefetch has already told the turn and
            # recorded recall.timeout. It is an OperationalError too, but the store was reached (#158 review).
            return {"error": "recall.timeout"}
        except psycopg.OperationalError as exc:
            failures.record(conn, "db.unreachable", "failed", reason=str(exc), **fields)
            return {"error": "db.unreachable"}
        except psycopg.Error as exc:   # a store without migration 0005, for one
            failures.record(conn, "db.query", "failed", reason=str(exc), **fields)
            return {"error": "db.query"}

    return _with_store(machine, session_id, connect, work)


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
    key = None   # built-in memory is one file per profile, so its facts belong to no project (#142)
    fields = {"source": machine, "project": key, "session_id": session_id, "turn": turn}

    def work(conn):
        if action == "remove":
            # Hermes sends a remove with empty content; the removed entry is in previous_content.
            removed = redact_sensitive_text(str(metadata.get("previous_content", "")), force=True)
            _update(conn, fields, store.retire, key, removed, "removed from built-in memory")
            return
        fact = store.Fact(body=body, basis="user_stated", project=key, source=machine, session_id=session_id,
                          source_turn=turn)
        outcome = _save(conn, fact, embedder=embedder, judge_at=judge_at)
        new = outcome.get("stored") or outcome.get("already_stored")
        if action == "replace" and new and metadata.get("previous_content"):
            old = redact_sensitive_text(str(metadata["previous_content"]), force=True)
            _update(conn, fields, store.supersede, key, old, new)

    _with_store(machine, session_id, connect, work)


def extract(user_content, messages, *, machine, session_id, turn, embedder, judge_at, connect):
    """Store the facts the extraction model proposes from a finished turn, each through the judge's gate
    (3.3 step 3) and then the store function, with origin `extracted` (#148)."""
    turn_source = extraction.source(user_content, messages)
    fields = {"source": machine, "project": project.key(resolve_agent_cwd()), "session_id": session_id, "turn": turn}
    failure, proposals = None, []
    try:   # before the store is opened: the call can take as long as its auxiliary timeout
        proposals = extraction.propose(turn_source)
    except (extraction.ExtractRefused, extraction.ExtractError) as exc:
        failure = exc
    if not failure and not proposals:
        return

    def work(conn):
        if isinstance(failure, extraction.ExtractRefused):
            failures.record(conn, "extract.refused", "failed", reason=str(failure), **fields)
            return
        if failure:
            failures.record(conn, "extract.error", "failed", reason=str(failure), **fields)
            return
        for body, basis in proposals:
            fact = store.Fact(body=redact_sensitive_text(body, force=True), basis=basis, project=fields["project"],
                              source=machine, session_id=session_id, source_turn=turn, origin="extracted",
                              gate=turn_source)
            _save(conn, fact, embedder=embedder, judge_at=judge_at)

    _with_store(machine, session_id, connect, work)


def write_digest(messages, *, machine, session_id, turn, embedder, judge_at, connect):
    """Write the session's digest (3.3, #155): the digest model, then the judge's gate, then the store
    function, as kind and origin `digest`, replacing the session's earlier digest (#156). A session gets one when
    it is long, or when it already has one: after an in-place compression its transcript is short again."""
    if not session_id:
        return
    has = _with_store(machine, session_id, connect, lambda conn: store.live_digest(conn, session_id) is not None)
    if not isinstance(has, bool):
        return   # the store could not be reached (recorded)
    if not has and not digest.is_long(messages):
        return
    pending = {"session_id": session_id, "project": project.key(resolve_agent_cwd()), "source": machine,
               "turn": turn, "written_at": datetime.datetime.now(datetime.UTC).isoformat(),   # orders them (#156)
               "transcript": digest.transcript(messages)}   # redacted
    # Queued before the call (#156 item 2): a process stopped during it leaves the digest for the next start.
    row_id = queue.add_digest(pending)
    _digest_from(pending, embedder=embedder, judge_at=judge_at, connect=connect)
    queue.digest_done(row_id)


def _digest_from(pending, *, embedder, judge_at, connect):
    """The digest model on a queued transcript, then the judge's gate and the store; a failure recorded."""
    machine, session_id = pending["source"], pending["session_id"]
    fields = {"source": machine, "project": pending["project"], "session_id": session_id, "turn": pending["turn"]}
    failure, body = None, None
    try:   # with no connection open: the call can take as long as its auxiliary timeout
        body = _redact_forced(digest.write(pending["transcript"]))
    except (extraction.ExtractRefused, extraction.ExtractError) as exc:
        failure = exc

    def work(conn):
        if isinstance(failure, extraction.ExtractRefused):
            failures.record(conn, "digest.refused", "failed", reason=str(failure), **fields)
            return
        if failure:
            failures.record(conn, "digest.error", "failed", reason=str(failure), **fields)
            return
        # The weakest basis of its inputs: the transcript holds the agent's replies (3.3 rule 6).
        fact = store.Fact(body=body, basis="assistant_inferred", project=fields["project"], source=machine,
                          session_id=session_id, source_turn=pending["turn"], origin="digest", kind="digest",
                          written_at=pending["written_at"])
        _save(conn, fact, embedder=embedder, judge_at=judge_at)

    _with_store(machine, session_id, connect, work)


def write_orphaned_digests(*, machine, embedder, judge_at, connect):
    """Each digest a stopped process queued and never finished (#156 item 2), claimed by this process so that
    only one start writes it, written now, then taken off the queue whatever happened, so a digest whose call
    fails again is recorded once, not at every start."""
    for row_id, owner, pending in queue.orphaned_digests():
        if not queue.claim(row_id, owner):   # another start took it first
            continue
        try:
            _digest_from(pending, embedder=embedder, judge_at=judge_at, connect=connect)
        finally:
            queue.digest_done(row_id)


def start_orphaned_digests(*, machine, embedder, judge_at, connect):
    """write_orphaned_digests() on a thread of its own, so a model call never holds the provider's start; None
    when nothing waits. A crash in it is recorded as unhandled (CONTRIBUTING.md rule C2)."""
    if not queue.orphaned_digests():
        return None
    run = failures.boundary(lambda: machine, connect, failures.Returns(lambda exc: None))(write_orphaned_digests)
    worker = spawn_context_thread(run, name="custom_build-pending-digests", kwargs={
        "machine": machine, "embedder": embedder, "judge_at": judge_at, "connect": connect})
    worker.start()
    return worker


def _redact_forced(text):
    return redact_sensitive_text(text, force=True)


def _update(conn, fields, change, *args):
    """change(conn, *args) in a savepoint, a refused or failed update recorded as db.query or db.unreachable."""
    try:
        with conn.transaction():
            change(conn, *args)
    except psycopg.OperationalError as exc:
        failures.record(conn, "db.unreachable", "failed", reason=str(exc), **fields)
    except psycopg.Error as exc:   # a store without migration 0003 refuses the retire
        failures.record(conn, "db.query", "failed", reason=str(exc), **fields)


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
        if fact.gate is not None or fact.kind == "digest":   # gated: unless the same text is already live
            if existing := store.identical(conn, fact.project, fact.body):
                return {"already_stored": existing}
            if not _kept(fact, judge_at):
                return _dropped(conn, fact, "the judge did not keep it", fields)
        outcome = store.store(conn, fact, embedder=embedder, judge=judge_at, timeout=TIMEOUT)
        if "dropped" in outcome:   # a later digest of the session is live, or another committed first (#156)
            return _dropped(conn, fact, outcome["dropped"], fields)
        return outcome
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


def _dropped(conn, fact, reason, fields):
    """{"dropped": reason}; a digest's drop is recorded, so its retries at the session's later ends are seen
    (#156 item 3). A fact's drop by the gate is the gate's ordinary work and is not."""
    if fact.kind == "digest":
        failures.record(conn, "digest.dropped", "ok", reason=reason, **fields)
    return {"dropped": reason}


def _kept(fact, judge_at):
    """The judge's gate: a digest on worth keeping; an extracted fact on that and on its source turn's support.
    Raises the judge's errors."""
    reach = {"url": judge_at.url, "key": judge_at.key, "model": judge_at.model, "timeout": judge.DEADLINE}
    if fact.kind == "digest":
        return judge.keeps_digest(judge.digest_answers(fact.body, **reach))
    return judge.keeps(judge.keep_answers(fact.body, fact.gate, **reach))


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
        reason = f"dropped on retry: {outcome['dropped']}" if "dropped" in outcome else "resolved on retry"
        failures.record(conn, "store.gate_error", "degraded", source=fact.source, project=fact.project,
                        session_id=fact.session_id, turn=fact.source_turn, reason=reason,
                        detail={"attempts": attempts + 1})
