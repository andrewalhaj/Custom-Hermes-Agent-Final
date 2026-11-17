"""Prove the eval runner (issue #167; ARCHITECTURE.md 3.8; decided: option C, the full pipeline).

The runner replays each question's chat history through the build as Hermes drives it, then asks the
question through prefetch() and scores the notes. Here it runs end to end on a tiny made-up dataset in
LongMemEval's format, with the fakes: Ollama, the judge, and Hermes's call_llm, which a wrapper around
the replay replaces. Nothing is downloaded and no real model is called. It needs TEST_DATABASE_URL, a
superuser connection to a throwaway Postgres, as the store tests do.
"""

import hashlib
import http.server
import itertools
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
    if os.environ.get("EVAL_TEST_ROUTES"):   # where Hermes would send this task's call
        route = auxiliary_client._resolve_task_provider_model(kwargs["task"], None, None, None, None)[:2]
        with open(os.environ["EVAL_TEST_ROUTES"], "a") as routes:
            routes.write(json.dumps([kwargs["task"], *route]) + "\n")
    if kwargs["task"] == "custom_build_extraction":
        user = json.loads(kwargs["messages"][-1]["content"])["user"]
        facts = [{"body": user, "basis": "user_stated"}] if ("vim" in user or "Porto" in user) else []
        text = json.dumps(facts)
    else:
        text = json.dumps({"summary": "A short session.", "open_items": []})
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}

auxiliary_client.call_llm = fake_call_llm
if os.environ.get("EVAL_TEST_ENV_OUT"):   # the Claude and Anthropic settings the replay was started with
    with open(os.environ["EVAL_TEST_ENV_OUT"], "a") as out:
        out.write(json.dumps(sorted(n for n in os.environ if n.startswith(("ANTHROPIC", "CLAUDE")))) + "\n")

def patch(modules):
    modules["embedding"].OLLAMA_URL = os.environ["EVAL_TEST_OLLAMA_URL"]
    modules["judge"].URL = os.environ["EVAL_TEST_JUDGE_URL"]
    if sys.argv[1] == "ask" and os.environ.get("EVAL_TEST_ASK_FAILS"):
        modules["embedding"].OLLAMA_URL = "http://127.0.0.1:9"   # nothing listens: recall fails
    if json.load(open(sys.argv[2]))["question_id"] == os.environ.get("EVAL_TEST_CRASH_ON"):
        raise SystemExit("the replay crashed")

if os.environ.get("EVAL_TEST_TIMES") and sys.argv[1] == "ingest":   # when each question's replay ran (#219)
    import time
    started = time.time()
    time.sleep(1)
    replay.main(sys.argv[1:], patch=patch)
    with open(os.environ["EVAL_TEST_TIMES"], "a") as times:
        times.write(json.dumps([started, time.time()]) + "\n")
else:
    replay.main(sys.argv[1:], patch=patch)
