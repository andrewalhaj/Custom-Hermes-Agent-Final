"""Prove session digests (issue #155, ARCHITECTURE.md 3.3, the fourth entrance; 3.6, long session).

The unit tests replace Hermes's `call_llm` in this process. The end-to-end tests copy the plugin into a
throwaway Hermes home and, in a process of their own, load the provider through Hermes v0.23.1's loader
and call on_session_end() as Hermes does at a session boundary; there `call_llm` is replaced by a fake
that answers as the scenario says and writes down what it was sent.
"""

import json
import pathlib
import shutil
import subprocess
import sys

import psycopg
import pytest
import yaml
from agent import auxiliary_client

from custom_build.memory import digest, extraction

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
FAKE_KEY = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"   # assembled at run time, never a literal key
SUMMARY = "The session moved the mini's gateway to the hermes user."
BODY = f"{SUMMARY}\n\nOpen items:\n- Restart the gateway.\n- Update the unit file on the laptop."
ANSWER = json.dumps({"summary": SUMMARY, "open_items": ["Restart the gateway.", "Update the unit file on the laptop."]})


def _session(pairs, *, secret=False):
    """A system prompt, then `pairs` user and assistant messages: 2 * pairs messages that count."""
    messages = [{"role": "system", "content": "You are Hermes."}]
    for n in range(pairs):
        user = f"Question {n}" + (f" with key {FAKE_KEY}" if secret and n == 0 else "")
        messages += [{"role": "user", "content": user}, {"role": "assistant", "content": f"Answer {n}"}]
    return messages


def _answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_a_session_is_long_at_twenty_messages_not_counting_the_system_prompt():
    assert digest.is_long(_session(10)) and not digest.is_long(_session(10)[:-1])


def test_the_transcript_leaves_out_the_system_prompt_and_keeps_the_newest_messages_under_the_cap():
    messages = _session(3)
    assert digest.transcript(messages, max_chars=10_000) == [
        {"role": m["role"], "content": m["content"]} for m in messages[1:]]
    assert digest.transcript(messages, max_chars=20) == [{"role": "user", "content": "Question 2"},
                                                         {"role": "assistant", "content": "Answer 2"}]


def test_the_answer_becomes_the_summary_and_its_open_items(monkeypatch):
    calls = []
    monkeypatch.setattr(auxiliary_client, "call_llm", lambda **kw: calls.append(kw) or _answer(ANSWER))
    assert digest.write([{"role": "user", "content": "u"}]) == BODY
    assert calls[0]["task"] == "custom_build_digest"


def test_a_digest_with_no_open_items_is_the_summary_alone(monkeypatch):
    monkeypatch.setattr(auxiliary_client, "call_llm",
                        lambda **kw: _answer(json.dumps({"summary": SUMMARY, "open_items": []})))
    assert digest.write([{"role": "user", "content": "u"}]) == SUMMARY


@pytest.mark.parametrize("text", ["not json", json.dumps([SUMMARY]), json.dumps({"summary": ""}),
                                  json.dumps({"summary": SUMMARY, "open_items": "Restart it."})])
def test_an_answer_that_is_not_a_digest_is_an_error(monkeypatch, text):
    monkeypatch.setattr(auxiliary_client, "call_llm", lambda **kw: _answer(text))
    with pytest.raises(extraction.ExtractError):
        digest.write([{"role": "user", "content": "u"}])


