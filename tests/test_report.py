"""Prove the daily failure report, `hermes custom-build report` (issue #138, OPERATIONS.md section 4).

Each case runs the real `hermes` CLI in a throwaway Hermes home, with the harness of test_heartbeat.py,
against events seeded in the throwaway store from the `db` fixture (conftest.py).
"""

import json
import pathlib
import re
import subprocess
import sys

import psycopg
import pytest

from tests.test_heartbeat import hermes, make_home, run_under_cron
from tests.test_job_health import recorded
from tests.test_plugin_load import FAKE_SECRET

# What Hermes does with the report's output when it delivers it: the MEDIA paths it would attach.
ATTACH = r"""
import json, sys
from gateway.platforms.base import BasePlatformAdapter, validate_media_delivery_path
media, text = BasePlatformAdapter.extract_media(sys.stdin.read())
print(json.dumps({"files": [validate_media_delivery_path(path) for path, _ in media], "text": text.strip()}))
"""


def seed(db, *events):
    """Insert (code, result, source, hours ago, reason, detail, session) rows."""
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        for code, result, source, hours, reason, detail, session in events:
            conn.execute("insert into memory.events (at, source, code, result, reason, detail, session_id)"
                         " values (now() - make_interval(hours => %s), %s, %s, %s, %s, %s, %s)",
                         (hours, source, code, result, reason, psycopg.types.json.Jsonb(detail), session))


def ran(code, source, hours, detail=None):
    return (code, "ok", source, hours, None, detail or {}, None)


