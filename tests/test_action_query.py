"""Prove the action query, built switched off (issue #174; ARCHITECTURE.md 3.5 point 2, 3.6, and section 2's table).

Each case copies the plugin into a throwaway Hermes home, enables it as an agent host does, and, in a process of
its own whose working directory is a throwaway repository, runs Hermes v0.23.1's `pre_llm_call` hooks through its
own `invoke_hook`, with the arguments Hermes passes before a turn. The judge and Ollama are the tests' fakes.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tomllib
import uuid

import psycopg
import pytest
import yaml

from custom_build import failures

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
CONFIG = PLUGIN / "memory" / "action_query.toml"
PROJECT = "github.com/Owner/Repo"
BASE = [1.0] + [0.0] * 2559
MESSAGE = "fix the failing test in the parser"
PREVIOUS = "x" * 600 + " and the parser test still fails on an empty file."
HISTORY = [{"role": "user", "content": "run the tests"}, {"role": "assistant", "content": PREVIOUS},
           {"role": "user", "content": MESSAGE}]
FAILED = "Recall failed ({}): memory could not be searched this turn, so its notes are unknown, not empty."

SCENARIO = r"""
import json, os, sys
from hermes_cli.plugins import discover_plugins
from hermes_cli.lifecycle import invoke_hook
discover_plugins()
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = os.environ["OLLAMA_URL"]
modules["judge"].URL = os.environ["JUDGE_URL"]
mode = os.environ.get("MODE", "")
if mode == "crash":
    def crash(*args, **kwargs):
        raise KeyError("boom")
    modules["search"].candidates = crash
holder = None
if mode == "stall":   # every read of facts now waits on this lock
    import psycopg
    holder = psycopg.connect(os.environ["OWNER_URL"])
    holder.execute("lock table memory.facts in access exclusive mode")
results = invoke_hook("pre_llm_call", session_id="s1", task_id="t1", turn_id="u1",
                      user_message=os.environ["MESSAGE"], conversation_history=json.loads(os.environ["HISTORY"]),
                      is_first_turn=False, model="m", platform="cli", parent_session_id="", sender_id="")
if holder is not None:
    holder.rollback()
