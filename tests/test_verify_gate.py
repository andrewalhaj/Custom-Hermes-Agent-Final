"""Prove the verify gate in log mode (issue #124, ARCHITECTURE.md sections 5.3 and 5.4).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own, runs Hermes
v0.23.1's ordinary plugin discovery, then a sequence of steps through the functions Hermes itself uses:
`resolve_pre_tool_block` before a tool call, `_emit_post_tool_call_hook` after it, and
`apply_llm_output_transform` on the final reply. In log mode the gate blocks and withholds nothing; it
records a change to a listed file or setting the session never read, and a reply that states a listed
file's contents when the session never read it.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tomllib

import psycopg
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"

SCENARIO = r"""
import json, os, sys, threading, types
from hermes_cli.plugins import discover_plugins, get_plugin_manager, resolve_pre_tool_block
from model_tools import _emit_post_tool_call_hook
from agent.turn_finalizer import apply_llm_output_transform

discover_plugins()
entry = next((p for p in get_plugin_manager().list_plugins() if p["name"] == "custom_build"), None)
scenario, steps = sys.argv[1], json.loads(sys.argv[2])
if scenario == "crash":
    loaded = [m for name, m in sys.modules.items() if name.endswith("custom_build.guards.verify_gate")]
    assert len(loaded) == 1, sorted(n for n in sys.modules if "custom_build" in n)
    def crash(*args, **kwargs):
        raise KeyError("boom")
    loaded[0].changes = crash

def settle():
    # The plugin records on worker threads: let a step's recording finish, so rows keep the steps' order.
    for thread in threading.enumerate():
        if thread.name.startswith("custom_build."):
            thread.join(30)

answers = []
for n, step in enumerate(steps):
    kind, session = step[0], step[1]
    if kind == "corrupt":   # the plugin's state file, as Hermes names it, made unreadable
        from hermes_cli.plugins_state import PluginState
        path = PluginState("custom_build").path
        assert path.exists(), path
        path.write_text("{bad", encoding="utf-8")
        answers.append(None)
    elif kind == "pre":
        answers.append(resolve_pre_tool_block(step[2], step[3], session_id=session, task_id="t1"))
    elif kind == "post":
        _emit_post_tool_call_hook(function_name=step[2], function_args=step[3], result="{}", task_id="t1",
                                  session_id=session, status=step[4])
        answers.append(None)
    else:
        agent = types.SimpleNamespace(session_id=session, model="m", platform="cli")
        reply, transformed, _ = apply_llm_output_transform(agent, step[2], turn_id=f"u{n}")
        answers.append(reply if transformed else None)
    settle()
