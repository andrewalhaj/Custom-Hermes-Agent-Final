"""Prove the recall gate in log mode (issue #173, ARCHITECTURE.md sections 5.3 and 5.4).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own, runs Hermes
v0.23.1's ordinary plugin discovery, then each step through the function Hermes itself uses:
`_collect_pre_llm_call_context` for the user's message, `_emit_post_tool_call_hook` after a tool call,
and `apply_llm_output_transform` on the reply. In log mode the gate adds nothing to the turn and
withholds nothing; it records a turn that asks about past work, and its reply when the turn ran without
both the memory search and the session search.
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
import json, sys, threading, types
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from agent.turn_context import _collect_pre_llm_call_context
from model_tools import _emit_post_tool_call_hook
from agent.turn_finalizer import apply_llm_output_transform

discover_plugins()
entry = next((p for p in get_plugin_manager().list_plugins() if p["name"] == "custom_build"), None)
scenario, steps = sys.argv[1], json.loads(sys.argv[2])
if scenario != "plain":
    loaded = [m for name, m in sys.modules.items() if name.endswith("custom_build.guards.recall_gate")]
    assert len(loaded) == 1, sorted(n for n in sys.modules if "custom_build" in n)
    def crash(*args, **kwargs):
        raise KeyError("boom")
    setattr(loaded[0], scenario, crash)   # the scenario names the function that raises

def settle():
    for thread in threading.enumerate():
        if thread.name.startswith("custom_build."):
            thread.join(30)

answers = []
for kind, session, turn, value, *rest in steps:
    agent = types.SimpleNamespace(session_id=session, model="m", platform="cli", _persist_disabled=False)
    if kind == "turn":
        blocks = []
        context = _collect_pre_llm_call_context(agent, effective_task_id="t1", turn_id=turn, original_user_message=value,
                                                messages=[], conversation_history=[], blocks=blocks)
        answers.append([context, blocks])
    elif kind == "post":
        _emit_post_tool_call_hook(function_name=value, function_args={}, result="{}", task_id="t1",
                                  session_id=session, turn_id=turn, status=rest[0])
        answers.append(None)
    else:
        reply, transformed, _ = apply_llm_output_transform(agent, value, turn_id=turn)
        answers.append(reply if transformed else None)
    settle()
print(json.dumps({"entry": entry, "answers": answers, "holds_stream": get_plugin_manager().holds_stream()}))
"""


