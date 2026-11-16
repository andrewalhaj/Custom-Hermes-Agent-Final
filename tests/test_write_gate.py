"""Prove the write gate in log mode (issue #107, ARCHITECTURE.md sections 5.3, 5.4 and 5.6).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own, runs Hermes
v0.23.1's ordinary plugin discovery and then, for each tool call, the function Hermes's tool dispatch
uses to run `pre_tool_call` hooks and act on what they return, `resolve_pre_tool_block`. It returns
the message that blocks the call, or None when the call goes ahead. In log mode the gate blocks
nothing and records each call on its list in memory.events.
"""

import contextlib
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
# A fake key in Supabase's format, assembled at run time as in test_plugin_load.py.
FAKE_SECRET = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"
URL_TOKEN = "abcd1234" + "efgh5678ijkl"   # a fake credential in a URL's query string

SCENARIO = r"""
import json, sys
from hermes_cli.plugins import discover_plugins, get_plugin_manager, resolve_pre_tool_block

discover_plugins()
entry = next((p for p in get_plugin_manager().list_plugins() if p["name"] == "custom_build"), None)
scenario, calls = sys.argv[1], json.loads(sys.argv[2])
if scenario == "crash":
    # The ordinary load's copy of the gate; its hook looks the function up at each call.
    loaded = [m for name, m in sys.modules.items() if name.endswith("custom_build.guards.write_gate")]
    assert len(loaded) == 1, sorted(n for n in sys.modules if "custom_build" in n)
    def crash(*args, **kwargs):
        raise KeyError("boom")
    loaded[0].find_rule = crash

def run():
    return [resolve_pre_tool_block(tool, args, session_id="s1", task_id="t1") for tool, args in calls]

if scenario == "one-shot":
    # How a `hermes -z` run marks its turn: nobody can answer, so Hermes approves without asking.
    from gateway.session_finite import finite_turn_scope
    with finite_turn_scope(True, unattended=True):
        blocks = run()
else:
    blocks = run()
print(json.dumps({"entry": entry, "blocks": blocks}))
"""


def run_gate(tmp_path, db, calls, *, mode="log", scenario="plain", approvals=None, env=None, database_url=True,
             hook_timeout=None, during=None, config_link=False):
    """Run each call through Hermes's pre_tool_call dispatch; return (report, Hermes's output, events rows).

    `during` is a context manager held while the calls run, such as a lock on memory.events. With
    `config_link`, the home's config.yaml is a link to a copy kept elsewhere, as a dotfiles checkout keeps it.
    """
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    gate_config = home / "plugins" / "custom_build" / "guards" / "write_gate.toml"
    text = gate_config.read_text(encoding="utf-8")
    assert '\nmode = "log"\n' in text
    gate_config.write_text(text.replace('\nmode = "log"\n', f'\nmode = "{mode}"\n'), encoding="utf-8")
    config = {"memory": {"provider": "custom_build"},
              "plugins": {"enabled": ["custom_build"],
                          "entries": {"custom_build": {"settings": {"machine_name": "host-a"}}}}}
    if approvals:
        config["approvals"] = approvals
    if hook_timeout:
        config["plugins"]["hook_callback_timeout"] = hook_timeout
    config_file = tmp_path / "dotfiles" / "hermes-config.yaml" if config_link else home / "config.yaml"
    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    if config_link:
        (home / "config.yaml").symlink_to(config_file)
    environment = {"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin", **(env or {})}
    if database_url:
        environment["CUSTOM_BUILD_DATABASE_URL"] = db["agent"] if database_url is True else database_url
    with during or contextlib.nullcontext():
        result = subprocess.run([sys.executable, "-c", SCENARIO, scenario, json.dumps(calls)], capture_output=True,
                                text=True, env=environment, cwd=tmp_path, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    output = result.stderr + "".join(p.read_text(encoding="utf-8") for p in sorted((home / "logs").glob("*.log")))
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, reason, detail, source, session_id from memory.events order by id").fetchall()
    return report, output, rows


def test_the_gate_ships_in_log_mode():
    assert tomllib.loads((PLUGIN / "guards" / "write_gate.toml").read_text(encoding="utf-8"))["mode"] == "log"


def test_each_rule_records_its_failing_case_and_the_call_goes_ahead(tmp_path, db):
    home = tmp_path / "home"
    skill = home / "skills" / "note-taking" / "SKILL.md"
    calls = [
        ["write_file", {"path": str(skill), "content": "x"}],
        ["patch", {"path": "home/config.yaml", "old_string": "a", "new_string": "b"}],   # relative to the cwd
        ["write_file", {"path": str(home / ".env"), "content": "X=1"}],
        ["patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: "
                   + str(home / "plugins" / "custom_build" / "extra.py") + "\n+x = 1\n*** End Patch"}],
        ["terminal", {"command": "python3 -m pip install requests"}],
        ["terminal", {"command": "systemctl restart hermes-gateway"}],
        ["terminal", {"command": "ssh other-host uptime"}],
        ["terminal", {"command": "echo x >> $HERMES_HOME/skills/note-taking/SKILL.md"}],
        ["terminal", {"command": "hermes config set approvals.mode off"}],
    ]
    report, output, rows = run_gate(tmp_path, db, calls)
    assert report["blocks"] == [None] * len(calls), output
    assert [(code, result, detail) for code, result, _, detail, _, _ in rows] == [
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"write_file", "rule": "skills"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"patch", "rule": "config"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"write_file", "rule": "env"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"patch", "rule": "plugin"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"terminal", "rule": "install"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"terminal", "rule": "systemctl"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"terminal", "rule": "other_hosts"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"terminal", "rule": "skills"}),
        ("write_gate.would_ask", "ok", {"gen_ai.tool.name":"terminal", "rule": "config"}),
    ], output
    assert {(source, session) for *_, source, session in rows} == {("host-a", "s1")}
    # The reason is what the maintainer judges a trigger by: the path Hermes would write, or the command.
    assert rows[0][2] == str(skill.resolve())
    assert rows[4][2] == "python3 -m pip install requests"


