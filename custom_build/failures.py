"""The failure module: the catalog, the one recording helper and the job wrapper.

ARCHITECTURE.md section 6.1; CONTRIBUTING.md rules F1-F6 and L3. Every failure is logged as one JSON
line and written to memory.events with the same fields, through `record`. `run_job` is one of the two
places allowed to catch everything (rule C2); the boundary wrapper arrives with the plugin's first
entry point.
"""

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
    "guard.not_loaded": "The startup check found the plugin, a guard's hooks or the provider not loaded",
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
REASON_LIMIT = 500   # reasons are truncated, never full content (rule L5)


class UnknownFailureCode(ValueError):
    """A code that is not in the catalog: a bug in the caller (rule F6)."""


def record(conn, code, result, *, source, reason=None, project=None, session_id=None, turn=None, detail=None):
    """Log one JSON line and write the matching memory.events row.

    Raises UnknownFailureCode for a code outside the catalog and ValueError for an unknown result,
    before anything is logged or written. When the row cannot be written because the connection
    failed (psycopg.OperationalError), the original line is already logged, and the loss is logged
    at ERROR as events.write_failed (rule F5); nothing is returned either way.
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
    try:
        with conn.transaction():
            conn.execute(
                "insert into memory.events (source, project, session_id, turn, code, result, reason, detail)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (source, project, session_id, turn, code, result, fields["reason"], Jsonb(fields["detail"])),
            )
    except psycopg.OperationalError as exc:
        logger.error(json.dumps({
            "code": "events.write_failed", "source": source, "project": project, "session_id": session_id,
            "turn": turn, "result": "failed", "reason": str(exc)[:REASON_LIMIT], "detail": {"lost_code": code},
        }))


def run_job(name, job, *, connect, source):
    """Run job(conn) on a fresh connection; on any exception record it and exit non-zero.

    The crash is recorded as `unhandled`, with the traceback in the log line and the exception's type
    and message as the reason, then as `job.failed` naming the job. SystemExit(1) follows, so the
    scheduler sees the failure too.
    """
    with connect() as conn:
        try:
            job(conn)
        except Exception as exc:
            logger.exception("job %s crashed", name)
            reason = "".join(traceback.format_exception_only(exc)).strip()
            record(conn, "unhandled", "failed", source=source, reason=reason)
            record(conn, "job.failed", "failed", source=source, reason=reason, detail={"job": name})
            raise SystemExit(1) from exc
