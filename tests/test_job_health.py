"""Prove the job-health check, `hermes custom-build job-health` (issue #134, ARCHITECTURE.md section 6.3).

Each case runs the real `hermes` CLI in a throwaway Hermes home, with the harness of test_heartbeat.py,
against events seeded in the throwaway store from the `db` fixture (conftest.py).
"""

import psycopg
import pytest
import yaml

from tests.test_heartbeat import hermes, make_home, run_under_cron


def with_hosts(home, hosts):
    """Set the job host's list of agent hosts, `plugins.entries.custom_build.settings.agent_hosts`."""
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["plugins"]["entries"]["custom_build"]["settings"]["agent_hosts"] = hosts
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return home


def seed(db, *events):
    """Insert (code, source, hours ago, detail) rows, as earlier runs on any host would have."""
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        for code, source, hours, detail in events:
            conn.execute("insert into memory.events (at, source, code, result, detail)"
                         " values (now() - make_interval(hours => %s), %s, %s, 'ok', %s)",
                         (hours, source, code, psycopg.types.json.Jsonb(detail)))


def recorded(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select code, result, source, detail from memory.events"
                            " where at > now() - interval '1 minute' order by id").fetchall()


def test_a_host_with_no_heartbeat_in_24_hours_is_recorded_as_missing(tmp_path, db):
    home = with_hosts(make_home(tmp_path, db), ["host-a", "host-b", "host-c"])
    seed(db, ("host.heartbeat", "host-a", 5, {}), ("host.heartbeat", "host-b", 25, {}))
    result = hermes(tmp_path, home, "custom-build", "job-health")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""   # Hermes cron would deliver anything printed; the report sends the findings
    assert recorded(db) == [
        ("host.missing", "failed", "host-a", {"host": "host-b"}),
        ("host.missing", "failed", "host-a", {"host": "host-c"}),
        ("job.succeeded", "ok", "host-a", {"job": "job-health"}),
    ]


@pytest.mark.parametrize("hosts", [None, [], ["host-a", ""], "host-a", [7], [" host-a"], ["host-a", "host-a"]],
                         ids=["unset", "empty", "a blank name", "not a list", "not text", "a padded name", "a repeat"])
def test_a_missing_or_invalid_host_list_stops_the_check_with_config_missing(tmp_path, db, hosts):
    home = make_home(tmp_path, db)
    if hosts is not None:
        with_hosts(home, hosts)
    result = hermes(tmp_path, home, "custom-build", "job-health")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "config.missing" in result.stderr and "agent_hosts" in result.stderr, result.stderr
    assert recorded(db) == []


def test_a_listed_job_with_no_recent_success_or_no_output_is_recorded(tmp_path, db):
    home = with_hosts(make_home(tmp_path, db), ["host-a"])
    module = home / "plugins" / "custom_build" / "job_health.py"
    text = module.read_text(encoding="utf-8")
    assert "JOBS = ()" in text   # the list starts empty; the report and retention add themselves
    jobs = ("nightly", "digest", "fresh", "never-ran", "one-empty", "idle", "ordered", "uncounted")
    module.write_text(text.replace("JOBS = ()", f"JOBS = {jobs!r}"), encoding="utf-8")
    seed(db, ("host.heartbeat", "host-a", 1, {}),
         ("job.succeeded", "host-a", 30, {"job": "nightly"}),                         # last ran 30 hours ago
         ("job.succeeded", "host-a", 20, {"job": "digest", "input": 5, "output": 0}),
         ("job.succeeded", "host-a", 2, {"job": "digest", "input": 4, "output": 0}),   # two runs in, nothing out
         ("job.succeeded", "host-a", 26, {"job": "fresh", "input": 3, "output": 0}),
         ("job.succeeded", "host-a", 1, {"job": "fresh", "input": 3, "output": 3}),   # one empty run, then output
         # Not flagged: one empty run alone; nothing to do (no input); only the newest two runs count;
         # a run that reports no output count is no evidence of none.
         ("job.succeeded", "host-a", 1, {"job": "one-empty", "input": 2, "output": 0}),
         ("job.succeeded", "host-a", 20, {"job": "idle", "input": 0, "output": 0}),
         ("job.succeeded", "host-a", 2, {"job": "idle", "input": 0, "output": 0}),
         ("job.succeeded", "host-a", 30, {"job": "ordered", "input": 2, "output": 0}),
         ("job.succeeded", "host-a", 20, {"job": "ordered", "input": 2, "output": 0}),
         ("job.succeeded", "host-a", 1, {"job": "ordered", "input": 2, "output": 2}),
         ("job.succeeded", "host-a", 20, {"job": "uncounted", "input": 2}),
         ("job.succeeded", "host-a", 2, {"job": "uncounted", "input": 2}))
    result = hermes(tmp_path, home, "custom-build", "job-health")
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded(db) == [
        ("job.missed", "failed", "host-a", {"job": "nightly"}),
        ("job.no_output", "failed", "host-a", {"job": "digest"}),
        ("job.missed", "failed", "host-a", {"job": "never-ran"}),
        ("job.succeeded", "ok", "host-a", {"job": "job-health"}),
    ]


def test_hermes_cron_runs_the_check_daily_from_its_scripts_folder_and_delivers_nothing(tmp_path, db):
    seed(db, ("host.heartbeat", "host-a", 1, {}))
    answer, run = run_under_cron(tmp_path, with_hosts(make_home(tmp_path, db), ["host-a"]), "job-health", "45 6 * * *")
    assert "Ran now: succeeded" in answer, answer
    assert "silent (empty output)" in run
    assert recorded(db) == [("job.succeeded", "ok", "host-a", {"job": "job-health"})]


def test_a_finding_the_store_refuses_fails_the_check(tmp_path, db):
    # The store takes the check's own success but not its finding: that finding must not be lost quietly.
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        conn.execute("create function memory.refuse() returns trigger language plpgsql as"
                     " $$ begin raise exception 'refused'; end $$")
        conn.execute("create trigger refuse_missing before insert on memory.events for each row"
                     " when (new.code = 'host.missing') execute function memory.refuse()")
    result = hermes(tmp_path, with_hosts(make_home(tmp_path, db), ["host-b"]), "custom-build", "job-health")
    assert result.returncode != 0, result.stdout + result.stderr   # so Hermes cron delivers its error alert
    assert "events.write_failed" in result.stderr, result.stderr
    assert recorded(db) == []   # a run that lost a finding does not count as having run


def test_a_day_the_report_did_not_run_is_recorded_as_missed(tmp_path, db):
    seed(db, ("host.heartbeat", "host-a", 1, {}), ("job.succeeded", "host-a", 26, {"job": "daily-report"}))
    result = hermes(tmp_path, with_hosts(make_home(tmp_path, db), ["host-a"]), "custom-build", "job-health")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("job.missed", "failed", "host-a", {"job": "daily-report"}) in recorded(db)
