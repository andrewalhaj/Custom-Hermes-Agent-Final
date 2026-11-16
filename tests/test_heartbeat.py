"""Prove the heartbeat, `hermes custom-build heartbeat` (issue #106, ARCHITECTURE.md section 6.3).

Each case builds a throwaway Hermes home holding a copy of the plugin, a config.yaml and a .env, runs the
real `hermes` CLI there in a process of its own, and reads what the heartbeat wrote to `events` in the
throwaway store from the `db` fixture (conftest.py).
"""

import pathlib
import re
import shutil
import subprocess
import sys

import psycopg
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
HERMES = "import sys; from hermes_cli.main import main; sys.argv = ['hermes', *sys.argv[1:]]; main()"
WRAPPER = "custom-build-heartbeat.py"


def make_home(tmp_path, db, *, machine_name="host-a", database_url=True, failures_edit=None):
    """A Hermes home with the plugin installed and enabled, the way an agent host has it."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    if failures_edit:
        module = home / "plugins" / "custom_build" / "failures.py"
        module.write_text(failures_edit(module.read_text(encoding="utf-8")), encoding="utf-8")
    entry = {"settings": {"machine_name": machine_name}} if machine_name else {}
    config = {"memory": {"provider": "custom_build"},
              "plugins": {"enabled": ["custom_build"], "entries": {"custom_build": entry}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if database_url:
        (home / ".env").write_text(f"CUSTOM_BUILD_DATABASE_URL={db['agent']}\n", encoding="utf-8")
    return home


def hermes(tmp_path, home, *args):
    return subprocess.run(
        [sys.executable, "-c", HERMES, *args], capture_output=True, text=True, timeout=180, check=False,
        cwd=tmp_path, env={"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )


def rows(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select code, result, reason, source, detail from memory.events order by id").fetchall()


def test_the_heartbeat_records_one_event_and_prints_nothing(tmp_path, db):
    result = hermes(tmp_path, make_home(tmp_path, db), "custom-build", "heartbeat")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""   # Hermes cron would deliver anything printed, every 6 hours
    assert rows(db) == [("host.heartbeat", "ok", None, "host-a", {})]


def test_hermes_cron_runs_the_wrapper_from_its_scripts_folder_and_delivers_nothing(tmp_path, db):
    home = make_home(tmp_path, db)
    (home / "scripts").mkdir()
    # From the installed plugin into the folder Hermes runs script-only jobs from (OPERATIONS.md section 4).
    shutil.copy(home / "plugins" / "custom_build" / "scripts" / WRAPPER, home / "scripts" / WRAPPER)
    created = hermes(tmp_path, home, "cron", "create", "every 6h", "--name", "custom-build-heartbeat",
                     "--script", WRAPPER, "--no-agent", "--deliver", "local")
    assert created.returncode == 0, created.stdout + created.stderr
    job_id = re.search(r"Created job: ([0-9a-f]+)", created.stdout).group(1)
    ran = hermes(tmp_path, home, "cron", "run", job_id)   # with no gateway serving the home, it runs at once
    assert "Ran now: succeeded" in ran.stdout, ran.stdout + ran.stderr
    assert rows(db) == [("host.heartbeat", "ok", None, "host-a", {})]
    (run,) = (home / "cron" / "output" / job_id).glob("*.md")
    assert "silent (empty output)" in run.read_text(encoding="utf-8")


@pytest.mark.parametrize("missing, kwargs", [
    ("machine_name", {"machine_name": None}),
    ("CUSTOM_BUILD_DATABASE_URL", {"database_url": False}),
])
def test_a_missing_setting_stops_the_heartbeat_with_config_missing(tmp_path, db, missing, kwargs):
    home = make_home(tmp_path, db, **kwargs)
    result = hermes(tmp_path, home, "custom-build", "heartbeat")
    assert result.returncode != 0, result.stdout + result.stderr
    assert rows(db) == []   # no heartbeat from a host that cannot say which it is; config.missing has nowhere to go
    errors_log = (home / "logs" / "errors.log").read_text(encoding="utf-8")
    assert missing in errors_log
    assert errors_log.index('"code": "config.missing"') < errors_log.index('"code": "events.write_failed"')


def test_a_crash_in_the_heartbeat_is_recorded_by_the_job_wrapper(tmp_path, db):
    def drop_the_heartbeat_code(text):
        # record() then raises UnknownFailureCode inside the job: a bug, which the job wrapper records (rule F6).
        line = "    \"host.heartbeat\": \"Not a failure: an agent host's heartbeat job ran\",\n"
        assert line in text
        return text.replace(line, "")

    home = make_home(tmp_path, db, failures_edit=drop_the_heartbeat_code)
    result = hermes(tmp_path, home, "custom-build", "heartbeat")
    assert result.returncode != 0, result.stdout + result.stderr   # so Hermes cron delivers its error alert
    assert [(code, res, detail) for code, res, _, _, detail in rows(db)] == [
        ("unhandled", "failed", {}), ("job.failed", "failed", {"job": "heartbeat"}),
    ]
