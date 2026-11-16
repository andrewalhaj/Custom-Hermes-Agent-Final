"""Prove the mirror of built-in memory writes into the facts store (issue #142, ARCHITECTURE.md 2 and 3.3).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own whose working
directory is a throwaway repository, loads the provider through Hermes v0.23.1's memory-provider
loader and calls on_memory_write() as Hermes does after a committed write to MEMORY.md or USER.md.
Ollama and the judge are the `ollama` and `typesafe` fixtures (conftest.py).
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
RULE = "The user wants answers short, with the command first."
NEW_RULE = "The user wants answers short, with the command first and the risk after."
FAKE_KEY = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"   # assembled at run time, never a literal key

SCENARIO = r"""
import json, logging, sys
import plugins.memory

logging.basicConfig(level=logging.INFO, stream=sys.stderr)   # the test reads the log lines from stderr
ollama_url, judge_url, mode, writes = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
provider = plugins.memory.load_memory_provider("custom_build")
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = ollama_url
modules["judge"].URL = judge_url
if mode == "crash":
    def crash(*args, **kwargs):
        raise KeyError("boom")
    modules["store"].store = crash
provider.initialize(session_id="s1", hermes_home=sys.argv[5], platform="cli", agent_context="primary")
provider.on_turn_start(4, "a user message")
raised = []
for action, target, content, metadata in writes:
    try:
        provider.on_memory_write(action, target, content, metadata=metadata)
    except KeyError as exc:
        raised.append(type(exc).__name__)
print(json.dumps({"raised": raised}))
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


def mirror(home, db, ollama_url, judge_url, writes, *, mode="plain", env_extra=None):
    """Start the provider and mirror each (action, target, content, metadata); return (report, log output)."""
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "CUSTOM_BUILD_DATABASE_URL": db["agent"], "TYPESAFE_API_KEY": "test-key-not-a-real-one", **(env_extra or {})}
    result = subprocess.run(
        [sys.executable, "-c", SCENARIO, ollama_url, judge_url, mode, json.dumps(writes), str(home)],
        capture_output=True, text=True, env=env, cwd=home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr


def _facts(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute(
            "select body, basis, origin, project, source, session_id, source_turn, superseded_by is null,"
            " retired_reason from memory.facts order by created_at").fetchall()


def _codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select code from memory.events order by id")]


@pytest.mark.parametrize("target", ["memory", "user"])
def test_an_approved_entry_is_stored_as_a_fact_the_user_stated(home, db, ollama, typesafe, target):
    mirror(home, db, ollama.url, typesafe.url, [["add", target, RULE, {}]])
    # No project: built-in memory is one file per profile, not per repository (maintainer, #142).
    assert _facts(db) == [(RULE, "user_stated", "explicit", None, "host-a", "s1", 4, True, None)]


def test_a_short_entry_is_stored_too(home, db, ollama, typesafe):
    # A person approved it, so the stored-on-purpose filter does not apply (#142).
    mirror(home, db, ollama.url, typesafe.url, [["add", "memory", "Uses vim", {}]])
    assert [row[0] for row in _facts(db)] == ["Uses vim"]


def test_a_replaced_entry_supersedes_the_old_fact(home, db, ollama, typesafe):
    ollama.vectors = {RULE: [1.0] + [0.0] * 2559, NEW_RULE: [0.5, 0.866] + [0.0] * 2558}   # far apart: no judge
    mirror(home, db, ollama.url, typesafe.url,
           [["add", "memory", RULE, {}], ["replace", "memory", NEW_RULE, {"previous_content": RULE}]])
    assert [(row[0], row[7]) for row in _facts(db)] == [(RULE, False), (NEW_RULE, True)]


def test_a_removed_entry_retires_its_fact_and_can_come_back(home, db, ollama, typesafe):
    # Hermes v0.23.1 sends a remove with empty content; the entry is in metadata["previous_content"].
    removal = ["remove", "memory", "", {"old_text": "answers short", "previous_content": RULE}]
    mirror(home, db, ollama.url, typesafe.url, [["add", "memory", RULE, {}], removal, ["add", "memory", RULE, {}]])
    assert [(row[0], row[8]) for row in _facts(db)] == [(RULE, "removed from built-in memory"), (RULE, None)]


def test_a_judge_failure_queues_the_entry(home, db, ollama, typesafe):
    ollama.vectors = {RULE: [1.0] + [0.0] * 2559, NEW_RULE: [0.85, 0.5268] + [0.0] * 2558}
    typesafe.status = 500
    mirror(home, db, ollama.url, typesafe.url, [["add", "memory", RULE, {}], ["add", "memory", NEW_RULE, {}]])
    assert [row[0] for row in _facts(db)] == [RULE]
    assert _codes(db) == ["judge.error", "store.gate_error"]
    assert list((home / "plugin-data" / "custom_build").glob("store_queue.db"))


def test_an_ollama_that_cannot_be_reached_is_recorded(home, db, ollama_down, typesafe):
    mirror(home, db, ollama_down, typesafe.url, [["add", "memory", RULE, {}]])
    assert _facts(db) == []
    assert _codes(db) == ["embed.unavailable", "embed.unavailable"]   # the start's check, then the mirror's own


def test_a_secret_in_an_entry_never_reaches_the_store_or_a_log_line(home, db, ollama, typesafe):
    _, output = mirror(home, db, ollama.url, typesafe.url, [["add", "memory", f"The store key is {FAKE_KEY}.", {}]],
                       env_extra={"HERMES_REDACT_SECRETS": "false"})   # the build's redaction is forced
    stored = [row[0] for row in _facts(db)]
    assert len(stored) == 1 and FAKE_KEY not in stored[0]
    assert all(FAKE_KEY not in text for batch in ollama.embedded for text in batch)
    assert FAKE_KEY not in output


def test_a_crash_is_recorded_as_unhandled_and_re_raised(home, db, ollama, typesafe):
    report, _ = mirror(home, db, ollama.url, typesafe.url, [["add", "memory", RULE, {}]], mode="crash")
    assert report["raised"] == ["KeyError"]
    assert _codes(db) == ["unhandled"]


@pytest.mark.parametrize("columns, write", [
    ("retired_at, retired_reason", ["remove", "memory", "", {"previous_content": RULE}]),   # a store without 0003
    ("superseded_by", ["replace", "memory", NEW_RULE, {"previous_content": RULE}]),
])
def test_a_refused_retire_or_supersede_is_recorded_as_a_query_failure(home, db, ollama, typesafe, columns, write):
    ollama.vectors = {RULE: [1.0] + [0.0] * 2559, NEW_RULE: [0.5, 0.866] + [0.0] * 2558}
    with psycopg.connect(db["owner"]) as conn:
        conn.execute(f"revoke update ({columns}) on memory.facts from memory_agent")
    report, _ = mirror(home, db, ollama.url, typesafe.url, [["add", "memory", RULE, {}], write])
    assert report["raised"] == []
    assert _codes(db) == ["db.query"]