def test_an_install_from_a_lock_file_is_an_install(tmp_path, db):
    commands = ["npm ci", "uv sync", "poetry install", "bundle install", "composer install"]
    _, output, rows = run_gate(tmp_path, db, [["terminal", {"command": c}] for c in commands])
    assert [(detail["rule"], reason) for _, _, reason, detail, _, _ in rows] == [("install", c) for c in commands], output


def test_more_commands_on_the_list_are_recorded(tmp_path, db):
    commands = {
        "hermes -p default config set approvals.mode off": "config",   # a flag before the subcommand
        "hermes tools disable terminal": "config",
        "hermes mcp add filesystem": "config",
        "hermes setup": "config",
        "pnpm i left-pad": "install",
        "ssh-copy-id other-host": "other_hosts",
    }
    _, output, rows = run_gate(tmp_path, db, [["terminal", {"command": c}] for c in commands])
    assert [(reason, detail["rule"]) for _, _, reason, detail, _, _ in rows] == list(commands.items()), output


def test_a_git_verb_inside_a_commit_message_is_off_the_list(tmp_path, db):
    _, output, rows = run_gate(tmp_path, db, [["terminal", {"command": "git commit -m 'pull in the fix'"}]])
    assert rows == [], output


def test_a_command_naming_a_path_in_the_users_home_records_it(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["terminal", {"command": "rm -r ~/home/skills/note-taking"}]])
    assert report["blocks"] == [None], output
    assert [(code, detail["rule"]) for code, _, _, detail, _, _ in rows] == [("write_gate.would_ask", "skills")]


def test_a_command_naming_a_path_from_where_it_runs_records_it(tmp_path, db):
    home = tmp_path / "home"
    calls = [
        ["terminal", {"command": "echo X=1 >> .env", "workdir": str(home)}],   # relative to its working directory
        ["terminal", {"command": f"cd {home} && echo X=1 >> .env"}],   # after it changes into the Hermes home
        ["terminal", {"command": "cd $HERMES_HOME && rm -r skills/note-taking"}],
    ]
    report, output, rows = run_gate(tmp_path, db, calls)
    assert report["blocks"] == [None] * len(calls), output
    assert [(code, detail["rule"]) for code, _, _, detail, _, _ in rows] == [
        ("write_gate.would_ask", "env"), ("write_gate.would_ask", "env"), ("write_gate.would_ask", "skills"),
    ], output


def test_a_command_naming_a_linked_config_records_it(tmp_path, db):
    home = tmp_path / "home"
    calls = [["terminal", {"command": "sed -i s/a/b/ ~/home/config.yaml"}],
             ["terminal", {"command": f"sed -i s/a/b/ {home}/config.yaml"}]]
    report, output, rows = run_gate(tmp_path, db, calls, config_link=True)
    assert report["blocks"] == [None, None], output
    assert [(code, detail["rule"]) for code, _, _, detail, _, _ in rows] == [("write_gate.would_ask", "config")] * 2, output


def test_a_call_off_the_list_records_nothing(tmp_path, db):
    home = tmp_path / "home"
    calls = [
        ["write_file", {"path": str(tmp_path / "notes.txt"), "content": "x"}],
        ["write_file", {"path": str(home / "skills-notes.md"), "content": "x"}],   # beside the folder, not in it
        ["patch", {"path": str(home / "memories" / "MEMORY.md"), "old_string": "a", "new_string": "b"}],
        ["read_file", {"path": str(home / "config.yaml")}],   # reads are not on the list
        ["terminal", {"command": "ls -la"}],
        ["terminal", {"command": "git status"}],
        ["web_search", {"query": "systemctl restart"}],   # only the terminal's commands are matched
    ]
    report, output, rows = run_gate(tmp_path, db, calls)
    assert report["blocks"] == [None] * len(calls), output
    assert rows == []


