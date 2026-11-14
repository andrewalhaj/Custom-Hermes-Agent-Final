"""Prove the per-turn settings check (issue #68, ARCHITECTURE.md sections 2 and 5.5).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own, runs Hermes
v0.23.1's ordinary plugin discovery and then the function Hermes's turn uses to run `pre_llm_call`
hooks, `_collect_pre_llm_call_context`. A block it collects ends the turn before any model call.
"""

import json
import pathlib
import shutil
import subprocess
import sys

import psycopg
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"

SCENARIO = r"""
import json, sys, types
from hermes_cli.plugins import discover_plugins
from agent.turn_context import _collect_pre_llm_call_context

discover_plugins()
if sys.argv[1] == "crash":
    # The ordinary load's copy of the settings module; the hook looks the function up at each call.
    loaded = [m for name, m in sys.modules.items() if name.endswith("custom_build.settings")]
    assert len(loaded) == 1, sorted(n for n in sys.modules if "custom_build" in n)
    def crash(ctx):
        raise KeyError("boom")
    loaded[0].missing_settings = crash
agent = types.SimpleNamespace(session_id="s1", model="m", platform="cli", _persist_disabled=False)
blocks = []
context = _collect_pre_llm_call_context(agent, effective_task_id="t1", turn_id="u1", original_user_message="hello",
                                        messages=[], conversation_history=[], blocks=blocks)
print(json.dumps({"blocks": blocks, "context": context}))
"""


def run_turn(tmp_path, db, *, machine_name="host-a", database_url=True, mode="plain"):
    """Run one turn's pre_llm_call hooks; return (blocks, Hermes's output, events rows)."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    entry = {"settings": {"machine_name": machine_name}} if machine_name else {}
    config = {"memory": {"provider": "custom_build"},
              "plugins": {"enabled": ["custom_build"], "entries": {"custom_build": entry}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    env = {"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    if database_url:
        env["CUSTOM_BUILD_DATABASE_URL"] = db["agent"]
    result = subprocess.run([sys.executable, "-c", SCENARIO, mode], capture_output=True, text=True,
                            env=env, cwd=tmp_path, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    logs = home / "logs"
    output = result.stderr + "".join(p.read_text(encoding="utf-8") for p in sorted(logs.glob("*.log")))
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, reason, source, session_id from memory.events order by id").fetchall()
    return report["blocks"], output, rows


def test_a_turn_with_both_settings_goes_ahead(tmp_path, db):
    blocks, output, rows = run_turn(tmp_path, db)
    assert blocks == [], output
    assert rows == []


def test_a_missing_machine_name_blocks_the_turn(tmp_path, db):
    blocks, output, rows = run_turn(tmp_path, db, machine_name=None)
    assert len(blocks) == 1, output
    assert "config.missing" in blocks[0]
    assert "plugins.entries.custom_build.settings.machine_name" in blocks[0]
    assert rows == []   # nowhere to write without the machine's name: logged with events.write_failed
    assert output.index('"code": "config.missing"') < output.index('"code": "events.write_failed"')


def test_a_missing_database_url_blocks_the_turn(tmp_path, db):
    blocks, output, rows = run_turn(tmp_path, db, database_url=False)
    assert len(blocks) == 1, output
    assert "config.missing" in blocks[0] and "CUSTOM_BUILD_DATABASE_URL" in blocks[0]
    assert rows == []


def test_a_crash_in_the_check_is_recorded_and_blocks_the_turn(tmp_path, db):
    blocks, output, rows = run_turn(tmp_path, db, mode="crash")
    assert len(blocks) == 1, output
    assert "guard.crash" in blocks[0]
    assert rows == [("guard.crash", "failed", "KeyError: 'boom'", "host-a", "s1")], output
