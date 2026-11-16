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
    "guard.bypassed": "A local session ran in safe mode or with the profile's settings ignored, or its launch policy cannot be read",
    "write_gate.would_ask": "Not a failure: in log mode, the write gate saw a call on its list that on would send for approval",
    "write_gate.would_block": "Not a failure: in log mode, the write gate saw a call on its list that on would block outright",
    "verify_gate.would_block": "Not a failure: in log mode, a call would change a listed file or setting the session never read",
    "verify_gate.would_withhold": "Not a failure: in log mode, a reply stated a listed file's contents the session never read",
    "config.unsafe": "The startup check found a Hermes setting with a value the build does not support",
    "config.missing": "A setting the build needs is missing or invalid",
    "redact.failed": "The redactor raised; the text is not stored or sent",
    "memory.write_rejected": "Hermes refused a built-in memory write",
    "events.write_failed": "An events row could not be written; logged locally at ERROR",
    "job.failed": "A scheduled job failed or the startup check crashed",
    "job.missed": "A scheduled job did not run",
    "job.no_output": "A scheduled job took input and produced nothing",
    "job.succeeded": "Not a failure: a scheduled job's run finished, naming the job and, where it has them, its counts in and out",
    "host.missing": "An agent host on the job-health check's list wrote no heartbeat in the last 24 hours",
    "host.heartbeat": "Not a failure: an agent host's heartbeat job ran",
    "unhandled": "Anything not in the catalog, outside a guard, recorded with its traceback",
}

RESULTS = ("ok", "empty", "failed", "degraded")
REASON_LIMIT = 500
# With settings.CONNECT_TIMEOUT, inside the 10 seconds a guard has for its whole run (ARCHITECTURE.md 5.5).
# Set per transaction, because Supabase's pooler drops a timeout given at connect (supavisor#1272).
RECORD_TIMEOUT = "3s"
# Postgres's and Supabase's pooler's texts for a refused login; psycopg gives no SQL state on connect.
# shortcut: matches English server messages only, extend if a server runs another lc_messages.
REFUSED_LOGIN = ("password authentication failed", "no pg_hba.conf entry", "pg_hba.conf rejects",
                 "is not permitted to log in", "Tenant or user not found")   # reasons are truncated, never full content (rule L5)


def refused_login(exc):
    """True when a failed connection's server refused the login, not when it could not be reached."""
    return any(text in str(exc) for text in REFUSED_LOGIN)


class UnknownFailureCode(ValueError):
    """A code that is not in the catalog: a bug in the caller (rule F6)."""