def run_gate(tmp_path, db, steps, *, mode="log", scenario="plain"):
    """Run the steps; return (report, Hermes's output, events rows). A step is ["turn", session, turn,
    message], ["post", session, turn, tool, status] or ["reply", session, turn, text]."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    gate_config = home / "plugins" / "custom_build" / "guards" / "recall_gate.toml"
    text = gate_config.read_text(encoding="utf-8")
    assert '\nmode = "log"\n' in text
    gate_config.write_text(text.replace('\nmode = "log"\n', f'\nmode = "{mode}"\n'), encoding="utf-8")
    config = {"memory": {"provider": "custom_build"},
              "plugins": {"enabled": ["custom_build"],
                          "entries": {"custom_build": {"settings": {"machine_name": "host-a", "judge_model": "jev-1.14.0"}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    environment = {"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                   "CUSTOM_BUILD_DATABASE_URL": db["agent"],
                   "TYPESAFE_API_KEY": "test-key-not-a-real-one"}   # both settings the per-turn check needs (#132)
    result = subprocess.run([sys.executable, "-c", SCENARIO, scenario, json.dumps(steps)], capture_output=True,
                            text=True, env=environment, cwd=tmp_path, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    output = result.stderr + "".join(p.read_text(encoding="utf-8") for p in sorted((home / "logs").glob("*.log")))
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, reason, detail, source, session_id from memory.events"
                            " where code like 'recall_gate.%' or code = 'guard.crash' order by id").fetchall()
    return report, output, rows


def events(rows):
    return [(code, detail.get("kind"), detail.get("rule"), detail.get("why")) for code, _, _, detail, _, _ in rows]


def withheld(rows):
    return [why for code, _, _, why in events(rows) if code == "recall_gate.would_withhold"]


ASK = "Did we already fix the gateway?"


def test_the_gate_ships_in_log_mode():
    config = tomllib.loads((PLUGIN / "guards" / "recall_gate.toml").read_text(encoding="utf-8"))
    assert (config["mode"], config["searches"]) == ("log", ["memory_search", "session_search"])


def test_a_question_about_past_or_open_work_arms_its_turn_and_adds_nothing_to_it(tmp_path, db):
    questions = {
        ASK: ("past_work", "did_we"),
        "What did we decide last time about the store?": ("past_work", "what_did"),
        "Remind me what we did on the backup.": ("past_work", "remind_me"),
        "What is left to do?": ("open_work", "left"),
        "what's next": ("open_work", "next"),
    }
    steps = [["turn", "s1", f"u{n}", message] for n, message in enumerate(questions)]
    report, output, rows = run_gate(tmp_path, db, steps)
    assert report["answers"] == [["", []]] * len(steps), output   # no reminder, no block in log mode
    assert events(rows) == [("recall_gate.armed", kind, rule, None) for kind, rule in questions.values()], output
    assert {(row[1], row[4], row[5]) for row in rows} == {("ok", "host-a", "s1")}
    assert not any(message in json.dumps(rows) for message in questions)   # no message text stored (L5)


def test_an_armed_turn_without_both_searches_is_recorded_and_delivered(tmp_path, db):
    memory, session, reply = "memory_search", "session_search", "We fixed it on Tuesday."
    steps = [
        ["turn", "s1", "u1", ASK], ["reply", "s1", "u1", reply],   # neither search
        ["turn", "s1", "u2", ASK], ["post", "s1", "u2", memory, "ok"], ["reply", "s1", "u2", reply],   # one alone
        ["turn", "s1", "u3", ASK], ["post", "s1", "u3", session, "ok"], ["reply", "s1", "u3", reply],   # the other alone
        ["turn", "s1", "u4", ASK], ["post", "s1", "u4", memory, "error"], ["post", "s1", "u4", session, "ok"],
        ["reply", "s1", "u4", reply],   # a search that failed does not count
        ["turn", "s1", "u5", ASK], ["post", "s1", "u4", memory, "ok"], ["post", "s1", "u4", session, "ok"],
        ["reply", "s1", "u5", reply],   # another turn's searches
        ["turn", "s1", "u6", ASK], ["post", "s1", "u6", "terminal", "ok"], ["post", "s1", "u6", "delegate_task", "ok"],
        ["reply", "s1", "u6", reply],   # neither is one of the two searches
        ["turn", "s1", "u7", ASK], ["post", "s1", "u7", memory, "blocked"], ["post", "s1", "u7", session, "timeout"],
        ["reply", "s1", "u7", reply],   # 5.3: any status other than ok does not count
    ]
    report, output, rows = run_gate(tmp_path, db, steps)
    assert all(answer in (None, ["", []]) for answer in report["answers"]), output   # no reply is rewritten
    both = "no memory_search or session_search"
    assert withheld(rows) == [both, "no session_search", "no memory_search", "no memory_search", both, both, both], output
    assert report["holds_stream"] is False   # log mode holds no reply back from the stream


def test_an_armed_turn_that_ran_both_searches_records_only_that_it_was_armed(tmp_path, db):
    steps = [["turn", "s1", "u1", ASK], ["post", "s1", "u1", "session_search", "ok"],
             ["post", "s1", "u1", "memory_search", "ok"], ["reply", "s1", "u1", "We fixed it on Tuesday."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [code for code, *_ in events(rows)] == ["recall_gate.armed"], output


def test_a_turn_that_does_not_ask_about_past_work_records_nothing(tmp_path, db):
    steps = [["turn", "s1", "u1", "Did you set the env var?"], ["reply", "s1", "u1", "Yes, in .env."],
             ["turn", "s1", "u2", [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]],
             ["reply", "s1", "u2", "It is a screenshot."]]   # a message with no text
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output


def test_a_message_given_as_parts_is_read_as_its_text_as_recall_reads_it(tmp_path, db):
    # Hermes hands prefetch() the flattened text of a message with an image, so recall widens on it;
    # the gate must arm on the same text, or the search and the gate disagree (#173 review).
    parts = [{"type": "text", "text": ASK}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    _, output, rows = run_gate(tmp_path, db, [["turn", "s1", "u1", parts]])
    assert events(rows) == [("recall_gate.armed", "past_work", "did_we", None)], output


def test_off_registers_no_hook_and_records_nothing(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["turn", "s1", "u1", ASK]], mode="off")
    assert report["entry"]["error"] is None, output
    assert report["entry"]["hooks"] == 8   # the settings check, the write gate, and the verify and fact gates' three each
    assert rows == []


@pytest.mark.parametrize("mode", ["on", "enforce"])
def test_a_mode_other_than_log_or_off_fails_the_load(tmp_path, db, mode):
    report, output, _ = run_gate(tmp_path, db, [], mode=mode)
    assert report["entry"]["error"], output


@pytest.mark.parametrize(("raises", "then"), [
    ("arm", []), ("write_event", []),   # the first hook, and the worker that writes its event
    ("count", [["post", "s1", "u1", "memory_search", "ok"]]), ("check", [["reply", "s1", "u1", "Yes."]]),
])
def test_a_crash_in_any_hook_or_the_worker_is_recorded_and_the_turn_goes_ahead(tmp_path, db, raises, then):
    report, output, rows = run_gate(tmp_path, db, [["turn", "s1", "u1", ASK], *then], scenario=raises)
    assert all(answer in (None, ["", []]) for answer in report["answers"]), output
    crashes = [(reason, session) for code, _, reason, _, _, session in rows if code == "guard.crash"]
    assert crashes == [("KeyError: 'boom'", "s1")], output


def test_a_turn_keeps_its_count_when_compression_gives_the_session_a_new_id(tmp_path, db):
    # Hermes's compression can rotate agent.session_id mid-turn; the turn's id stays the same.
    steps = [["turn", "s1", "u1", ASK], ["post", "s1-rotated", "u1", "memory_search", "ok"],
             ["post", "s1-rotated", "u1", "session_search", "ok"], ["reply", "s1-rotated", "u1", "Yes."],
             ["turn", "s1", "u2", ASK], ["reply", "s1-rotated", "u2", "Yes."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert withheld(rows) == ["no memory_search or session_search"], output


def test_a_turn_is_judged_once_and_its_reply_text_is_never_stored(tmp_path, db):
    reply = "We fixed it in commit 4242."
    steps = [["turn", "s1", "u1", ASK], ["reply", "s1", "u1", reply],
             ["reply", "s1", "u1", reply]]   # a second call for the same turn finds it already judged
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [code for code, *_ in events(rows)] == ["recall_gate.armed", "recall_gate.would_withhold"], output
    assert rows[1][1:] == ("ok", None, {"kind": "past_work", "rule": "did_we", "why": "no memory_search or session_search"},
                           "host-a", "s1"), output   # the pattern's name is what #163's counts need
    assert "4242" not in json.dumps(rows)


def test_only_the_1000_most_recent_armed_turns_are_held(tmp_path, db):
    steps = [["turn", "s1", f"u{n}", ASK] for n in range(1001)] + [
        ["post", "s1", "u1000", "memory_search", "ok"], ["reply", "s1", "u0", "Yes."], ["reply", "s1", "u1000", "Yes."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert withheld(rows) == ["no session_search"], output   # u0, the oldest, was dropped; u1000 kept its count
