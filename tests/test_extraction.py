"""Prove extraction (issue #148, ARCHITECTURE.md 3.3, the third entrance).

The unit tests replace Hermes's `call_llm` in this process. The end-to-end tests copy the plugin into a
throwaway Hermes home and, in a process of their own, load the provider through Hermes v0.23.1's loader
and call sync_turn() as Hermes's background worker does after a turn; there `call_llm` is replaced by a
fake that answers as the scenario says and writes down what it was sent.
"""

import contextlib
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import types

import psycopg
import pytest
import yaml
from agent import auxiliary_client

from custom_build.memory import extraction

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
FACT = "The mini's gateway runs as the hermes user."
FAKE_KEY = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"   # assembled at run time, never a literal key
REPLY = "The gateway runs as root, so restart it as root."         # the agent's reply: never sent
UNIT = "User=hermes\nExecStart=/usr/bin/hermes gateway"
TURN = [
    {"role": "user", "content": "An earlier question."},
    {"role": "assistant", "content": "An earlier reply."},
    {"role": "tool", "tool_call_id": "a", "content": "earlier output"},
    {"role": "user", "content": "Why did the gateway fail?"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "b", "type": "function",
                                                         "function": {"name": "read_file", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "b", "content": UNIT},
    {"role": "assistant", "content": REPLY},
]


def _answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class Refusal(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_the_source_is_the_users_message_and_this_turns_tool_results_never_a_reply():
    assert extraction.source("Why did the gateway fail?", TURN) == {
        "user": "Why did the gateway fail?", "tool_results": [UNIT]}


def test_the_call_goes_to_the_build_task_without_the_agents_replies(monkeypatch):
    calls = []
    monkeypatch.setattr(auxiliary_client, "call_llm",
                        lambda **kw: calls.append(kw) or _answer(json.dumps([{"body": FACT, "basis": "tool_observed"}])))
    proposals = extraction.propose({"user": "Why did the gateway fail?", "tool_results": [UNIT]})
    assert proposals == [(FACT, "tool_observed")]
    [call] = calls
    assert call["task"] == "custom_build_extraction"
    assert REPLY not in json.dumps(call["messages"])


@pytest.mark.parametrize("text", ["not json", json.dumps({"body": FACT}), json.dumps([{"body": FACT, "basis": "guessed"}]),
                                  json.dumps([{"basis": "user_stated"}]), ""])
def test_an_answer_that_is_not_a_list_of_facts_is_an_extraction_error(monkeypatch, text):
    monkeypatch.setattr(auxiliary_client, "call_llm", lambda **kw: _answer(text))
    with pytest.raises(extraction.ExtractError):
        extraction.propose({"user": "u", "tool_results": []})


@pytest.mark.parametrize("status, error", [(401, "ExtractRefused"), (402, "ExtractRefused"), (403, "ExtractRefused"),
                                           (429, "ExtractError"), (503, "ExtractError"), (None, "ExtractError")])
def test_a_refusal_for_the_account_is_told_apart_from_other_failures(monkeypatch, status, error):
    def fail(**kw):
        raise Refusal(status)
    monkeypatch.setattr(auxiliary_client, "call_llm", fail)
    with pytest.raises(getattr(extraction, error)):
        extraction.propose({"user": "u", "tool_results": []})


SCENARIO = r"""
import json, logging, sys
import plugins.memory
from agent import auxiliary_client

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
ollama_url, judge_url, answer, sent_path, context = sys.argv[1:6]
user, messages = json.loads(sys.argv[6]), json.loads(sys.argv[7])

class Refusal(RuntimeError):
    status_code = 403

def fake_call_llm(**kwargs):
    with open(sent_path, "a") as f:
        f.write(json.dumps(kwargs["messages"]) + "\n")
    if answer == "refused":
        raise Refusal("Claude Code refused: this account is restricted")
    return {"choices": [{"message": {"role": "assistant", "content": answer}}]}

auxiliary_client.call_llm = fake_call_llm
provider = plugins.memory.load_memory_provider("custom_build")
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = ollama_url
modules["judge"].URL = judge_url
provider.initialize(session_id="s1", hermes_home=sys.argv[8], platform="cli", agent_context=context)
provider.on_turn_start(4, user)
provider.sync_turn(user, "The reply.", session_id="s1", messages=messages)
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


def turn(home, db, ollama_url, judge_url, answer, *, user="Why did the gateway fail?", messages=TURN,
         context="primary", env_extra=None):
    """Start the provider and sync one turn; return (what call_llm was sent, log output)."""
    sent = home.parent / "sent.jsonl"
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "CUSTOM_BUILD_DATABASE_URL": db["agent"], "TYPESAFE_API_KEY": "test-key-not-a-real-one", **(env_extra or {})}
    result = subprocess.run(
        [sys.executable, "-c", SCENARIO, ollama_url, judge_url, answer, str(sent), context, json.dumps(user),
         json.dumps(messages), str(home)],
        capture_output=True, text=True, env=env, cwd=home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return (sent.read_text() if sent.exists() else ""), result.stderr


def _facts(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select body, origin, basis, project, source, session_id, source_turn from memory.facts"
                            " order by created_at").fetchall()


def _codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select code from memory.events order by id")]


ONE_FACT = json.dumps([{"body": FACT, "basis": "tool_observed"}])


def test_a_fact_the_judge_keeps_is_stored_as_extracted(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.9, "s0": 0.9, "s1": 0.9}
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    assert _facts(db) == [(FACT, "extracted", "tool_observed", "github.com/Owner/Repo", "host-a", "s1", 4)]
    [gate] = typesafe.requests
    assert gate["body"]["state"] == {"fact": FACT}
    assert UNIT in gate["body"]["questions"]["s1"]["instructions"]   # the tool result, in a question of its own (#251)


def test_a_fact_the_judge_does_not_keep_is_dropped(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.9, "s0": 0.3, "s1": 0.3}
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    assert _facts(db) == []
    assert _codes(db) == []


def test_a_refused_account_is_recorded_as_extract_refused(home, db, ollama, typesafe):
    turn(home, db, ollama.url, typesafe.url, "refused")
    assert _facts(db) == []
    assert _codes(db) == ["extract.refused"]


def test_a_malformed_answer_is_recorded_as_extract_error(home, db, ollama, typesafe):
    turn(home, db, ollama.url, typesafe.url, "Sure! Here are the facts: none.")
    assert _codes(db) == ["extract.error"]


def test_a_judge_failure_queues_the_fact_and_the_retry_still_gates_it(home, db, ollama, typesafe):
    typesafe.status = 500
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    assert _facts(db) == []
    assert _codes(db) == ["judge.error", "store.gate_error"]
    typesafe.status, typesafe.answers = 200, {"worth_keeping": 0.9, "s0": 0.2, "s1": 0.2}
    turn(home, db, ollama.url, typesafe.url, "[]")   # the next start retries the queue
    assert _facts(db) == []   # the retry asked the gate, which refused it
    assert typesafe.requests[-1]["body"]["state"]["fact"] == FACT
    with psycopg.connect(db["owner"]) as conn:   # #148 review: recorded as dropped, not as resolved
        assert conn.execute("select code, result, reason from memory.events order by id desc limit 1").fetchone() == (
            "store.gate_error", "degraded", "dropped on retry: the judge did not keep it")


def _queued(home):
    with contextlib.closing(sqlite3.connect(home / "plugin-data" / "custom_build" / "store_queue.db")) as queue:
        return queue.execute("select count(*) from queued").fetchone()[0]


TWO_FACTS = json.dumps([{"body": FACT, "basis": "tool_observed"},
                        {"body": "The gateway's unit starts hermes gateway.", "basis": "tool_observed"}])


def test_a_request_the_judge_refuses_is_recorded_with_its_cause_and_not_queued(home, db, ollama, typesafe):
    # #251: the same request gets the same refusal, so a retry cannot fix it.
    typesafe.status, typesafe.detail = 400, {"error_type": "max_tokens_exceeded"}
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    assert _facts(db) == [] and _queued(home) == 0
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, reason from memory.events order by id").fetchall()
    assert [row[:2] for row in rows] == [("judge.error", "failed"), ("store.gate_error", "failed")]
    assert "max_tokens_exceeded" in rows[0][2] and rows[1][2].endswith("the fact is dropped")


def test_a_400_that_does_not_name_the_request_as_too_big_still_queues_the_fact(home, db, ollama, typesafe):
    typesafe.status = 400   # the fake's plain "refused": not a cause known to repeat
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    assert _queued(home) == 1


def test_queued_facts_the_judge_refuses_leave_the_queue_in_one_pass(home, db, ollama, typesafe):
    typesafe.status = 500
    turn(home, db, ollama.url, typesafe.url, TWO_FACTS)
    assert _queued(home) == 2
    typesafe.status, typesafe.detail = 400, {"error_type": "max_tokens_exceeded"}
    asked = len(typesafe.requests)
    turn(home, db, ollama.url, typesafe.url, "[]")   # the next start retries the queue
    assert _queued(home) == 0 and len(typesafe.requests) == asked + 2   # both tried: a refusal does not end the pass
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select code, result, reason from memory.events order by id desc limit 1").fetchone() == (
            "store.gate_error", "failed", "dropped on retry: the judge refused the request")


def test_a_fact_already_stored_does_not_ask_the_judge_again(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.9, "s0": 0.9, "s1": 0.9}
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    turn(home, db, ollama.url, typesafe.url, ONE_FACT)
    assert len(typesafe.requests) == 1   # the second proposal is the same text, already live
    assert len(_facts(db)) == 1


def test_nothing_is_extracted_outside_a_primary_session(home, db, ollama, typesafe):
    sent, _ = turn(home, db, ollama.url, typesafe.url, ONE_FACT, context="subagent")
    assert sent == ""
    assert _facts(db) == []


def test_a_secret_in_the_turn_never_reaches_the_model_the_judge_the_store_or_a_log_line(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.9, "s0": 0.9, "s1": 0.9}
    user = f"The store key is {FAKE_KEY}, why does it fail?"
    messages = [{"role": "user", "content": user}, {"role": "tool", "tool_call_id": "c", "content": f"key={FAKE_KEY}"}]
    answer = json.dumps([{"body": f"The store key is {FAKE_KEY}.", "basis": "user_stated"}])
    sent, output = turn(home, db, ollama.url, typesafe.url, answer, user=user, messages=messages,
                        env_extra={"HERMES_REDACT_SECRETS": "false"})   # the build's redaction is forced
    assert sent and FAKE_KEY not in sent
    assert all(FAKE_KEY not in json.dumps(r["body"]) for r in typesafe.requests)
    assert all(FAKE_KEY not in row[0] for row in _facts(db))
    assert FAKE_KEY not in output


class ProviderError(RuntimeError):
    """Shaped like the subscription provider's ClaudeAPIError (directsdk.py at 6313fad): a status_code attribute."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class FakeClient:
    """An OpenAI-shaped client: raises `error`, or answers `text`."""

    def __init__(self, error=None, text="[]"):
        self.error, self.text, self.base_url, self.api_key, self.calls = error, text, "", "placeholder", 0
        client = self

        class Completions:
            @staticmethod
            def create(**kwargs):
                client.calls += 1
                if client.error:
                    raise client.error
                message = types.SimpleNamespace(content=client.text, tool_calls=None)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
                                             usage=None, model="m")

        self.chat = types.SimpleNamespace(completions=Completions)


@pytest.fixture
def ladder(monkeypatch):
    """Hermes's real call_llm, with only the clients replaced: the task's provider fails as told, and the main
    agent model, on another provider, answers with a fact."""
    def route(error):
        primary, main = FakeClient(error), FakeClient(text=ONE_FACT)
        monkeypatch.setattr(auxiliary_client, "_resolve_task_provider_model",
                            lambda *a, **k: (extraction.DEFAULTS["provider"], "haiku", None, None, None))
        monkeypatch.setattr(auxiliary_client, "_read_main_provider", lambda: "openrouter")
        monkeypatch.setattr(auxiliary_client, "_read_main_model", lambda: "some/model")
        monkeypatch.setattr(auxiliary_client, "_get_cached_client", lambda *a, **k: (primary, "haiku"))
        monkeypatch.setattr(auxiliary_client, "resolve_provider_client", lambda provider=None, model=None, **k: (main, model))
        monkeypatch.setattr(auxiliary_client.time, "sleep", lambda seconds: None)
        return main
    return route


@pytest.mark.parametrize("error, raised", [
    (ProviderError("billing_error: usage credits required", 402), extraction.ExtractRefused),
    (ProviderError("You've hit your session limit", 429), extraction.ExtractError),
    # Hermes labels a quota 429 "payment error" too; a plan limit is not enforcement (#148 review round 2).
    (ProviderError("You've reached your weekly usage limit", 429), extraction.ExtractError),
    (ProviderError("rate_limit_error: quota exceeded for this plan", 429), extraction.ExtractError),
    (TimeoutError("Claude request timed out"), extraction.ExtractError),
])
def test_an_answer_from_hermess_fallback_is_not_used_and_the_failure_is_raised(ladder, error, raised):
    # #148 review: Hermes answers some failures with the main agent model instead of raising.
    main = ladder(error)
    with pytest.raises(raised):
        extraction.propose({"user": "u", "tool_results": []})
    assert main.calls == 1   # the fallback did answer; its facts are discarded


@pytest.fixture
def plugin_patterns():
    """The plugin's Supabase key patterns, registered as its load does; Hermes's own redactor lacks them."""
    from agent import redact

    import custom_build
    redact.register_redaction_patterns(custom_build.REDACTION_PATTERNS, source="plugin:custom_build")
    yield
    redact._reset_plugin_redaction_patterns()


@pytest.mark.parametrize("error", [ProviderError("Claude Code is not logged in", 401),
                                   ProviderError("permission_error: this organization is forbidden", 403)])
def test_a_refused_account_through_hermess_real_call_is_extract_refused(ladder, error):
    ladder(error)
    with pytest.raises(extraction.ExtractRefused):
        extraction.propose({"user": "u", "tool_results": []})


def test_a_long_provider_error_is_redacted_before_it_is_cut(monkeypatch, plugin_patterns):
    # #148 review: cutting first could leave part of a key that the redactor no longer recognises.
    message = "x" * 155 + f" {FAKE_KEY}"   # "ProviderError: " + this puts the 200-character cut 29 characters into the key
    monkeypatch.setattr(auxiliary_client, "call_llm", lambda **kw: (_ for _ in ()).throw(ProviderError(message, 500)))
    with pytest.raises(extraction.ExtractError) as caught:
        extraction.propose({"user": "u", "tool_results": []})
    assert FAKE_KEY[:25] not in str(caught.value)
