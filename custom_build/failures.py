"""The failure module: the catalog, the one recording helper and the job wrapper.

ARCHITECTURE.md section 6.1; CONTRIBUTING.md rules F1-F6 and L3. Every failure is logged as one JSON
line and written to memory.events with the same fields, through `record`. `run_job` and `boundary` are
the only places allowed to catch everything (rule C2).
"""

import contextlib
import functools
import json
import logging
import traceback

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

CATALOG = {
    "embed.unavailable": "The embedding service cannot be reached",
    "embed.model_mismatch": "This machine's embedding build differs from the one recorded in the store",
    "db.unreachable": "The memory store cannot be reached",
    "db.auth": "The memory store refuses the role",
    "db.query": "The memory store rejects a query",
    "recall.timeout": "Recall misses its deadline",
    "judge.timeout": "The judge model times out",
    "judge.error": "The judge model errors",
    "rerank.fallback": "The judge could not rerank, and the pool kept its fused order (degraded)",
    "store.gate_error": "The judge failed during a write; the fact is queued",
    "guard.crash": "A guard raised during a turn",
    "guard.not_loaded": "The startup check found a guard's hooks or the provider not loaded",
    "config.unsafe": "The startup check found a Hermes setting with a value the build does not support",
    "config.missing": "A setting the build needs is missing or invalid",
    "redact.failed": "The redactor raised; the text is not stored or sent",
    "memory.write_rejected": "Hermes refused a built-in memory write",
    "events.write_failed": "An events row could not be written; logged locally at ERROR",
    "job.failed": "A scheduled job failed or the startup check crashed",
    "job.missed": "A scheduled job did not run",
    "job.no_output": "A scheduled job took input and produced nothing",
    "unhandled": "Anything not in the catalog, outside a guard, recorded with its traceback",
}

RESULTS = ("ok", "empty", "failed", "degraded")
REASON_LIMIT = 500
# Postgres's and Supabase's pooler's texts for a refused login; psycopg gives no SQL state on connect.
# shortcut: matches English server messages only, extend if a server runs another lc_messages.
REFUSED_LOGIN = ("password authentication failed", "no pg_hba.conf entry", "pg_hba.conf rejects",
                 "is not permitted to log in", "Tenant or user not found")   # reasons are truncated, never full content (rule L5)


class UnknownFailureCode(ValueError):
    """A code that is not in the catalog: a bug in the caller (rule F6)."""


def record(conn, code, result, *, source, reason=None, project=None, session_id=None, turn=None, detail=None):
    """Log one JSON line and write the matching memory.events row.

    Raises UnknownFailureCode for a code outside the catalog and ValueError for an unknown result,
    before anything is logged or written. When the row cannot be written, because `conn` is None (no
    store is configured) or the connection failed (psycopg.OperationalError), the original line is
    already logged, and the loss is logged at ERROR as events.write_failed (rule F5); nothing is
    returned either way.
    """
    if code not in CATALOG:
        raise UnknownFailureCode(code)
    if result not in RESULTS:
        raise ValueError(f"result must be one of {RESULTS}, not {result!r}")
    fields = {
        "code": code, "source": source, "project": project, "session_id": session_id, "turn": turn,
        "result": result, "reason": reason[:REASON_LIMIT] if reason else reason, "detail": detail or {},
    }
    logger.log(logging.WARNING if result in ("failed", "degraded") else logging.INFO, json.dumps(fields))
    if conn is None:
        _log_lost_write(fields, "no connection to the store")
        return
    try:
        with conn.transaction():
            conn.execute(
                "insert into memory.events (source, project, session_id, turn, code, result, reason, detail)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (source, project, session_id, turn, code, result, fields["reason"], Jsonb(fields["detail"])),
            )
    except psycopg.OperationalError as exc:
        _log_lost_write(fields, str(exc))


def _log_lost_write(fields, reason):
    logger.error(json.dumps({
        **fields, "code": "events.write_failed", "result": "failed", "reason": reason[:REASON_LIMIT],
        "detail": {"lost_code": fields["code"]},
    }))


def run_job(name, job, *, connect, source):
    """Run job(conn) on a fresh connection; on any exception record it and exit non-zero.

    The crash is recorded as `unhandled`, with the traceback in the log line and the exception's type
    and message as the reason, then as `job.failed` naming the job. SystemExit(1) follows, so the
    scheduler sees the failure too. When the connection cannot be opened, the job does not run: the
    failure is logged as `db.auth` (the server refused the login) or `db.unreachable`, with
    `events.write_failed`, since there is no store to write to, and SystemExit(1) follows.
    """
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        if any(text in str(exc) for text in REFUSED_LOGIN):
            record(None, "db.auth", "failed", source=source, reason=str(exc))
        else:
            record(None, "db.unreachable", "failed", source=source, reason=str(exc))
        raise SystemExit(1) from exc
    with conn:
        try:
            job(conn)
        except Exception as exc:
            logger.exception("job %s crashed", name)
            if not conn.closed:   # a lost connection reads as closed; record() then logs events.write_failed
                conn.rollback()   # leave the job's open or aborted transaction, so the records below commit
            reason = "".join(traceback.format_exception_only(exc)).strip()
            record(conn, "unhandled", "failed", source=source, reason=reason)
            record(conn, "job.failed", "failed", source=source, reason=reason, detail={"job": name})
            raise SystemExit(1) from exc


def boundary(store, on_crash):
    """Wrap a guard that Hermes calls on `pre_llm_call` (ARCHITECTURE.md section 2).

    On any exception the guard raises, log it with its traceback, record `guard.crash` with the
    exception as the reason, and return `on_crash(exc)`, the block that ends the turn: Hermes would
    otherwise log the error and let the turn go ahead. `store()` gives the machine's name and a new
    connection, or None for the connection when there is no store to write to; when it gives None or
    raises, the record is logged with events.write_failed instead, and the block is still returned.
    Only this row of section 2's table is built; the others arrive with their entry points.
    """
    def wrap(guard):
        @functools.wraps(guard)
        def call(*args, **kwargs):
            try:
                return guard(*args, **kwargs)
            except Exception as exc:
                logger.exception("guard %s crashed", guard.__qualname__)
                reason = "".join(traceback.format_exception_only(exc)).strip()
                try:
                    source, conn = store()
                except Exception:   # the turn's block must not depend on reaching the store (rule F5)
                    logger.exception("no connection to record guard.crash")
                    source, conn = "unknown", None
                with conn or contextlib.nullcontext():   # closes the connection opened for this record
                    record(conn, "guard.crash", "failed", source=source, reason=reason, session_id=kwargs.get("session_id"))
                return on_crash(exc)
        return call
    return wrap