def record(conn, code, result, *, source, reason=None, project=None, session_id=None, turn=None, detail=None):
    """Log one JSON line and write the matching memory.events row; return True when the row was written.

    Raises UnknownFailureCode for a code outside the catalog and ValueError for an unknown result,
    before anything is logged or written. When the row cannot be written, because `conn` is None (no
    store is configured) or the store failed or refused the insert (any psycopg.Error), the original
    line is already logged, the loss is logged at ERROR as events.write_failed (rule F5), and False is
    returned, for a caller whose row is its whole job.
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
        return False
    try:
        own = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE   # no caller's transaction is open
        with conn.transaction():
            # A stalled store then gives up, a lost write like any other, instead of holding a hook past
            # Hermes's deadline. Set inside a caller's transaction, the bound would outlive this record.
            if own:
                conn.execute(f"set local statement_timeout = '{RECORD_TIMEOUT}'")
            conn.execute(
                "insert into memory.events (source, project, session_id, turn, code, result, reason, detail)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (source, project, session_id, turn, code, result, fields["reason"], Jsonb(fields["detail"])),
            )
    except psycopg.Error as exc:
        _log_lost_write(fields, str(exc))
        return False
    return True


def _log_lost_write(fields, reason):
    logger.error(json.dumps({
        **fields, "code": "events.write_failed", "result": "failed", "reason": reason[:REASON_LIMIT],
        "detail": {"lost_code": fields["code"]},
    }))


def _record_lost_connection(exc, source):
    """Log a connection that could not be opened: `db.auth` when the server refused the login."""
    if refused_login(exc):
        record(None, "db.auth", "failed", source=source, reason=str(exc))
    else:
        record(None, "db.unreachable", "failed", source=source, reason=str(exc))


@contextlib.contextmanager
def store(source, connect):
    """Give a new connection from `connect()` to record on, and close it on leaving.

    Gives None when `connect()` gives None (no store is configured) or the connection fails to open,
    which is logged as `db.auth` (the server refused the login) or `db.unreachable`, as in `run_job`.
    `record()` then logs what it is given with events.write_failed.
    """
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        _record_lost_connection(exc, source)
        conn = None
    with conn or contextlib.nullcontext():
        yield conn


def run_job(name, job, *, connect, source):
    """Run job(conn) on a fresh connection and return what it returns; on any exception record it and exit non-zero.

    The crash is recorded as `unhandled`, with the traceback in the log line and the exception's type
    and message as the reason, then as `job.failed` naming the job. SystemExit(1) follows, so the
    scheduler sees the failure too. When the connection cannot be opened, the job does not run: the
    failure is logged as `db.auth` (the server refused the login) or `db.unreachable`, with
    `events.write_failed`, since there is no store to write to, and SystemExit(1) follows. Any other
    exception from `connect` is a bug, logged as `unhandled` and `job.failed` the same way.
    """
    try:
        conn = connect()
    except psycopg.OperationalError as exc:
        _record_lost_connection(exc, source)
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.exception("job %s could not open its connection", name)
        _record_job_crash(None, name, source, exc)
        raise SystemExit(1) from exc
    with conn:
        try:
            return job(conn)
        except Exception as exc:
            logger.exception("job %s crashed", name)
            if not conn.closed:   # a lost connection reads as closed; record() then logs events.write_failed
                conn.rollback()   # leave the job's open or aborted transaction, so the records below commit
            _record_job_crash(conn, name, source, exc)
            raise SystemExit(1) from exc


def exit_note(stop):
    """Name what run_job recorded before it raised `stop`, and where, for a caller's stderr (#64)."""
    if isinstance(stop.__cause__, psycopg.OperationalError):
        code = "db.auth" if refused_login(stop.__cause__) else "db.unreachable"
        return (f"{code}; not recorded in events: events.write_failed (no connection to the store); "
                "the details are in Hermes's errors.log")
    return "unhandled and job.failed; the traceback is in Hermes's errors.log"


def _record_job_crash(conn, name, source, exc):
    reason = "".join(traceback.format_exception_only(exc)).strip()
    record(conn, "unhandled", "failed", source=source, reason=reason)
    record(conn, "job.failed", "failed", source=source, reason=reason, detail={"job": name})


# Pass as `on_crash` for a provider method other than prefetch (ARCHITECTURE.md section 2): the crash
# is recorded as `unhandled`, not `guard.crash`, and re-raised once recorded.
PROVIDER = object()


def boundary(source, connect, on_crash):
    """Wrap a guard that Hermes calls (ARCHITECTURE.md section 2).

    On any exception the guard raises, log it with its traceback, record `guard.crash` with the
    exception's type and message as the reason, and return `on_crash(exc)`: for a guard on
    `pre_llm_call`, the block that ends the turn, which Hermes would otherwise let go ahead; for a
    guard in log mode, None, so the call goes ahead. `source()` names the machine and `connect()`
    opens a connection for the record, or gives None when there is no store. A connection that fails
    to open is logged as `db.auth` or `db.unreachable`, as in `run_job`; with no connection, or an
    insert the store refuses, the record is logged with events.write_failed, and `on_crash(exc)` is
    still returned. The other rows of section 2's table arrive with their entry points.
    """
    def wrap(guard):
        @functools.wraps(guard)
        def call(*args, **kwargs):
            try:
                return guard(*args, **kwargs)
            except Exception as exc:
                logger.exception("guard %s crashed", guard.__qualname__)
                reason = "".join(traceback.format_exception_only(exc)).strip()
                machine, conn = "unknown", None
                try:
                    machine = source()
                    conn = connect()
                except psycopg.OperationalError as lost:
                    _record_lost_connection(lost, machine)
                except Exception:   # the turn's block must not depend on reaching the store (rule F5)
                    logger.exception("no connection to record guard.crash")
                if on_crash is PROVIDER:
                    with conn or contextlib.nullcontext():
                        record(conn, "unhandled", "failed", source=machine, reason=reason, session_id=kwargs.get("session_id"))
                    raise
                with conn or contextlib.nullcontext():   # closes the connection opened for this record
                    record(conn, "guard.crash", "failed", source=machine, reason=reason,
                           session_id=kwargs.get("session_id") or None)   # Hermes passes "" for no session
                return on_crash(exc)
        return call
    return wrap
