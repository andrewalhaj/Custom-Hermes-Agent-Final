"""Prove the store_fact tool, its retry queue and its redaction (issue #113, ARCHITECTURE.md 2 and 3.3).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own whose working
directory is a throwaway repository, loads the provider through Hermes v0.23.1's memory-provider
loader and drives it as Hermes does: initialize(), on_turn_start(), then handle_tool_call(). Ollama and
the judge are the `ollama` and `typesafe` fixtures (conftest.py).
"""

import json
import pathlib
import shutil
import subprocess
import sys

import psycopg
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
STORED = "The mini's gateway runs as root."
CLOSE = "The mini's gateway service user is configured in its unit."
# A fake Supabase secret key, assembled at run time so no key-shaped literal sits in the repository.
FAKE_KEY = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"

SCENARIO = r"""
import json, logging, sys
import plugins.memory

logging.basicConfig(level=logging.INFO, stream=sys.stderr)   # the test reads the log lines from stderr
ollama_url, judge_url, mode, calls = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
provider = plugins.memory.load_memory_provider("custom_build")
# The provider load imports the plugin as _hermes_user_memory.custom_build__source_<hash>.
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = ollama_url
modules["judge"].URL = judge_url
if mode == "crash":
    def crash(*args, **kwargs):
        raise KeyError("boom")
    modules["store"].store = crash
provider.initialize(session_id="s1", hermes_home=sys.argv[5], platform="cli")
if mode == "switch":   # /new, /resume or /branch while the process runs
    provider.on_session_switch("s2", parent_session_id="s1")
provider.on_turn_start(4, "a user message")
results = []
for args in calls:
    try:
        results.append(json.loads(provider.handle_tool_call("store_fact", args)))
    except KeyError as exc:
        results.append({"raised": type(exc).__name__})
print(json.dumps({"tools": [s["name"] for s in provider.get_tool_schemas()], "results": results}))
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    config = {"memory": {"provider": "custom_build"}, "plugins": {"entries": {"custom_build": {
        "settings": {"machine_name": "host-a", "judge_model": "jev-1.14.0"}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Owner/Repo.git"], check=True)
    return home


def run(home, db, ollama_url, judge_url, calls, *, mode="plain"):
    """Start the provider and call store_fact once per entry of `calls`; return (report, log output)."""
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "CUSTOM_BUILD_DATABASE_URL": db["agent"], "TYPESAFE_API_KEY": "test-key-not-a-real-one"}
    result = subprocess.run(
        [sys.executable, "-c", SCENARIO, ollama_url, judge_url, mode, json.dumps(calls), str(home)],
        capture_output=True, text=True, env=env, cwd=home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr


def _events(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select code, result, source, session_id from memory.events order by id").fetchall()


def _facts(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select body, basis, origin, project, source, session_id, source_turn, superseded_by is null"
                            " from memory.facts order by created_at").fetchall()


def test_the_provider_offers_the_store_tool(home, db, ollama, typesafe):
    report, _ = run(home, db, ollama.url, typesafe.url, [])
    assert report["tools"] == ["store_fact"]


def test_a_fact_is_stored_with_its_project_session_and_turn(home, db, ollama, typesafe):
    report, output = run(home, db, ollama.url, typesafe.url, [{"fact": STORED, "basis": "user_stated"}])
    assert report["results"][0]["superseded"] is None, output
    assert _facts(db) == [(STORED, "user_stated", "explicit", "github.com/Owner/Repo", "host-a", "s1", 4, True)]
    assert _events(db) == []


def test_after_a_session_switch_a_fact_records_the_new_session(home, db, ollama, typesafe):
    run(home, db, ollama.url, typesafe.url, [{"fact": STORED, "basis": "user_stated"}], mode="switch")
    assert [row[5] for row in _facts(db)] == ["s2"]


def test_a_refused_fact_is_not_stored_and_says_why(home, db, ollama, typesafe):
    report, _ = run(home, db, ollama.url, typesafe.url, [{"fact": "Let me check the logs now.", "basis": "user_stated"}])
    assert report["results"][0]["stored"] is False
    assert report["results"][0]["refused"].startswith("narration")
    assert _facts(db) == []


def test_an_ollama_that_cannot_be_reached_is_an_error_not_a_stored_fact(home, db, ollama_down, typesafe):
    report, _ = run(home, db, ollama_down, typesafe.url, [{"fact": STORED, "basis": "user_stated"}])
    assert report["results"][0] == {"error": "embed.unavailable"}
    assert ("embed.unavailable", "failed", "host-a", "s1") in _events(db)
    assert _facts(db) == []


def test_a_judge_failure_queues_the_fact_and_a_later_start_stores_it(home, db, ollama, typesafe):
    ollama.vectors = {STORED: [1.0] + [0.0] * 2559, CLOSE: [0.85, 0.5268] + [0.0] * 2558}
    typesafe.status = 500
    report, _ = run(home, db, ollama.url, typesafe.url,
                    [{"fact": STORED, "basis": "user_stated"}, {"fact": CLOSE, "basis": "tool_observed"}])
    assert report["results"][1] == {"error": "store.gate_error", "queued": True}
    assert [row[:2] for row in _events(db)] == [("judge.error", "failed"), ("store.gate_error", "failed")]
    assert [row[0] for row in _facts(db)] == [STORED]

    typesafe.status = 200
    typesafe.answers = {"same_subject": 0.9, "supersedes": 0.9}
    run(home, db, ollama.url, typesafe.url, [])   # the next start retries the queue
    assert [row[:2] for row in _events(db)][-1] == ("store.gate_error", "degraded")
    assert [(row[0], row[1], row[7]) for row in _facts(db)] == [(STORED, "user_stated", False), (CLOSE, "tool_observed", True)]

    run(home, db, ollama.url, typesafe.url, [])   # and the queue is empty after it
    assert [row[:2] for row in _events(db)].count(("store.gate_error", "degraded")) == 1


def test_a_secret_never_reaches_the_store_the_judge_the_queue_or_a_log_line(home, db, ollama, typesafe):
    secret_fact = f"The deploy key for the store is {FAKE_KEY} on the mini."
    ollama.vectors = {STORED: [1.0] + [0.0] * 2559}
    ollama.other = [0.85, 0.5268] + [0.0] * 2558   # the secret fact lands in the judge's band
    typesafe.status = 500                          # so it is queued
    _, first = run(home, db, ollama.url, typesafe.url,
                   [{"fact": STORED, "basis": "user_stated"}, {"fact": secret_fact, "basis": "user_stated"}])
    queue = list((home / "plugin-data" / "custom_build").glob("store_queue.db*"))
    assert queue and all(FAKE_KEY.encode() not in path.read_bytes() for path in queue)
    typesafe.status = 200
    typesafe.answers = {"same_subject": 0.1, "supersedes": 0.1}
    _, second = run(home, db, ollama.url, typesafe.url, [])   # the retry stores it beside the first
    stored = [row[0] for row in _facts(db)]
    assert len(stored) == 2 and FAKE_KEY not in stored[1], stored
    assert len(typesafe.requests) == 2 and all(FAKE_KEY not in json.dumps(r["body"]) for r in typesafe.requests)
    assert all(FAKE_KEY not in text for batch in ollama.embedded for text in batch)
    logs = "".join(path.read_text(encoding="utf-8") for path in (home / "logs").glob("*.log"))
    assert FAKE_KEY not in first + second + logs


def test_a_crash_is_recorded_as_unhandled_and_re_raised(home, db, ollama, typesafe):
    report, _ = run(home, db, ollama.url, typesafe.url, [{"fact": STORED, "basis": "user_stated"}], mode="crash")
    assert report["results"] == [{"raised": "KeyError"}]
    # A tool call carries no session id for the wrapper to record.
    assert [row[:3] for row in _events(db)] == [("unhandled", "failed", "host-a")]
