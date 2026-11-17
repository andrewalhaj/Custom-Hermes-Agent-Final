"""Prove the fact gate in log mode (issue #133, ARCHITECTURE.md sections 5.3 and 5.4).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own, runs Hermes
v0.23.1's ordinary plugin discovery, then each step through the function Hermes itself uses:
`_collect_pre_llm_call_context` for the user's message, `_emit_post_tool_call_hook` after a tool call,
and `apply_llm_output_transform` on the reply. In log mode the gate adds nothing to the turn and
withholds nothing; it records an armed turn, and a reply that would have been withheld.
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
if scenario == "crash":
    loaded = [m for name, m in sys.modules.items() if name.endswith("custom_build.guards.fact_gate")]
    assert len(loaded) == 1, sorted(n for n in sys.modules if "custom_build" in n)
    def crash(*args, **kwargs):
        raise KeyError("boom")
    loaded[0].armed_by = crash

def settle():
    for thread in threading.enumerate():
        if thread.name.startswith("custom_build."):
            thread.join(30)

answers = []
for kind, session, turn, value, *rest in steps:
    platform = rest[0] if kind == "turn" and rest else "cli"   # Hermes builds a subagent with platform="subagent"
    agent = types.SimpleNamespace(session_id=session, model="m", platform=platform, _persist_disabled=False)
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
print(json.dumps({"entry": entry, "answers": answers}))
"""


