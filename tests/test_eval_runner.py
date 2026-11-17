"""Prove the eval runner (issue #167; ARCHITECTURE.md 3.8; decided: option C, the full pipeline).

The runner replays each question's chat history through the build as Hermes drives it, then asks the
question through prefetch() and scores the notes. Here it runs end to end on a tiny made-up dataset in
LongMemEval's format, with the fakes: Ollama, the judge, and Hermes's call_llm, which a wrapper around
the replay replaces. Nothing is downloaded and no real model is called. It needs TEST_DATABASE_URL, a
superuser connection to a throwaway Postgres, as the store tests do.
"""

import hashlib
import http.server
import json
import os
import pathlib
import sys
import threading

import psycopg
import pytest

from evals import longmemeval

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Three questions in LongMemEval's format: one about what the user said, one with the answer in no session
# (abstention), and one whose evidence session is the second of two.
DATASET = [
    {"question_id": "q1", "question_type": "single-session-user", "question": "Which editor does the user use?",
     "answer": "vim", "question_date": "2023/05/30 (Tue) 10:00",
     "haystack_session_ids": ["s-a", "s-b"], "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 09:00"],
     "haystack_sessions": [
         [{"role": "user", "content": "I edit all my code in vim, every day.", "has_answer": True},
          {"role": "assistant", "content": "Good choice."}],
         [{"role": "user", "content": "Plan a trip to Lisbon."}, {"role": "assistant", "content": "Sure."}]],
     "answer_session_ids": ["s-a"]},
    {"question_id": "q2_abs", "question_type": "single-session-user", "question": "What is the user's cat called?",
     "answer": "The user never said.", "question_date": "2023/05/30 (Tue) 10:00",
     "haystack_session_ids": ["s-c"], "haystack_dates": ["2023/05/22 (Mon) 08:00"],
     "haystack_sessions": [[{"role": "user", "content": "Recommend a pasta recipe."},
                            {"role": "assistant", "content": "Try carbonara."}]],
     "answer_session_ids": []},
    {"question_id": "q3", "question_type": "knowledge-update", "question": "Which city does the user live in now?",
     "answer": "Porto", "question_date": "2023/06/30 (Fri) 10:00",
     "haystack_session_ids": ["s-d", "s-e"], "haystack_dates": ["2023/06/01 (Thu) 08:00", "2023/06/10 (Sat) 08:00"],
     "haystack_sessions": [
         [{"role": "user", "content": "Tell me a joke."}, {"role": "assistant", "content": "Why did the chicken..."}],
         [{"role": "user", "content": "I just moved and now live in Porto city.", "has_answer": True},
          {"role": "assistant", "content": "Welcome to Porto."}]],
     "answer_session_ids": ["s-e"]},
]

# The replay's wrapper: Hermes's call_llm answers as Haiku would, extracting the user's own words as one fact
# (and nothing from small talk), and writing a short digest; Ollama and the judge are the test's fakes.
WRAPPER = r"""
import json, os, sys
from agent import auxiliary_client
from evals import replay

def fake_call_llm(**kwargs):
    if kwargs["task"] == "custom_build_extraction":
        user = json.loads(kwargs["messages"][-1]["content"])["user"]
        facts = [{"body": user, "basis": "user_stated"}] if ("vim" in user or "Porto" in user) else []
        text = json.dumps(facts)
    else:
        text = json.dumps({"summary": "A short session.", "open_items": []})
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}

auxiliary_client.call_llm = fake_call_llm

def patch(modules):
    modules["embedding"].OLLAMA_URL = os.environ["EVAL_TEST_OLLAMA_URL"]
    modules["judge"].URL = os.environ["EVAL_TEST_JUDGE_URL"]

replay.main(sys.argv[1:], patch=patch)
"""


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(DATASET), encoding="utf-8")
    return path