@pytest.mark.parametrize("approvals, env, scenario, why", [
    ({"cron_mode": "approve"}, None, "plain", "approvals.cron_mode reads as approve"),
    ({"single_query_mode": "yes"}, None, "plain", "approvals.single_query_mode reads as approve"),
    ({"unattended_mode": "allow"}, None, "plain", "approvals.unattended_mode reads as approve"),
    ({"mode": "off"}, None, "plain", "approval bypass"),
    (None, {"HERMES_YOLO_MODE": "1"}, "plain", "yolo"),
    (None, None, "one-shot", "yolo"),
])
def test_a_call_on_would_block_outright_is_recorded_and_goes_ahead(tmp_path, db, approvals, env, scenario, why):
    calls = [["terminal", {"command": "systemctl stop hermes-gateway"}], ["terminal", {"command": "ls"}]]
    report, output, rows = run_gate(tmp_path, db, calls, approvals=approvals, env=env, scenario=scenario)
    assert report["blocks"] == [None, None], output
    assert [(code, detail) for code, _, _, detail, _, _ in rows] == [
        ("write_gate.would_block", {"gen_ai.tool.name":"terminal", "rule": "systemctl", "why": why}),
    ], output


def test_off_registers_no_hook_and_records_nothing(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["terminal", {"command": "systemctl restart x"}]], mode="off")
    assert report["entry"]["error"] is None, output
    assert report["entry"]["hooks"] == 1   # only the per-turn settings check
    assert report["blocks"] == [None]
    assert rows == []


@pytest.mark.parametrize("mode", ["on", "enforce"])
def test_a_mode_other_than_log_or_off_fails_the_load(tmp_path, db, mode):
    # `on` arrives with enforcement, in the pull request that moves the gate to it (5.4 item 6).
    report, output, _ = run_gate(tmp_path, db, [], mode=mode)
    assert report["entry"]["error"], output


def test_a_crash_in_the_gate_is_recorded_and_the_call_goes_ahead(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["terminal", {"command": "ls"}]], scenario="crash")
    assert report["blocks"] == [None], output
    assert [(code, result, reason, source, session) for code, result, reason, _, source, session in rows] == [
        ("guard.crash", "failed", "KeyError: 'boom'", "host-a", "s1"),
    ]


@pytest.mark.parametrize("env", [None, {"HERMES_REDACT_SECRETS": "false"}])
def test_the_recorded_command_is_redacted_even_with_hermes_redaction_off(tmp_path, db, env):
    calls = [["terminal", {"command": f"ssh other-host 'export KEY={FAKE_SECRET}'"}],
             ["terminal", {"command": f"wget 'https://example.com/file?token={URL_TOKEN}'"}]]
    _, output, rows = run_gate(tmp_path, db, calls, env=env)
    reasons = [reason for _, _, reason, *_ in rows]
    assert len(reasons) == 2, output
    assert FAKE_SECRET not in reasons[0] and "ssh other-host" in reasons[0]
    assert URL_TOKEN not in reasons[1] and "wget" in reasons[1]


def test_an_unreachable_store_is_logged_and_the_call_goes_ahead(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["terminal", {"command": "systemctl restart x"}]],
                                    database_url="postgresql://u:p@localhost:1/x")
    assert report["blocks"] == [None], output
    assert rows == []
    assert output.index('"code": "db.unreachable"') < output.index('"lost_code": "write_gate.would_ask"'), output


@contextlib.contextmanager
def events_locked(db):
    """Hold a lock on memory.events, which stands in for a stalled store, until the calls have run."""
    with psycopg.connect(db["owner"]) as holder:
        holder.execute("lock table memory.events in access exclusive mode")
        yield
        holder.rollback()


def test_a_stalled_store_is_logged_and_the_call_goes_ahead(tmp_path, db):
    # Hermes blocks a call whose pre_tool_call hook runs past its deadline, then every call for 60 seconds;
    # a guard keeps its whole run under 10 seconds (ARCHITECTURE.md 5.5).
    calls = [["terminal", {"command": "systemctl restart x"}], ["terminal", {"command": "ls"}]]
    report, output, rows = run_gate(tmp_path, db, calls, hook_timeout=10, during=events_locked(db))
    assert report["blocks"] == [None, None], output
    assert rows == []
    assert '"lost_code": "write_gate.would_ask"' in output, output