"""


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(DATASET), encoding="utf-8")
    return path


def run(tmp_path, dataset, ollama, typesafe, **options):
    os.environ["EVAL_TEST_OLLAMA_URL"], os.environ["EVAL_TEST_JUDGE_URL"] = ollama.url, typesafe.url
    for name in ("EVAL_TEST_ASK_FAILS", "EVAL_TEST_CRASH_ON", "EVAL_TEST_ROUTES", "EVAL_TEST_ENV_OUT", "EVAL_TEST_TIMES"):
        os.environ.pop(name, None)
    os.environ.update(options.pop("test_env", {}))
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


def test_the_replay_sends_extraction_where_the_plugin_routes_it_haiku_on_the_subscription(
        tmp_path, dataset, ollama, typesafe):
    # Found before the first real run: the plugin's own route is registered only when Hermes discovers the
    # plugin; without it the task routes "auto", wherever the environment points, not to the subscription.
    typesafe.default = 0.9
    routes = tmp_path / "routes.jsonl"
    run(tmp_path, dataset, ollama, typesafe, test_env={"EVAL_TEST_ROUTES": str(routes)})
    calls = [json.loads(line) for line in routes.read_text(encoding="utf-8").splitlines()]
    assert calls and all(call == ["custom_build_extraction", "claude-subscription-directsdk-experimental", "haiku"]
                         for call in calls)


def test_the_replay_never_inherits_the_callers_claude_or_anthropic_settings(
        tmp_path, dataset, ollama, typesafe, monkeypatch):
    # Seen in the first real run, started from a Claude Code shell: the shell's ANTHROPIC_BASE_URL reached the
    # subscription provider, which refused every extraction; the shell's account and organisation ids would
    # have reached its `claude` child too. The provider's own setting picks the login (#170).
    typesafe.default = 0.9
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CLAUDE_CODE_ORGANIZATION_UUID", "not-the-eval-login")
    monkeypatch.setenv("CLAUDECODE", "1")
    seen = tmp_path / "env.jsonl"
    run(tmp_path, dataset, ollama, typesafe, test_env={"EVAL_TEST_ENV_OUT": str(seen)})
    started = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
    assert started and all(names == [] for names in started)


def test_without_yes_the_runner_only_estimates_what_a_run_would_call(tmp_path, dataset, ollama, typesafe):
    report = run(tmp_path, dataset, ollama, typesafe, yes=False)
    # 5 user turns, each an extraction call; 5 sessions, each ending in a digest when long enough (none is);
    # the judge at least once per question, for the rerank (#167 review).
    assert report == {"estimate": {"questions": 3, "user_turns": 5, "sessions": 5, "haiku_calls_at_least": 5,
                                   "judge_calls_at_least": 3}}
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


def _chatty(turns):
    """One question whose single session has `turns` user turns, each with a reply."""
    session = []
    for n in range(turns):
        session += [{"role": "user", "content": f"Message {n}."}, {"role": "assistant", "content": "Noted."}]
    return [{"question_id": "q-long", "question_type": "single-session-user", "question": "Anything?",
             "answer": "no", "question_date": "2023/05/30 (Tue) 10:00", "haystack_session_ids": ["s-long"],
             "haystack_dates": ["2023/05/20 (Sat) 02:21"], "haystack_sessions": [session],
             "answer_session_ids": ["s-long"]}]


def _events(report, code):
    return sum(1 for result in report["per_question"] for seen in result["codes"] if seen == code)


@pytest.mark.parametrize("status, message, code", [
    (403, "this account is restricted", "extract.refused"),           # the enforcement alert (#148)
    (429, "You've hit your session limit", "extract.error"),          # a plan limit (#167 item 4)
])
def test_a_refusal_or_a_plan_limit_stops_the_replay_at_once(tmp_path, ollama, typesafe, status, message, code):
    # #167 review: the run stopped only after the whole question, calling a refused account again and again.
    data = tmp_path / "long.json"
    data.write_text(json.dumps(_chatty(10)), encoding="utf-8")
    failing = WRAPPER.replace('''def fake_call_llm(**kwargs):''', f'''class Refusal(RuntimeError):
    status_code = {status}

def fake_call_llm(**kwargs):
    raise Refusal("{message}")''')
    report = run(tmp_path, data, ollama, typesafe, questions=1, replay_command=[sys.executable, "-c", failing],
                 sha256=hashlib.sha256(data.read_bytes()).hexdigest())
    assert report["stopped"] and _events(report, code) == 1


# The first real pilot (#171): the provider refused the caller's settings on every call, recorded as an
# extract.error with no limit in its reason; the run went on through every question and reported recall 0.
ERRORING = WRAPPER.replace('''def fake_call_llm(**kwargs):''', '''def fake_call_llm(**kwargs):
    raise ValueError("OAuth provider refuses conflicting native auth/backend overrides: ANTHROPIC_BASE_URL")''')


def test_an_extraction_that_errors_on_every_call_stops_the_run_after_five_calls(tmp_path, ollama, typesafe):
    data = tmp_path / "long.json"
    data.write_text(json.dumps(_chatty(10)), encoding="utf-8")
    report = run(tmp_path, data, ollama, typesafe, questions=1, replay_command=[sys.executable, "-c", ERRORING],
                 sha256=hashlib.sha256(data.read_bytes()).hexdigest())
    assert report["stopped"] == "extract.error" and _events(report, "extract.error") == 5


def test_four_failures_in_the_first_five_calls_do_not_stop_the_run(tmp_path, ollama, typesafe):
    # Guards the rule's edge (#171 review): only when all of a question's first five calls fail is the failure
    # systematic; four failures and an answer is not.
    typesafe.default = 0.9
    data = tmp_path / "long.json"
    data.write_text(json.dumps(_chatty(10)), encoding="utf-8")
    flaky = WRAPPER.replace('''def fake_call_llm(**kwargs):''', '''calls = []

def fake_call_llm(**kwargs):
    calls.append(1)
    if len(calls) <= 4:
        raise ValueError("a failure that passes")
    return answered(**kwargs)

def answered(**kwargs):''')
    report = run(tmp_path, data, ollama, typesafe, questions=1, replay_command=[sys.executable, "-c", flaky],
                 sha256=hashlib.sha256(data.read_bytes()).hexdigest())
    assert not report.get("stopped") and _events(report, "extract.error") == 4


def test_the_report_lists_each_failure_reason_with_its_count(tmp_path, dataset, ollama, typesafe):
    # A reason was found only by replaying a turn by hand: the databases are dropped after each question.
    report = run(tmp_path, dataset, ollama, typesafe, replay_command=[sys.executable, "-c", ERRORING])
    # The questions have 2, 1 and 2 turns, under five each: none stops, so the count adds across all three.
    assert not report.get("stopped")
    reason ="ValueError: OAuth provider refuses conflicting native auth/backend overrides: ANTHROPIC_BASE_URL"
    assert report["failure_reasons"] == [{"code": "extract.error", "reason": reason, "count": 5}]
    assert reason in (tmp_path / "report" / "report.md").read_text(encoding="utf-8")


def test_a_failed_recall_is_counted_as_a_failure_never_as_a_correct_abstention(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    report = run(tmp_path, dataset, ollama, typesafe, test_env={"EVAL_TEST_ASK_FAILS": "1"})
    assert report["abstention"] == {"count": 1, "correct": 0}
    assert report["recall_failed"] == 3
    assert report["failure_codes"].get("embed.unavailable")


def test_a_crashed_replay_keeps_the_report_of_what_was_measured(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    second = longmemeval.subset(json.loads(json.dumps(DATASET)), 3, seed=1)[1]["question_id"]
    with pytest.raises(longmemeval.EvalError):
        run(tmp_path, dataset, ollama, typesafe, questions=3, test_env={"EVAL_TEST_CRASH_ON": second})
    written = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    assert written["stopped"].startswith("crashed") and written["questions"] == 1   # the first, already paid for


def test_a_postgres_that_already_has_the_memory_roles_is_refused_and_left_alone(tmp_path, dataset, ollama, typesafe):
    # #167 review: the runner drops the cluster-wide roles it creates; on a cluster that has them, it would
    # drop someone else's. It refuses instead.
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as admin:
        admin.execute("create role memory_agent nologin")
        try:
            with pytest.raises(longmemeval.EvalError, match="memory_agent"):
                run(tmp_path, dataset, ollama, typesafe)
            assert admin.execute("select count(*) from pg_roles where rolname = 'memory_agent'").fetchone() == (1,)
        finally:
            admin.execute("drop role memory_agent")


def test_with_parallel_two_questions_replay_at_the_same_time(tmp_path, dataset, ollama, typesafe):
    # #219: each question has its own database and Hermes home, so N of them can run at once.
    typesafe.default = 0.9
    times = tmp_path / "times.jsonl"
    run(tmp_path, dataset, ollama, typesafe, parallel=2, test_env={"EVAL_TEST_TIMES": str(times)})
    spans = sorted(json.loads(line) for line in times.read_text(encoding="utf-8").splitlines())
    assert len(spans) == 3 and any(later[0] < earlier[1] for earlier, later in itertools.pairwise(spans))


def test_a_parallel_run_scores_as_the_same_questions_one_at_a_time(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    one_at_a_time = run(tmp_path, dataset, ollama, typesafe, out=tmp_path / "one")
    parallel = run(tmp_path, dataset, ollama, typesafe, parallel=3, out=tmp_path / "three")
    keep = ("questions", "answerable", "abstention", "by_type", "failure_codes")
    assert {k: parallel[k] for k in keep} == {k: one_at_a_time[k] for k in keep}
    assert [r["question_id"] for r in parallel["per_question"]] == [r["question_id"] for r in
                                                                   one_at_a_time["per_question"]]


def test_a_parallel_run_leaves_no_database_or_role_behind(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    run(tmp_path, dataset, ollama, typesafe, parallel=3)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as admin:
        assert admin.execute("select count(*) from pg_database where datname like 'eval_%'").fetchone() == (0,)
        assert admin.execute("select count(*) from pg_roles where rolname in ('memory_agent', 'memory_maintenance')"
                             " or rolname like 'eval_%'").fetchone() == (0,)


def test_a_stop_in_a_parallel_run_starts_no_new_question(tmp_path, dataset, ollama, typesafe):
    # Questions already running finish and are reported; none starts after the stop.
    refusing = WRAPPER.replace('''def fake_call_llm(**kwargs):''', '''class Refusal(RuntimeError):
    status_code = 403

def fake_call_llm(**kwargs):
    raise Refusal("this account is restricted")''')
    report = run(tmp_path, dataset, ollama, typesafe, parallel=2, replay_command=[sys.executable, "-c", refusing])
    # Both start before either is refused, so both are reported (#219 review: "<= 2" also passed with none).
    assert report["stopped"] == "extract.refused" and report["questions"] == 2


@pytest.mark.parametrize("parallel", [1, 2])
def test_ctrl_c_while_questions_run_keeps_the_ones_already_measured(tmp_path, dataset, ollama, typesafe, monkeypatch,
                                                                    parallel):
    # #219 review: results were collected only after the wait, so Ctrl-C reported none, even at parallel 1.
    def measured(entry, **kwargs):
        return {"question_id": entry["question_id"], "question_type": entry["question_type"], "abstention": False,
                "notes": 0, "recall_failed": False, "first_hit": None, "codes": [], "failures": [],
                "embedding_build": None, "stopped": None}

    def interrupted(futures, **kwargs):
        futures[0].result()   # the first question is measured, then Ctrl-C
        raise KeyboardInterrupt

    monkeypatch.setattr(longmemeval, "one", measured)
    monkeypatch.setattr(longmemeval.concurrent.futures, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, dataset, ollama, typesafe, parallel=parallel)
    written = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    assert written["questions"] >= 1 and written["stopped"].startswith("crashed: KeyboardInterrupt")


def test_a_first_migration_without_the_role_statements_the_runner_removes_fails_the_run(
        tmp_path, dataset, ollama, typesafe, monkeypatch):
    # The roles are made once per run, so each question's database applies 0001 without them (#219); a 0001 that
    # no longer has them must fail loudly, never apply half a migration.
    changed = tmp_path / "0001_memory_events.sql"
    original = longmemeval.MIGRATIONS[0]
    changed.write_text(original.read_text(encoding="utf-8").replace("create role memory_agent nologin;\n", ""),
                       encoding="utf-8")
    monkeypatch.setattr(longmemeval, "MIGRATIONS", [changed, *longmemeval.MIGRATIONS[1:]])
    with pytest.raises(longmemeval.EvalError, match="0001"):
        run(tmp_path, dataset, ollama, typesafe)


def test_a_turn_with_no_reply_is_not_synced_as_hermes_never_does():
    session = [{"role": "user", "content": "First."}, {"role": "user", "content": "Second."},
               {"role": "assistant", "content": "Reply."}, {"role": "user", "content": "Unanswered."}]
    from evals import replay
    assert [(user, reply) for user, reply, _ in replay.turns(session)] == [("Second.", "Reply.")]