def run_gate(tmp_path, db, steps, *, mode="log", scenario="plain"):
    """Run the steps; return (report, Hermes's output, events rows). A step is ["turn", session, turn,
    message] (a fifth item sets the agent's platform), ["post", session, turn, tool, status] or
    ["reply", session, turn, text]."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    gate_config = home / "plugins" / "custom_build" / "guards" / "fact_gate.toml"
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
                            " where code like 'fact_gate.%' or code = 'guard.crash' order by id").fetchall()
    return report, output, rows


def events(rows):
    return [(code, detail.get("rule"), detail.get("why")) for code, _, _, detail, _, _ in rows]


def test_the_gate_ships_in_log_mode():
    assert tomllib.loads((PLUGIN / "guards" / "fact_gate.toml").read_text(encoding="utf-8"))["mode"] == "log"


def test_each_checkable_question_arms_its_turn_and_adds_nothing_to_it(tmp_path, db):
    questions = {
        "version": "Which version of Postgres does Supabase run?",
        "state": "Is the gateway running on the mini?",
        "value": "What is the current value of approvals.mode?",
        "count": "How many facts are in the store?",
        "location": "Where is the plugin installed?",
        "support": "Does pgvector support halfvec indexes?",
        "date": "When was Hermes v0.23.1 released?",
        "error": "Why does the startup check fail with config.unsafe?",
    }
    steps = [["turn", "s1", f"u{n}", message] for n, message in enumerate(questions.values())]
    report, output, rows = run_gate(tmp_path, db, steps)
    assert report["answers"] == [["", []]] * len(steps), output   # no reminder, no block in log mode
    assert events(rows) == [("fact_gate.armed", rule, None) for rule in questions], output
    assert {(row[1], row[4], row[5]) for row in rows} == {("ok", "host-a", "s1")}
    assert not any(message in json.dumps(rows) for message in questions.values())   # no message text stored (L5)


def test_an_armed_turn_answered_without_a_lookup_is_recorded_and_delivered(tmp_path, db):
    ask = "How many facts are in the store?"
    steps = [
        ["turn", "s1", "u1", ask], ["reply", "s1", "u1", "There are 42."],   # no lookup
        ["turn", "s1", "u2", ask], ["post", "s1", "u2", "terminal", "ok"], ["reply", "s1", "u2", "There are 42."],
        ["turn", "s1", "u3", ask], ["post", "s1", "u3", "terminal", "error"], ["reply", "s1", "u3", "There are 42."],
        ["turn", "s1", "u4", ask], ["post", "s1", "u3", "terminal", "ok"], ["reply", "s1", "u4", "There are 42."],   # another turn's
        ["turn", "s1", "u5", ask], ["post", "s1", "u5", "todo_list", "ok"], ["reply", "s1", "u5", "There are 42."],   # not a lookup
    ]
    report, output, rows = run_gate(tmp_path, db, steps)
    assert all(answer in (None, ["", []]) for answer in report["answers"]), output   # no reply is rewritten
    withheld = [(rule, why) for code, rule, why in events(rows) if code == "fact_gate.would_withhold"]
    assert withheld == [("count", "no lookup")] * 4, output


def test_a_procedure_or_diagnosis_without_a_documentation_lookup_is_recorded(tmp_path, db):
    ask = "Why does the startup check fail with config.unsafe?"
    procedure = "To fix it:\n1. Open config.yaml.\n2. Set approvals.mode to manual."
    diagnosis = "The root cause is approvals.mode reading as off."
    steps = [
        ["turn", "s1", "u1", ask], ["post", "s1", "u1", "terminal", "ok"], ["reply", "s1", "u1", procedure],
        ["turn", "s1", "u2", ask], ["post", "s1", "u2", "terminal", "ok"], ["reply", "s1", "u2", diagnosis],
        ["turn", "s1", "u3", ask], ["post", "s1", "u3", "web_search", "ok"], ["reply", "s1", "u3", diagnosis],
    ]
    _, output, rows = run_gate(tmp_path, db, steps)
    withheld = [(rule, why) for code, rule, why in events(rows) if code == "fact_gate.would_withhold"]
    assert withheld == [("procedure", "no documentation lookup"), ("diagnosis", "no documentation lookup")], output


def test_a_turn_that_asks_nothing_checkable_records_nothing(tmp_path, db):
    steps = [["turn", "s1", "u1", "Thanks, that worked."],
             ["reply", "s1", "u1", "Glad it did. The root cause was the timeout:\n1. a\n2. b"]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output


def test_a_subagents_turn_arms_nothing(tmp_path, db):
    # #188: a subagent's goal is its parent's words, not a user's question; the parent's turn is the one judged.
    steps = [["turn", "s2", "u1", "How many facts are in the store?", "subagent"], ["reply", "s2", "u1", "42."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert rows == [], output


def test_each_event_carries_its_turns_id_and_every_judged_turn_ends_in_a_row(tmp_path, db):
    # #188: an armed row joins to its turn's later row by the id; an armed row with none means no reply came.
    ask = "How many facts are in the store?"
    steps = [["turn", "s1", "u1", ask], ["reply", "s1", "u1", "There are 42."],
             ["turn", "s1", "u2", ask, "telegram"], ["post", "s1", "u2", "terminal", "ok"],   # any platform but a subagent's
             ["reply", "s1", "u2", "The root cause is the timeout."],
             ["turn", "s1", "u3", ask], ["post", "s1", "u3", "terminal", "ok"], ["reply", "s1", "u3", "There are 42."],
             ["turn", "s1", "u4", ask]]   # no reply
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [(code, detail["turn_id"], detail.get("why")) for code, _, _, detail, _, _ in rows] == [
        ("fact_gate.armed", "u1", None), ("fact_gate.would_withhold", "u1", "no lookup"),
        ("fact_gate.armed", "u2", None), ("fact_gate.would_withhold", "u2", "no documentation lookup"),
        ("fact_gate.armed", "u3", None), ("fact_gate.passed", "u3", None),
        ("fact_gate.armed", "u4", None)], output


def test_off_registers_no_hook_and_records_nothing(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["turn", "s1", "u1", "How many facts are there?"]], mode="off")
    assert report["entry"]["error"] is None, output
    assert report["entry"]["hooks"] == 8   # the settings check, the write gate, and the verify and recall gates' three each
    assert rows == []


@pytest.mark.parametrize("mode", ["on", "enforce"])
def test_a_mode_other_than_log_or_off_fails_the_load(tmp_path, db, mode):
    report, output, _ = run_gate(tmp_path, db, [], mode=mode)
    assert report["entry"]["error"], output


def test_a_crash_in_the_gate_is_recorded_and_the_turn_goes_ahead(tmp_path, db):
    report, output, rows = run_gate(tmp_path, db, [["turn", "s1", "u1", "How many facts are there?"]], scenario="crash")
    assert report["answers"] == [["", []]], output
    assert [(code, reason, session) for code, _, reason, _, _, session in rows] == [
        ("guard.crash", "KeyError: 'boom'", "s1")], output


def test_a_turn_keeps_its_count_when_compression_gives_the_session_a_new_id(tmp_path, db):
    # Hermes's compression can rotate agent.session_id mid-turn; the turn's id stays the same.
    steps = [["turn", "s1", "u1", "How many facts are in the store?"], ["post", "s1-rotated", "u1", "terminal", "ok"],
             ["reply", "s1-rotated", "u1", "There are 42."],
             ["turn", "s1", "u2", "How many facts are in the store?"], ["reply", "s1-rotated", "u2", "There are 42."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [(code, rule, why) for code, rule, why in events(rows) if code == "fact_gate.would_withhold"] == [
        ("fact_gate.would_withhold", "count", "no lookup")], output


def test_a_turn_is_judged_once_and_its_reply_text_is_never_stored(tmp_path, db):
    reply = "There are exactly 4242 facts."
    steps = [["turn", "s1", "u1", "How many facts are in the store?"], ["reply", "s1", "u1", reply],
             ["reply", "s1", "u1", reply]]   # a second call for the same turn finds it already judged
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [code for code, _, _ in events(rows)] == ["fact_gate.armed", "fact_gate.would_withhold"], output
    assert "4242" not in json.dumps(rows)


def test_only_the_1000_most_recent_armed_turns_are_held(tmp_path, db):
    ask = "How many facts are in the store?"
    steps = [["turn", "s1", f"u{n}", ask] for n in range(1001)] + [["reply", "s1", "u0", "42."], ["reply", "s1", "u1000", "42."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [code for code, _, _ in events(rows)].count("fact_gate.would_withhold") == 1, output   # u0 was dropped


def test_common_phrasings_of_a_checkable_question_arm_the_turn(tmp_path, db):
    questions = {
        "what's the default timeout for hooks?": "value",
        "Which python version do I have?": "version",
        "Does Postgres 17 support MERGE?": "support",
        "is the docker daemon running": "state",
        "Is port 5432 open?": "state",
        "Why do I keep getting these errors?": "error",
    }
    steps = [["turn", "s1", f"u{n}", message] for n, message in enumerate(questions)]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [rule for _, rule, _ in events(rows)] == list(questions.values()), output


def test_a_turn_that_delegates_its_lookup_counts_as_looked_up(tmp_path, db):
    # A subagent's own tool calls carry its session and turn, so the parent sees only delegate_task.
    steps = [["turn", "s1", "u1", "How many facts are in the store?"], ["post", "s1", "u1", "delegate_task", "ok"],
             ["reply", "s1", "u1", "There are 42."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [code for code, _, _ in events(rows)] == ["fact_gate.armed", "fact_gate.passed"], output


def test_a_turn_that_searches_memory_counts_as_looked_up(tmp_path, db):
    # ARCHITECTURE.md 5.3: the memory search tool joins the lookups when it ships (#158).
    steps = [["turn", "s1", "u1", "How many facts are in the store?"], ["post", "s1", "u1", "memory_search", "ok"],
             ["reply", "s1", "u1", "There are 42."]]
    _, output, rows = run_gate(tmp_path, db, steps)
    assert [code for code, _, _ in events(rows)] == ["fact_gate.armed", "fact_gate.passed"], output


def test_a_long_message_is_matched_in_linear_time():
    # A pasted log reaches pre_llm_call whole; a pattern that rescans it at every position holds the GIL.
    import time

    from custom_build.guards import fact_gate

    config = fact_gate.load_config()
    for message in ("a " * 100_000 + "?", "x" * 200_000):
        started = time.monotonic()
        fact_gate.armed_by(config, message)
        assert time.monotonic() - started < 1, len(message)


def test_is_or_does_inside_a_sentence_does_not_arm_the_turn(tmp_path, db):
    statements = ["That is entirely up to you.", "My mind is all set, thanks",
                  "The function is not running fast enough, can you optimise it", "Your argument does not support that."]
    _, output, rows = run_gate(tmp_path, db, [["turn", "s1", f"u{n}", text] for n, text in enumerate(statements)])
    assert rows == [], output


def test_a_long_reply_is_matched_in_linear_time():
    # A reply reaches transform_llm_output whole, and Hermes waits on that hook.
    import time

    from custom_build.guards import fact_gate

    config = fact_gate.load_config()
    for name, pattern in config["needs_documentation"].items():
        for reply in ("\n" * 100_000, "\n " * 50_000, "a " * 100_000):
            started = time.monotonic()
            pattern.search(reply)
            assert time.monotonic() - started < 1, (name, len(reply))
