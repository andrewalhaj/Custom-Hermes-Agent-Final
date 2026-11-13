"""Prove the failure module (issue #33, ARCHITECTURE.md section 6.1, CONTRIBUTING.md F1-F6 and L3).

The database tests use the `db` fixture (conftest.py) and write through a login in memory_agent.
"""

import ast
import json
import logging
import pathlib

import psycopg
import pytest

from custom_build import failures

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _rows(dsn):
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "select source, project, session_id, turn, code, result, reason, detail from memory.events order by id"
        ).fetchall()


def _json_lines(caplog):
    return [(r.levelno, json.loads(r.getMessage())) for r in caplog.records if r.name == failures.__name__]


def test_record_writes_one_row_and_logs_the_same_fields(db, caplog):
    caplog.set_level(logging.INFO, logger=failures.__name__)
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        failures.record(conn, "db.query", "failed", source="host-a", project="p1", session_id="s1", turn=3,
                        reason="rejected", detail={"rows": 2})
    assert _rows(db["agent"]) == [("host-a", "p1", "s1", 3, "db.query", "failed", "rejected", {"rows": 2})]
    assert _json_lines(caplog) == [(logging.WARNING, {
        "code": "db.query", "source": "host-a", "project": "p1", "session_id": "s1", "turn": 3,
        "result": "failed", "reason": "rejected", "detail": {"rows": 2},
    })]


def test_an_ok_event_is_logged_at_info(db, caplog):
    caplog.set_level(logging.INFO, logger=failures.__name__)
    with psycopg.connect(db["agent"], autocommit=True) as conn:
        failures.record(conn, "job.failed", "ok", source="host-a")
    assert [level for level, _ in _json_lines(caplog)] == [logging.INFO]


def test_an_unknown_code_raises_and_writes_nothing(db):
    with psycopg.connect(db["agent"], autocommit=True) as conn, pytest.raises(failures.UnknownFailureCode):
        failures.record(conn, "made.up", "failed", source="host-a")
    assert _rows(db["agent"]) == []


def test_a_lost_events_write_is_logged_as_events_write_failed(db, caplog):
    conn = psycopg.connect(db["agent"], autocommit=True)
    conn.close()
    failures.record(conn, "db.unreachable", "failed", source="host-a", reason="refused")
    lines = _json_lines(caplog)
    assert [(level, line["code"]) for level, line in lines] == [
        (logging.WARNING, "db.unreachable"),
        (logging.ERROR, "events.write_failed"),
    ]


def _crashing_job(conn):
    raise RuntimeError("boom")


def test_job_wrapper_records_a_crash_and_exits_non_zero(db, caplog):
    with pytest.raises(SystemExit) as exit_info:
        failures.run_job("nightly", _crashing_job, source="host-a",
                         connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    assert exit_info.value.code != 0
    rows = _rows(db["agent"])
    assert [(r[4], r[5]) for r in rows] == [("unhandled", "failed"), ("job.failed", "failed")]
    assert "RuntimeError" in rows[0][6]
    assert rows[1][7] == {"job": "nightly"}
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records)


def _query_then_crash(conn):
    conn.execute("select 1")
    raise RuntimeError("boom")


def _sql_error(conn):
    conn.execute("select 1/0")


@pytest.mark.parametrize("job", [_query_then_crash, _sql_error])
def test_job_wrapper_records_a_crash_inside_an_open_transaction(db, job):
    # psycopg's default connection opens a transaction on the first query; the crash leaves it open or aborted
    with pytest.raises(SystemExit) as exit_info:
        failures.run_job("nightly", job, source="host-a", connect=lambda: psycopg.connect(db["agent"]))
    assert exit_info.value.code != 0
    assert [(r[4], r[5]) for r in _rows(db["agent"])] == [("unhandled", "failed"), ("job.failed", "failed")]


def test_job_wrapper_returns_quietly_when_the_job_succeeds(db):
    ran = []
    failures.run_job("nightly", lambda conn: ran.append(True), source="host-a",
                     connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    assert ran == [True]
    assert _rows(db["agent"]) == []


# The catalog test (F6): every literal code passed to record() anywhere in the package is in the catalog.

def _codes_in(source):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "record":
            arg = node.args[1] if len(node.args) > 1 else None
            yield arg.value if isinstance(arg, ast.Constant) else None


def test_the_catalog_scan_catches_an_unknown_code():
    assert list(_codes_in("record(conn, 'made.up', 'failed')")) == ["made.up"]
    assert list(_codes_in("failures.record(conn, code, 'failed')")) == [None]


def test_every_code_recorded_in_the_package_is_in_the_catalog():
    found = [(path.name, code) for path in (ROOT / "custom_build").rglob("*.py")
             for code in _codes_in(path.read_text(encoding="utf-8"))]
    assert found, "the scan found no record() calls, so it proves nothing"
    assert [item for item in found if item[1] not in failures.CATALOG] == []


def test_the_catalog_holds_the_architecture_codes():
    assert set(failures.CATALOG) == {
        "embed.unavailable", "embed.model_mismatch", "db.unreachable", "db.auth", "db.query", "recall.timeout",
        "judge.timeout", "judge.error", "rerank.fallback", "store.gate_error", "guard.crash", "guard.not_loaded",
        "config.unsafe", "config.missing", "redact.failed", "memory.write_rejected", "events.write_failed",
        "job.failed", "job.missed", "job.no_output", "unhandled",
    }