context = [r["context"] for r in results if isinstance(r, dict) and r.get("context")]
print(json.dumps({"context": context, "results": results}), flush=True)
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    config = {"plugins": {"enabled": ["custom_build"], "entries": {"custom_build": {
        "settings": {"machine_name": "host-a", "judge_model": "jev-1.14.0"}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Owner/Repo.git"], check=True)
    return home


def switch_on(home, **values):
    """The action query switched on in the home's copy of the plugin, with any other values changed."""
    path = home / "plugins" / "custom_build" / "memory" / "action_query.toml"
    lines = path.read_text(encoding="utf-8").splitlines()
    changes = {"on": "true", **{name: str(value) for name, value in values.items()}}
    for n, line in enumerate(lines):
        name = line.split(" = ", 1)[0]
        if name in changes and " = " in line:
            lines[n] = f"{name} = {changes.pop(name)}"
    assert changes == {}, f"not in the file: {changes}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def actions():
    return tomllib.loads(CONFIG.read_text(encoding="utf-8"))["actions"]


def _add(owner, body, vector, build):
    owner.execute(
        "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding, embedding_model)"
        " values (%s, %s, 'fact', 'explicit', 'user_stated', %s, 'host-a', %s, %s)",
        (body, uuid.uuid4().hex, PROJECT, str(vector), build))


def turn(home, db, ollama, typesafe, *, mode=None, message=MESSAGE, history=HISTORY):
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "TYPESAFE_API_KEY": "test-key-not-a-real-one", "CUSTOM_BUILD_DATABASE_URL": db["agent"],
           "OWNER_URL": db["owner"], "OLLAMA_URL": ollama.url, "JUDGE_URL": typesafe.url,
           "MESSAGE": message, "HISTORY": json.dumps(history), "MODE": mode or ""}
    result = subprocess.run([sys.executable, "-c", SCENARIO], capture_output=True, text=True, env=env,
                            cwd=home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select code from memory.events order by id")]


def _routing(typesafe):
    return [r["body"] for r in typesafe.requests if "action" in r["body"]["questions"]]


def test_the_action_query_ships_switched_off_and_then_does_nothing(home, db, ollama, typesafe):
    assert tomllib.loads(CONFIG.read_text(encoding="utf-8"))["on"] is False   # switched on only after #26
    seen = turn(home, db, ollama, typesafe)
    assert seen["context"] == [] and typesafe.requests == [] and _codes(db) == []


def test_switched_on_it_routes_the_message_and_the_previous_reply_end_through_the_judge(home, db, ollama, typesafe):
    switch_on(home)
    typesafe.answers, typesafe.default = {"asks_for_work": 0.9}, 0.5
    turn(home, db, ollama, typesafe)
    [routing] = _routing(typesafe)
    assert routing["state"] == {"message": MESSAGE, "previous_assistant_reply_end": PREVIOUS[-500:]}
    assert routing["questions"]["action"]["type"] == "choice"
    assert routing["questions"]["action"]["criteria"] == actions() and len(actions()) == 12
    assert routing["questions"]["asks_for_work"]["type"] == "noul"


def test_switched_on_it_adds_three_notes_found_by_the_picked_actions_description(home, db, ollama, typesafe):
    switch_on(home)
    typesafe.answers, typesafe.default, typesafe.choices = {"asks_for_work": 0.9}, 0.5, {"action": "change_code"}
    ollama.vectors = {actions()["change_code"]: BASE}
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        for n in range(4):
            _add(owner, f"Run the parser tests with pytest -k parser, note {n}.", BASE, ollama.build)
    seen = turn(home, db, ollama, typesafe)
    [context] = seen["context"]
    lines = context.splitlines()
    assert lines[0].startswith("Recalled notes for this kind of work") and len(lines) == 4
    assert all("Run the parser tests" in line for line in lines[1:])
    assert _codes(db) == []


def test_a_message_that_asks_for_no_work_skips_the_search_and_records_the_skip(home, db, ollama, typesafe):
    switch_on(home)
    typesafe.answers, typesafe.default = {"asks_for_work": 0.2}, 0.5   # under 0.3: a clear no (3.6)
    seen = turn(home, db, ollama, typesafe, message="thanks!")
    assert seen["context"] == [] and ollama.embedded == []
    assert _codes(db) == ["action.skipped"]


@pytest.mark.parametrize("fault, code", [("status", "judge.error"), ("delay", "judge.timeout")])
def test_a_routing_failure_skips_the_query_records_it_and_tells_the_turn(home, db, ollama, typesafe, fault, code):
    switch_on(home)
    if fault == "status":
        typesafe.status = 500
    else:
        typesafe.delay, typesafe.answers = 3, {"asks_for_work": 0.9}   # past the 2-second judge deadline
    seen = turn(home, db, ollama, typesafe)
    assert seen["context"] == [FAILED.format(code)]
    assert _codes(db) == [code] and ollama.embedded == []


def test_a_search_past_the_action_querys_deadline_is_recall_timeout(home, db, ollama, typesafe):
    switch_on(home, deadline=1)
    typesafe.answers, typesafe.default = {"asks_for_work": 0.9}, 0.5
    seen = turn(home, db, ollama, typesafe, mode="stall")
    assert seen["context"] == [FAILED.format("recall.timeout")]


def test_a_crash_is_recorded_as_unhandled_and_the_turn_goes_ahead(home, db, ollama, typesafe):
    switch_on(home)
    typesafe.answers, typesafe.default = {"asks_for_work": 0.9}, 0.5
    seen = turn(home, db, ollama, typesafe, mode="crash")
    assert seen["context"] == [FAILED.format("unhandled")]
    assert "unhandled" in _codes(db)
    assert not any(isinstance(r, dict) and r.get("action") == "block" for r in seen["results"])


def test_the_skip_has_its_own_code_which_is_not_a_failure():
    assert failures.CATALOG["action.skipped"].startswith("Not a failure")
    assert "action.skipped" not in failures.SEVERITY