def run(tmp_path, dataset, ollama, typesafe, **options):
    os.environ["EVAL_TEST_OLLAMA_URL"], os.environ["EVAL_TEST_JUDGE_URL"] = ollama.url, typesafe.url
    defaults = {"data": dataset, "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "questions": 3,
                "seed": 1, "yes": True, "postgres": os.environ["TEST_DATABASE_URL"], "out": tmp_path / "report",
                "replay_command": [sys.executable, "-c", WRAPPER], "judge_key": "test-key-not-a-real-one"}
    return longmemeval.run(**{**defaults, **options})


def test_the_runner_scores_recall_mrr_and_abstention_through_the_whole_build(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9   # the gate keeps each extracted fact, and the rerank keeps each note
    report = run(tmp_path, dataset, ollama, typesafe)
    assert report["questions"] == 3
    assert report["answerable"] == {"count": 2, "recall_at_5": 1.0, "mrr": 1.0}
    assert report["abstention"] == {"count": 1, "correct": 1}
    assert report["by_type"]["knowledge-update"]["recall_at_5"] == 1.0
    written = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    assert written["answerable"] == report["answerable"] and written["embedding_build"] == ollama.build
    assert (tmp_path / "report" / "report.md").exists()


def test_without_yes_the_runner_only_estimates_what_a_run_would_call(tmp_path, dataset, ollama, typesafe):
    report = run(tmp_path, dataset, ollama, typesafe, yes=False)
    # 5 user turns, each an extraction call; 5 sessions, each ending in a digest when long enough (none is).
    assert report == {"estimate": {"questions": 3, "user_turns": 5, "sessions": 5, "haiku_calls_at_least": 5}}
    assert typesafe.requests == [] and ollama.embedded == []


def test_a_data_file_whose_hash_differs_is_refused(tmp_path, dataset, ollama, typesafe):
    with pytest.raises(longmemeval.EvalError, match="SHA-256"):
        run(tmp_path, dataset, ollama, typesafe, sha256="0" * 64)


def test_a_refused_account_stops_the_run_and_keeps_what_was_measured(tmp_path, dataset, ollama, typesafe):
    # The enforcement alert (#148): the first extract.refused ends the run.
    refusing = WRAPPER.replace('''def fake_call_llm(**kwargs):''', '''class Refusal(RuntimeError):
    status_code = 403

def fake_call_llm(**kwargs):
    raise Refusal("this account is restricted")''')
    report = run(tmp_path, dataset, ollama, typesafe, replay_command=[sys.executable, "-c", refusing])
    assert report["stopped"] == "extract.refused" and report["questions"] == 1


def test_the_run_leaves_no_database_or_role_behind(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    run(tmp_path, dataset, ollama, typesafe)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as admin:
        assert admin.execute("select count(*) from pg_database where datname like 'eval_%'").fetchone() == (0,)
        assert admin.execute("select count(*) from pg_roles where rolname in ('memory_agent', 'memory_maintenance')"
                             " or rolname like 'eval_%'").fetchone() == (0,)


def test_a_subset_is_the_same_for_the_same_seed_and_spread_across_question_types():
    entries = [{"question_id": f"q{n}", "question_type": ["a", "b", "c"][n % 3]} for n in range(30)]
    first = longmemeval.subset(entries, 6, seed=7)
    assert first == longmemeval.subset(entries, 6, seed=7)
    assert sorted(entry["question_type"] for entry in first) == ["a", "a", "b", "b", "c", "c"]


@pytest.fixture
def data_server():
    """A local HTTP server standing in for Hugging Face, serving the made-up dataset."""
    payload = json.dumps(DATASET).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/longmemeval_s_cleaned.json", payload
    server.server_close()


def test_the_data_file_is_downloaded_and_kept_only_when_its_hash_matches(tmp_path, data_server):
    url, payload = data_server
    kept = longmemeval.fetch(url, tmp_path / "good.json", hashlib.sha256(payload).hexdigest())
    assert kept.read_bytes() == payload
    with pytest.raises(longmemeval.EvalError, match="SHA-256"):
        longmemeval.fetch(url, tmp_path / "bad.json", "0" * 64)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["good.json"]   # nothing half-written is kept