def attach(tmp_path, home, output):
    result = subprocess.run([sys.executable, "-c", ATTACH], input=output, capture_output=True, text=True,
                            timeout=120, check=False, cwd=tmp_path,
                            env={"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_day_with_no_failures_sends_an_all_clear_naming_the_jobs_that_ran(tmp_path, db):
    seed(db, ran("host.heartbeat", "host-a", 2), ran("host.heartbeat", "host-b", 3),
         ran("job.succeeded", "host-a", 1, {"job": "job-health"}),
         ran("job.succeeded", "host-a", 30, {"job": "retention"}))   # outside the day
    home = make_home(tmp_path, db)
    result = hermes(tmp_path, home, "custom-build", "report")
    assert result.returncode == 0, result.stdout + result.stderr
    (line,) = result.stdout.strip().splitlines()   # one line, delivered as the message
    assert line.startswith("All clear"), line
    assert "heartbeat" in line and "host-a" in line and "host-b" in line and "job-health" in line, line
    assert "retention" not in line, line
    assert recorded(db) == [("job.succeeded", "ok", "host-a", {"job": "daily-report", "input": 0, "output": 1})]


def test_a_day_with_failures_sends_a_dated_file_grouped_by_code_and_machine(tmp_path, db):
    seed(db, ran("job.succeeded", "host-a", 1, {"job": "job-health"}),
         ("db.unreachable", "failed", "host-b", 5, "connection refused", {}, None),
         ("db.unreachable", "failed", "host-b", 3, "connection refused", {}, None),
         ("host.missing", "failed", "host-a", 2, None, {"host": "host-c"}, None),
         ("guard.bypassed", "failed", "host-b", 4, "safe_mode", {}, "s-1"),
         ("db.unreachable", "failed", "host-a", 6, "older", {}, None),
         ("db.unreachable", "failed", "host-a", 1, "newest | one\r\nwith \\| marks", {}, None),
         ("rerank.fallback", "degraded", "host-a", 2, "judge slow", {}, None),
         ("guard.crash", "failed", "host-b", 2, "KeyError", {}, "s-2"),
         ("config.missing", "failed", "host-a", 30, "machine_name", {}, None))   # outside the day
    home = make_home(tmp_path, db)
    result = hermes(tmp_path, home, "custom-build", "report")
    assert result.returncode == 0, result.stdout + result.stderr
    delivered = attach(tmp_path, home, result.stdout)
    (path,) = delivered["files"]   # Hermes's own media policy accepts the file
    assert path is not None and re.search(r"failures-\d{4}-\d{2}-\d{2}\.md$", path), path
    assert "8 failures" in delivered["text"], delivered["text"]   # the message that carries the file
    report = pathlib.Path(path).read_text(encoding="utf-8")
    for code, severity, shown in (("db.unreachable", "high", "no"), ("host.missing", "high", "no"),
                                  ("guard.bypassed", "high", "no"), ("rerank.fallback", "low", "no"),
                                  ("guard.crash", "high", "yes")):
        assert re.search(rf"^## {re.escape(code)}\b.*severity {severity}.*shown in the turn: {shown}", report, re.MULTILINE), code
    assert re.search(r"^\| host-b \| 2 \|", report, re.MULTILINE), report   # grouped by code, then machine
    (row,) = [line for line in report.split("\n") if line.startswith("| host-a | 2 |")]
    assert "newest" in row and "older" not in row, row   # its latest reason
    assert "\r" not in report and len(re.findall(r"(?<!\\)\|", row)) == 6, row   # no cell breaks its row
    assert "newest \\| one with \\\\\\| marks" in row, row   # a backslash in a reason is escaped too
    assert "host-c" in report and "s-1" in report   # what each finding names
    assert "config.missing" not in report   # outside the day
    assert recorded(db) == [("job.succeeded", "ok", "host-a", {"job": "daily-report", "input": 8, "output": 1})]


def test_a_day_the_job_health_check_did_not_run_is_reported(tmp_path, db):
    seed(db, ran("job.succeeded", "host-a", 26, {"job": "job-health"}))   # yesterday's, outside the day
    home = make_home(tmp_path, db)
    result = hermes(tmp_path, home, "custom-build", "report")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stdout.startswith("All clear"), result.stdout
    assert recorded(db)[0] == ("job.missed", "failed", "host-a", {"job": "job-health"})
    (path,) = attach(tmp_path, home, result.stdout)["files"]
    assert "job-health" in pathlib.Path(path).read_text(encoding="utf-8")


def test_the_report_redacts_what_it_sends(tmp_path, db):
    # It leaves the host, so a key in a recorded reason is redacted first (CONTRIBUTING.md rule L4).
    seed(db, ran("job.succeeded", "host-a", 1, {"job": "job-health"}),
         ("db.auth", "failed", "host-b", 1, f"refused with {FAKE_SECRET}", {}, None))
    home = make_home(tmp_path, db)
    config = home / "config.yaml"   # the build's redaction is forced, so it holds with Hermes's switched off
    config.write_text(config.read_text(encoding="utf-8") + "security:\n  redact_secrets: false\n", encoding="utf-8")
    result = hermes(tmp_path, home, "custom-build", "report")
    assert result.returncode == 0, result.stdout + result.stderr
    (path,) = attach(tmp_path, home, result.stdout)["files"]
    report = pathlib.Path(path).read_text(encoding="utf-8")
    assert "db.auth" in report
    assert FAKE_SECRET not in report and FAKE_SECRET not in result.stdout


def test_hermes_cron_runs_the_report_from_its_scripts_folder_and_delivers_it(tmp_path, db):
    seed(db, ran("job.succeeded", "host-a", 1, {"job": "job-health"}))
    answer, run = run_under_cron(tmp_path, make_home(tmp_path, db), "report", "0 7 * * *")
    assert "Ran now: succeeded" in answer, answer
    assert "All clear" in run, run   # what Hermes delivers to the job's target


@pytest.mark.parametrize("code, job", [("job.missed", "job-health"), ("job.succeeded", "daily-report")])
def test_a_row_the_store_refuses_fails_the_report(tmp_path, db, code, job):
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        conn.execute("create function memory.refuse() returns trigger language plpgsql as"
                     " $$ begin raise exception 'refused'; end $$")
        conn.execute(f"create trigger refuse_code before insert on memory.events for each row"
                     f" when (new.code = '{code}' and new.detail->>'job' = '{job}') execute function memory.refuse()")
    result = hermes(tmp_path, make_home(tmp_path, db), "custom-build", "report")
    assert result.returncode != 0, result.stdout + result.stderr   # so Hermes alerts instead of delivering
    assert "events.write_failed" in result.stderr, result.stderr
    if code == "job.missed":   # nothing printed that would read as a report of an incomplete day
        assert result.stdout == "", result.stdout


def test_a_day_with_one_failure_reads_in_the_singular(tmp_path, db):
    seed(db, ran("job.succeeded", "host-a", 1, {"job": "job-health"}),
         ("db.query", "failed", "host-a", 1, "rejected", {}, None))
    home = make_home(tmp_path, db)
    result = hermes(tmp_path, home, "custom-build", "report")
    delivered = attach(tmp_path, home, result.stdout)
    assert "1 failure under 1 code." in delivered["text"], delivered["text"]
    assert "1 failure." in pathlib.Path(delivered["files"][0]).read_text(encoding="utf-8")


def test_a_crash_in_the_report_names_the_job_as_the_job_health_check_does(tmp_path, db):
    def drop_job_missed(text):   # record() then raises inside the report: a bug, which the job wrapper records
        line = '    "job.missed": "A scheduled job did not run",\n'
        assert line in text
        return text.replace(line, "")

    result = hermes(tmp_path, make_home(tmp_path, db, failures_edit=drop_job_missed), "custom-build", "report")
    assert result.returncode != 0
    with psycopg.connect(db["owner"]) as conn:
        details = [d for (d,) in conn.execute("select detail from memory.events where code = 'job.failed'")]
    assert details == [{"job": "daily-report"}]   # the name job.succeeded and job.missed use


def test_a_job_health_success_another_host_wrote_does_not_count(tmp_path, db):
    # #149: the check runs on this host, the job host, so only its own row says the check ran.
    seed(db, ran("job.succeeded", "host-b", 1, {"job": "job-health"}))
    result = hermes(tmp_path, make_home(tmp_path, db), "custom-build", "report")
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded(db)[0] == ("job.missed", "failed", "host-a", {"job": "job-health"})