SCENARIO = r"""
import json, logging, sys
import plugins.memory
from agent import auxiliary_client

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
ollama_url, judge_url, answer, sent_path, context = sys.argv[1:6]
runs = json.loads(sys.argv[6])

class Refusal(RuntimeError):
    status_code = 401

try:   # a JSON list is one answer per call, in order
    answers = json.loads(answer) if json.loads(answer).__class__ is list else None
except ValueError:
    answers = None

def fake_call_llm(**kwargs):
    with open(sent_path, "a") as f:
        f.write(json.dumps(kwargs["messages"]) + "\n")
    if answer == "refused":
        raise Refusal("Claude Code is not logged in")
    text = json.dumps(answers.pop(0)) if answers is not None else answer
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}

auxiliary_client.call_llm = fake_call_llm
provider = plugins.memory.load_memory_provider("custom_build")
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = ollama_url
modules["judge"].URL = judge_url
provider.initialize(session_id="s1", hermes_home=sys.argv[7], platform="cli", agent_context=context)
provider.on_turn_start(7, "the last message")
for messages in runs:
    provider.on_session_end(messages)
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


def end(home, db, ollama_url, judge_url, answer, runs, *, context="primary", env_extra=None):
    """Start the provider and end the session once per entry in `runs`; return (what call_llm was sent, log)."""
    sent = home.parent / "sent.jsonl"
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "CUSTOM_BUILD_DATABASE_URL": db["agent"], "TYPESAFE_API_KEY": "test-key-not-a-real-one", **(env_extra or {})}
    result = subprocess.run(
        [sys.executable, "-c", SCENARIO, ollama_url, judge_url, answer, str(sent), context, json.dumps(runs), str(home)],
        capture_output=True, text=True, env=env, cwd=home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return (sent.read_text().splitlines() if sent.exists() else []), result.stderr


def _rows(db):
    with psycopg.connect(db["owner"]) as conn:
        return conn.execute("select body, kind, origin, basis, project, source, session_id, source_turn from memory.facts"
                            " order by created_at").fetchall()


def _codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select code from memory.events order by id")]


def test_a_long_session_gets_one_digest_the_judge_keeps(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.8}
    sent, _ = end(home, db, ollama.url, typesafe.url, ANSWER, [_session(10)])
    assert _rows(db) == [(BODY, "digest", "digest", "assistant_inferred", "github.com/Owner/Repo", "host-a", "s1", 7)]
    assert "You are Hermes." not in sent[0]
    [gate] = typesafe.requests
    assert gate["body"]["state"] == {"digest": BODY}


def test_a_short_session_gets_no_digest(home, db, ollama, typesafe):
    sent, _ = end(home, db, ollama.url, typesafe.url, ANSWER, [_session(10)[:-1]])
    assert sent == [] and _rows(db) == []


FIRST, LATER = SUMMARY, SUMMARY + " It also fixed the unit file."
TWO = json.dumps([{"summary": FIRST, "open_items": []}, {"summary": LATER, "open_items": []}])


def _live_digests(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select body from memory.facts where kind = 'digest'"
                                               " and superseded_by is null and retired_at is null")]


def test_a_later_digest_of_the_session_replaces_the_earlier_one(home, db, ollama, typesafe):
    # #156, decided B: Hermes ends a session at each compression too, and the later digest is the fuller one.
    typesafe.answers = {"worth_keeping": 0.8}
    sent, _ = end(home, db, ollama.url, typesafe.url, TWO, [_session(10), _session(12)])
    assert len(sent) == 2 and len(_rows(db)) == 2
    assert _live_digests(db) == [LATER]


def test_a_session_with_a_digest_gets_a_later_one_even_when_its_transcript_is_short_now(home, db, ollama, typesafe):
    # After an in-place compression the transcript is short, but the session is still a long one.
    typesafe.answers = {"worth_keeping": 0.8}
    end(home, db, ollama.url, typesafe.url, TWO, [_session(10), _session(3)])
    assert _live_digests(db) == [LATER]


def test_a_digest_the_judge_does_not_keep_is_dropped(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.2}
    end(home, db, ollama.url, typesafe.url, ANSWER, [_session(10)])
    assert _rows(db) == [] and _codes(db) == []


def test_a_refused_account_is_recorded_as_digest_refused(home, db, ollama, typesafe):
    end(home, db, ollama.url, typesafe.url, "refused", [_session(10)])
    assert _rows(db) == [] and _codes(db) == ["digest.refused"]


def test_a_malformed_answer_is_recorded_as_digest_error(home, db, ollama, typesafe):
    end(home, db, ollama.url, typesafe.url, "Here is the digest you asked for.", [_session(10)])
    assert _codes(db) == ["digest.error"]


def test_a_judge_failure_queues_the_digest_and_the_retry_still_gates_it(home, db, ollama, typesafe):
    typesafe.status = 500
    end(home, db, ollama.url, typesafe.url, ANSWER, [_session(10)])
    assert _rows(db) == [] and _codes(db) == ["judge.error", "store.gate_error"]
    typesafe.status, typesafe.answers = 200, {"worth_keeping": 0.2}
    end(home, db, ollama.url, typesafe.url, ANSWER, [])   # the next start retries the queue
    assert _rows(db) == []
    assert typesafe.requests[-1]["body"]["state"] == {"digest": BODY}


def test_nothing_is_written_outside_a_primary_session(home, db, ollama, typesafe):
    sent, _ = end(home, db, ollama.url, typesafe.url, ANSWER, [_session(10)], context="cron")
    assert sent == [] and _rows(db) == []


def test_a_secret_in_the_session_never_reaches_the_model_the_judge_the_store_or_a_log_line(home, db, ollama, typesafe):
    typesafe.answers = {"worth_keeping": 0.8}
    leaky = json.dumps({"summary": f"The store key is {FAKE_KEY}.", "open_items": []})
    sent, output = end(home, db, ollama.url, typesafe.url, leaky, [_session(10, secret=True)],
                       env_extra={"HERMES_REDACT_SECRETS": "false"})   # the build's redaction is forced
    assert sent and all(FAKE_KEY not in line for line in sent)
    assert all(FAKE_KEY not in json.dumps(r["body"]) for r in typesafe.requests)
    assert all(FAKE_KEY not in row[0] for row in _rows(db))
    assert FAKE_KEY not in output


def test_two_digests_queued_for_one_session_leave_one_live(home, db, ollama, typesafe):
    # #155 review: a session ended twice while the judge is down queues two; the retry leaves the later live.
    typesafe.status = 500
    end(home, db, ollama.url, typesafe.url, TWO, [_session(10), _session(12)])
    typesafe.status, typesafe.answers = 200, {"worth_keeping": 0.8}
    end(home, db, ollama.url, typesafe.url, ANSWER, [])   # the next start retries the queue
    assert _live_digests(db) == [LATER]


def test_a_newest_message_over_the_cap_is_cut_not_left_out():
    # #155 review: otherwise the model is sent an empty transcript.
    messages = [{"role": "user", "content": "x" * 50}]
    assert digest.transcript(messages, max_chars=20) == [{"role": "user", "content": "x" * 20}]