print(json.dumps({"entry": entry, "answers": answers}))
"""


def run_gate(tmp_path, db, steps, *, mode="log", scenario="plain"):
    """Run the steps; return (report, Hermes's output, events rows). A step is ["pre", session, tool,
    args], ["post", session, tool, args, status] or ["reply", session, text]."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    gate_config = home / "plugins" / "custom_build" / "guards" / "verify_gate.toml"
    text = gate_config.read_text(encoding="utf-8")
    assert '\nmode = "log"\n' in text
    gate_config.write_text(text.replace('\nmode = "log"\n', f'\nmode = "{mode}"\n'), encoding="utf-8")
    config = {"memory": {"provider": "custom_build"},
              "plugins": {"enabled": ["custom_build"],
                          "entries": {"custom_build": {"settings": {"machine_name": "host-a"}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    environment = {"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                   "CUSTOM_BUILD_DATABASE_URL": db["agent"]}
    result = subprocess.run([sys.executable, "-c", SCENARIO, scenario, json.dumps(steps)], capture_output=True,
                            text=True, env=environment, cwd=tmp_path, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    output = result.stderr + "".join(p.read_text(encoding="utf-8") for p in sorted((home / "logs").glob("*.log")))
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, reason, detail, source, session_id from memory.events"
                            " where code like 'verify_gate.%' or code = 'guard.crash' order by id").fetchall()
    return report, output, rows


def rules(rows):
    return [(code, detail["rule"]) for code, _, _, detail, _, _ in rows]


def test_the_gate_ships_in_log_mode():
    assert tomllib.loads((PLUGIN / "guards" / "verify_gate.toml").read_text(encoding="utf-8"))["mode"] == "log"


def test_a_change_to_each_listed_file_or_setting_never_read_is_recorded_and_goes_ahead(tmp_path, db):
    home = tmp_path / "home"
    steps = [
        ["pre", "s1", "write_file", {"path": str(home / "config.yaml"), "content": "x"}],
        ["pre", "s1", "patch", {"path": str(home / ".env"), "old_string": "a", "new_string": "b"}],
        ["pre", "s1", "write_file", {"path": str(home / "plugins" / "custom_build" / "extra.py"), "content": "x"}],
        ["pre", "s1", "patch", {"path": str(home / "memories" / "MEMORY.md"), "old_string": "a", "new_string": "b"}],
        ["pre", "s1", "terminal", {"command": f"sed -i s/a/b/ {home}/memories/USER.md"}],
        ["pre", "s1", "terminal", {"command": "hermes config set approvals.mode off"}],
    ]
    report, output, rows = run_gate(tmp_path, db, steps)
    assert report["answers"] == [None] * len(steps), output
    assert rules(rows) == [
        ("verify_gate.would_block", "config"), ("verify_gate.would_block", "env"),
        ("verify_gate.would_block", "plugin"), ("verify_gate.would_block", "memory"),
        ("verify_gate.would_block", "user"), ("verify_gate.would_block", "approvals.mode"),
    ], output
    assert {(row[1], row[4], row[5]) for row in rows} == {("ok", "host-a", "s1")}
    assert rows[0][3]["gen_ai.tool.name"] == "write_file"


def test_a_credential_in_a_command_never_reaches_the_store(tmp_path, db):
    # #115: the event names the listed file, or the setting and what was done to it, never the command.
    password = "hunter2" + "-not-a-real-one"
    steps = [["pre", "s1", "terminal", {"command": f"echo TOKEN={password} >> ~/home/.env"}],
             ["pre", "s1", "terminal", {"command": f"hermes config set approvals.mode {password}"}]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [row[2] for row in rows] == [str((tmp_path / "home" / ".env").resolve()), "hermes config set approvals.mode"], output
    assert password not in json.dumps(rows)


def test_a_read_reported_ok_clears_the_change(tmp_path, db):
    home = tmp_path / "home"
    steps = [
        ["post", "s1", "read_file", {"path": str(home / ".env")}, "ok"],
        ["pre", "s1", "patch", {"path": str(home / ".env"), "old_string": "a", "new_string": "b"}],
        ["post", "s1", "terminal", {"command": "cat ~/home/config.yaml"}, "ok"],   # a command that only names it
        ["pre", "s1", "terminal", {"command": "sed -i s/a/b/ ~/home/config.yaml"}],
        ["post", "s1", "terminal", {"command": "hermes config get approvals.mode"}, "ok"],
        ["pre", "s1", "terminal", {"command": "hermes config set approvals.mode manual"}],
        ["post", "s1", "terminal", {"command": "hermes config show"}, "ok"],   # shows every setting
        ["pre", "s1", "terminal", {"command": "hermes config set memory.provider custom_build"}],
    ]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output


def test_a_read_with_another_status_or_in_another_session_does_not_count(tmp_path, db):
    home = tmp_path / "home"
    change = ["pre", "s1", "patch", {"path": str(home / ".env"), "old_string": "a", "new_string": "b"}]
    steps = [
        ["post", "s1", "read_file", {"path": str(home / ".env")}, "error"], change,
        ["post", "s1", "read_file", {"path": str(home / ".env")}, "blocked"], change,
        ["post", "s2", "read_file", {"path": str(home / ".env")}, "ok"], change,
    ]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rules(rows) == [("verify_gate.would_block", "env")] * 3, output


def test_a_change_off_the_list_records_nothing(tmp_path, db):
    home = tmp_path / "home"
    steps = [
        ["pre", "s1", "write_file", {"path": str(tmp_path / "notes.txt"), "content": "x"}],
        ["pre", "s1", "write_file", {"path": str(home / "SOUL.md"), "content": "x"}],
        ["pre", "s1", "terminal", {"command": "hermes config set display.theme dark"}],   # not a listed key
        ["pre", "s1", "read_file", {"path": str(home / "config.yaml")}],   # a read is not a change
        ["pre", "s1", "terminal", {"command": "cat ~/home/.env"}],
        ["pre", "s1", "memory", {"action": "add", "target": "memory", "content": "x"}],   # held by Hermes's own checks
    ]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output


def test_a_reply_stating_a_listed_files_contents_unread_is_recorded_and_delivered(tmp_path, db):
    home = tmp_path / "home"
    steps = [
        ["reply", "s1", "Your config.yaml contains approvals.mode: deny. Nothing else changed."],
        ["reply", "s1", "I will edit config.yaml next."],   # names it without stating what it holds
        ["post", "s1", "read_file", {"path": str(home / "memories" / "USER.md")}, "ok"],
        ["reply", "s1", "USER.md says you prefer short answers."],   # read, so it may say so
        ["reply", "s2", "USER.md says you prefer short answers."],
    ]
    report, output, rows = run_gate(tmp_path, db, steps)
    assert report["answers"] == [None] * len(steps), output   # no reply is rewritten
    assert [(code, detail["rule"], session) for code, _, _, detail, _, session in rows] == [
        ("verify_gate.would_withhold", "config", "s1"), ("verify_gate.would_withhold", "user", "s2"),
    ], output
    assert "approvals.mode" not in json.dumps(rows)   # the reply's text is never stored (rule L5)


def test_off_registers_no_hook_and_records_nothing(tmp_path, db):
    home = tmp_path / "home"
    report, output, rows = run_gate(tmp_path, db, [["pre", "s1", "patch", {"path": str(home / ".env"),
                                                                           "old_string": "a", "new_string": "b"}]],
                                    mode="off")
    assert report["entry"]["error"] is None, output
    assert report["entry"]["hooks"] == 8   # the settings check, the write gate, and the fact and recall gates' three each
    assert rows == []


@pytest.mark.parametrize("mode", ["on", "enforce"])
def test_a_mode_other_than_log_or_off_fails_the_load(tmp_path, db, mode):
    report, output, _ = run_gate(tmp_path, db, [], mode=mode)
    assert report["entry"]["error"], output


def test_a_crash_in_the_gate_is_recorded_and_the_call_goes_ahead(tmp_path, db):
    home = tmp_path / "home"
    report, output, rows = run_gate(tmp_path, db, [["pre", "s1", "patch", {"path": str(home / ".env"),
                                                                           "old_string": "a", "new_string": "b"}]],
                                    scenario="crash")
    assert report["answers"] == [None], output
    assert [(code, reason, session) for code, _, reason, _, _, session in rows] == [
        ("guard.crash", "KeyError: 'boom'", "s1")], output


def test_only_the_100_most_recently_active_sessions_keep_their_reads(tmp_path, db):
    env = tmp_path / "home" / ".env"
    read = [["post", f"s{n}", "read_file", {"path": str(env)}, "ok"] for n in range(100)]   # s0 .. s99
    steps = [*read, ["post", "s0", "read_file", {"path": str(env)}, "ok"],   # s0 active again, so s1 is now oldest
             ["post", "s100", "read_file", {"path": str(env)}, "ok"]]   # the 101st session drops the oldest
    steps += [["pre", s, "patch", {"path": str(env), "old_string": "a", "new_string": "b"}] for s in ("s1", "s0", "s100")]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [(code, session) for code, *_, session in rows] == [("verify_gate.would_block", "s1")], output


def test_an_unreadable_state_file_troubles_only_calls_on_the_list(tmp_path, db):
    home = tmp_path / "home"
    steps = [["post", "s1", "read_file", {"path": str(home / ".env")}, "ok"],   # creates the state file
             ["corrupt", "s1"],
             ["pre", "s1", "read_file", {"path": str(tmp_path / "notes.txt")}],
             ["reply", "s1", "hi"]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output


def test_a_chained_hermes_config_command_is_read_whole(tmp_path, db):
    steps = [["pre", "s1", "terminal", {"command": "hermes config get approvals.mode && hermes config set approvals.mode off"}]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [rule for _, rule in rules(rows)] == ["approvals.mode"], output


def test_a_chained_hermes_config_command_counts_every_read(tmp_path, db):
    steps = [["post", "s1", "terminal", {"command": "hermes config set memory.provider x && hermes config get approvals.mode"}, "ok"],
             ["pre", "s1", "terminal", {"command": "hermes config set approvals.mode manual"}]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output
