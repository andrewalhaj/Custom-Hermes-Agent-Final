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
    if os.environ.get("EVAL_TEST_HOSTED_URL"):   # the hosted route's fake and its reference (#239)
        import pathlib
        modules["embedding"].HOSTED_URL = os.environ["EVAL_TEST_HOSTED_URL"]
        modules["embedding"].REFERENCE = pathlib.Path(os.environ["EVAL_TEST_REFERENCE"])
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
    for name in ("EVAL_TEST_ASK_FAILS", "EVAL_TEST_CRASH_ON", "EVAL_TEST_ROUTES", "EVAL_TEST_ENV_OUT", "EVAL_TEST_TIMES",
                 "EVAL_TEST_HOSTED_URL", "EVAL_TEST_REFERENCE"):
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
    # the judge at least twice per question: the rerank (#167 review), and the scores before the floor (#234).
    assert report == {"estimate": {"questions": 3, "user_turns": 5, "sessions": 5, "haiku_calls_at_least": 5,
                                   "judge_calls_at_least": 6}}
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
                             " or rolname = 'eval' or rolname like 'eval_%'").fetchone() == (0,)


def test_chosen_ids_run_exactly_those_questions_in_that_order(tmp_path, dataset, ollama, typesafe):
    # #236: the seeded subset held no abstention question; --ids runs exactly the questions asked for.
    typesafe.default = 0.9
    report = run(tmp_path, dataset, ollama, typesafe, ids=["q3", "q2_abs"])
    assert [r["question_id"] for r in report["per_question"]] == ["q3", "q2_abs"]
    assert report["ids"] == ["q3", "q2_abs"] and report["abstention"] == {"count": 1, "correct": 1}
    assert report["seed"] is None   # #236 review: a chosen run records no draw
    assert "questions chosen by id: q3, q2_abs." in (tmp_path / "report" / "report.md").read_text(encoding="utf-8")


def test_an_unknown_id_stops_the_run_before_anything_is_called(tmp_path, dataset, ollama, typesafe):
    with pytest.raises(longmemeval.EvalError, match="q9"):
        run(tmp_path, dataset, ollama, typesafe, ids=["q1", "q9"])
    assert typesafe.requests == [] and ollama.embedded == []


def test_ids_with_a_count_or_a_seed_are_refused(dataset, capsys):
    for extra in (["--questions", "3"], ["--seed", "2"]):
        with pytest.raises(SystemExit):
            longmemeval.main(["--data", str(dataset), "--ids", "q1", *extra])
        # its own refusal, not argparse's "unrecognized arguments" (which an unknown --ids would also raise)
        assert "--ids replaces the seeded subset" in capsys.readouterr().err


@pytest.mark.parametrize("given, refusal", [
    ("", "--ids names no question"),   # #236 review: an empty list fell back to a paid seeded run
    (" , ", "--ids names no question"),
    ("q1,q2,q1", "--ids names q1 twice"),   # a duplicate would be paid for and scored twice
])
def test_an_empty_or_repeating_ids_list_is_refused(dataset, capsys, given, refusal):
    with pytest.raises(SystemExit):
        longmemeval.main(["--data", str(dataset), "--ids", given])
    assert refusal in capsys.readouterr().err


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


def test_a_miss_the_score_floor_cut_is_counted_as_cut_by_the_floor(tmp_path, dataset, ollama, typesafe):
    # #234: the gate keeps each fact, but the judge scores every candidate under the floor (0.30), so recall
    # brings nothing; the evidence was stored and found, then cut.
    typesafe.answers, typesafe.default = {"worth_keeping": 0.9, "s0": 0.9, "s1": 0.9}, 0.1
    report = run(tmp_path, dataset, ollama, typesafe)
    assert report["answerable"]["recall_at_5"] == 0.0
    assert report["misses"] == {"cut_by_floor": 2}
    first = report["per_question"][0]
    assert (first["evidence_stored"], first["pool_hit"], first["pool_best_score"]) == (1, 1, 0.1)
    assert "Misses: 2 cut by the floor" in (tmp_path / "report" / "report.md").read_text(encoding="utf-8")


def test_a_miss_whose_evidence_was_never_stored_is_counted_as_not_stored(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    extracting_nothing = WRAPPER.replace('if ("vim" in user or "Porto" in user)', 'if False')
    report = run(tmp_path, dataset, ollama, typesafe, replay_command=[sys.executable, "-c", extracting_nothing])
    assert report["misses"] == {"not_stored": 2}
    assert report["per_question"][0]["evidence_stored"] == 0


def test_a_run_with_no_miss_counts_none(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    assert run(tmp_path, dataset, ollama, typesafe)["misses"] == {}


def _missed(**fields):
    """An answerable question's result that missed, with `fields` changed."""
    return {"first_hit": None, "codes": [], "evidence_rows": 1, "evidence_stored": 1, "pool": True, "pool_hit": 1,
            "pool_best_score": 0.1, **fields}


@pytest.mark.parametrize("fields, cause", [
    ({"evidence_rows": 0, "evidence_stored": 0}, "not_stored"),
    ({"evidence_stored": 0}, "replaced"),   # stored, then superseded or retired (#234 review)
    ({"pool": False, "pool_hit": None, "pool_best_score": None}, "unknown"),   # the second fetch failed
    ({"pool_hit": None, "pool_best_score": None}, "not_in_pool"),
    ({"codes": ["rerank.fallback"]}, "fused_order"),   # recall's judge failed: no floor ran (#234 review)
    ({"pool_best_score": None}, "unscored"),   # the second judge request failed (#234 review)
    ({"pool_best_score": 0.1}, "cut_by_floor"),
    ({"pool_best_score": 0.7}, "above_floor"),
    ({"first_hit": 7}, "below_rank_5"),   # a past-work question's hit at 6 to 8 still misses recall@5
])
def test_each_miss_is_put_down_to_one_cause(fields, cause):
    assert longmemeval._miss(_missed(**fields), 0.3) == cause


def test_the_best_evidence_score_decides_not_the_first_candidates():
    # #234 review: recall sorts by score, so an evidence candidate further down with a higher score is the one
    # that counts; the first evidence candidate's 0.1 must not make it "cut by the floor".
    assert longmemeval._best_score([{"score": 0.1}, {"score": 0.9}, {"score": 0.7}], [1, 3]) == 0.7
    assert longmemeval._best_score([{"score": None}, {"score": 0.4}], [1, 2]) == 0.4
    assert longmemeval._best_score([{"score": None}], [1]) is None


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


@pytest.mark.parametrize("role", ["memory_agent", "eval"])   # a group role, and the eval's own login (#149)
def test_a_postgres_that_already_has_a_role_the_runner_makes_is_refused_and_left_alone(
        tmp_path, dataset, ollama, typesafe, role):
    # #167 review: the runner drops the cluster-wide roles it creates; on a cluster that has them, it would
    # drop someone else's. It refuses instead.
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("create role {} nologin").format(psycopg.sql.Identifier(role)))
        try:
            with pytest.raises(longmemeval.EvalError, match=role):
                run(tmp_path, dataset, ollama, typesafe)
            assert admin.execute("select count(*) from pg_roles where rolname = %s", (role,)).fetchone() == (1,)
        finally:
            admin.execute(psycopg.sql.SQL("drop role if exists {}").format(psycopg.sql.Identifier(role)))


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
                             " or rolname = 'eval' or rolname like 'eval_%'").fetchone() == (0,)


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
                "evidence_rows": 0, "evidence_stored": 0, "pool": True, "pool_hit": None, "pool_best_score": None,
                "fact_pairs": {"without_judge": 0, "ask_judge_from": 0}, "embedding_build": None, "stopped": None}

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


NO_EXTRACTION = WRAPPER.replace('''def fake_call_llm(**kwargs):''', '''def fake_call_llm(**kwargs):
    raise RuntimeError("an ask-only pass called a model")''')


def _nothing_left():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as admin:
        return (admin.execute("select count(*) from pg_database where datname like 'eval_%'").fetchone()[0],
                admin.execute("select count(*) from pg_roles where rolname in ('memory_agent', 'memory_maintenance')"
                              " or rolname = 'eval'").fetchone()[0]) == (0, 0)


def test_a_kept_run_asked_again_scores_each_question_as_the_run_did_without_extracting(
        tmp_path, dataset, ollama, typesafe):
    # #242 (for #239's comparison): the same stored facts asked twice, so only what changed between the two
    # passes can move the scores, not extraction's or the judge gate's variance between runs.
    typesafe.default = 0.9
    kept = tmp_path / "kept"
    try:
        first = run(tmp_path, dataset, ollama, typesafe, keep=True, out=kept)
        assert sorted(json.loads((kept / "kept.json").read_text(encoding="utf-8"))["databases"]) == ["q1", "q2_abs",
                                                                                                    "q3"]
        again = run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=tmp_path / "again",
                    replay_command=[sys.executable, "-c", NO_EXTRACTION])
    finally:
        longmemeval.drop_kept(os.environ["TEST_DATABASE_URL"], kept)
    same = ("question_id", "first_hit", "notes", "pool_hit", "evidence_rows", "evidence_stored")
    assert [{k: r[k] for k in same} for r in again["per_question"]] == [{k: r[k] for k in same}
                                                                         for r in first["per_question"]]
    assert again["answerable"] == first["answerable"]
    assert not any(code.startswith(("extract.", "digest.")) for r in again["per_question"] for code in r["codes"])


def test_dropping_a_kept_run_leaves_no_database_or_role_behind(tmp_path, dataset, ollama, typesafe):
    typesafe.default = 0.9
    kept = tmp_path / "kept"
    try:
        run(tmp_path, dataset, ollama, typesafe, keep=True, out=kept)
        assert not _nothing_left()   # kept until dropped
    finally:
        longmemeval.drop_kept(os.environ["TEST_DATABASE_URL"], kept)
    assert _nothing_left()


def test_asking_a_kept_run_again_counts_only_the_new_asks_events(tmp_path, dataset, ollama, typesafe):
    # #242 review: a first run that records no failure cannot tell "this ask's events" from "every event".
    typesafe.default = 0.9
    kept = tmp_path / "kept"
    try:
        run(tmp_path, dataset, ollama, typesafe, keep=True, ids=["q1"], out=kept)
        name = json.loads((kept / "kept.json").read_text(encoding="utf-8"))["databases"]["q1"]
        owner = psycopg.conninfo.make_conninfo(os.environ["TEST_DATABASE_URL"], dbname=name)
        with psycopg.connect(owner, autocommit=True) as conn:   # as if the kept run's ingest had failed once
            conn.execute("insert into memory.events (code, result, source, reason) values"
                         " ('extract.error', 'failed', 'eval', 'from the kept run')")
        again = run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=tmp_path / "again")
    finally:
        longmemeval.drop_kept(os.environ["TEST_DATABASE_URL"], kept)
    assert "extract.error" not in again["per_question"][0]["codes"]


def test_a_refused_kept_run_leaves_the_earlier_kept_list_as_it_was(tmp_path, dataset, ollama, typesafe):
    # #242 review: the second run is refused (the first one's roles are still there), and must not overwrite
    # the list drop_kept() needs.
    typesafe.default = 0.9
    kept = tmp_path / "kept"
    try:
        run(tmp_path, dataset, ollama, typesafe, keep=True, ids=["q1"], out=kept)
        listed = (kept / "kept.json").read_text(encoding="utf-8")
        with pytest.raises(longmemeval.EvalError, match="already has"):
            run(tmp_path, dataset, ollama, typesafe, keep=True, ids=["q1"], out=kept)
        assert (kept / "kept.json").read_text(encoding="utf-8") == listed
    finally:
        longmemeval.drop_kept(os.environ["TEST_DATABASE_URL"], kept)
    assert _nothing_left()


def test_asking_a_kept_run_again_into_its_own_folder_is_refused(tmp_path, dataset, ollama, typesafe):
    # #242 review: it would overwrite the kept run's report, the other side of the comparison.
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "kept.json").write_text(json.dumps({"databases": {"q1": "eval_000000000000"}}), encoding="utf-8")
    with pytest.raises(longmemeval.EvalError, match="its own --out"):
        run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=kept)


def test_asking_or_dropping_a_folder_with_no_kept_list_says_so(tmp_path, dataset, capsys):
    for option in ("--ask-kept", "--drop-kept"):
        with pytest.raises(SystemExit):
            longmemeval.main(["--data", str(dataset), option, str(tmp_path)])
        assert "has no kept.json" in capsys.readouterr().err


def test_dropping_a_kept_run_needs_no_data_file(tmp_path, capsys):
    with pytest.raises(SystemExit):
        longmemeval.main(["--drop-kept", str(tmp_path)])
    assert "has no kept.json" in capsys.readouterr().err   # past the arguments: --data was not asked for


def test_the_report_scores_the_evidence_rank_before_the_rerank(tmp_path, dataset, ollama, typesafe):
    # #242: the pool's rank is decided by the vectors and the word search, not the judge, so it is the measure
    # that isolates an embedding change; recall@5 also moves with the rerank.
    typesafe.default = 0.9
    report = run(tmp_path, dataset, ollama, typesafe)
    assert report["pool"] == {"count": 2, "recall_at_5": 1.0, "mrr": 1.0}


def test_supersede_pairs_count_the_facts_whose_cosine_reaches_each_threshold(db):
    # #242: the supersede check also reads the vectors (3.6: 0.92 without the judge, 0.80 with it), so a
    # different build could supersede differently; the counts show it.
    def insert(owner, body, vector, kind="fact"):
        owner.execute("insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding,"
                      " embedding_model) values (%s, %s, %s, %s, 'user_stated', 'p1', 'host-a', %s, 'm')",
                      (body, body, kind, "digest" if kind == "digest" else "explicit",
                       "[" + ",".join(map(str, vector)) + "]"))

    def vector(*head):
        return [*head, *[0.0] * (2560 - len(head))]

    with psycopg.connect(db["owner"], autocommit=True) as owner:
        insert(owner, "a", vector(1.0, 0.0))
        insert(owner, "b", vector(1.0, 0.0))     # cosine 1 with a
        insert(owner, "c", vector(0.85, 0.527))  # cosine 0.85 with a and b
        insert(owner, "d", vector(0.0, 1.0))     # cosine under 0.80 with every other
        insert(owner, "e", vector(1.0, 0.0), kind="digest")   # never compared: the check reads facts only
    assert longmemeval.fact_pairs(db["owner"]) == {"without_judge": 1, "ask_judge_from": 3}


@pytest.mark.parametrize("given", [["--ids", "q1"], ["--questions", "3"], ["--seed", "2"], ["--keep"]])
def test_asking_a_kept_run_again_takes_no_other_choice_of_questions(dataset, capsys, given):
    with pytest.raises(SystemExit):
        longmemeval.main(["--data", str(dataset), "--ask-kept", "kept", *given])
    assert "--ask-kept asks the kept run's questions" in capsys.readouterr().err


@pytest.fixture
def hosted(tmp_path, deepinfra, monkeypatch):
    """The hosted route pointed at the fake, here and in each replay, with a reference recorded against it."""
    reference = tmp_path / "embed_reference.json"
    reference.write_text(json.dumps({"text": "The reference sentence.", "vector": deepinfra.other, "threshold": 0.999}),
                         encoding="utf-8")
    from custom_build.memory import embedding
    monkeypatch.setattr(embedding, "HOSTED_URL", deepinfra.url)
    monkeypatch.setattr(embedding, "REFERENCE", reference)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key-not-a-real-one")
    return {"EVAL_TEST_HOSTED_URL": deepinfra.url, "EVAL_TEST_REFERENCE": str(reference)}


def test_a_kept_run_asked_on_the_hosted_route_re_embeds_each_store_then_asks_through_it(
        tmp_path, dataset, ollama, typesafe, deepinfra, hosted):
    # #239's comparison: the same facts asked with local vectors, then re-embedded and asked with hosted ones.
    typesafe.default = 0.9
    kept = tmp_path / "kept"
    try:
        local = run(tmp_path, dataset, ollama, typesafe, keep=True, out=kept)
        embedded_locally = len(ollama.embedded)
        again = run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=tmp_path / "hosted", embedding="deepinfra",
                    test_env=hosted)
        names = json.loads((kept / "kept.json").read_text(encoding="utf-8"))["databases"]
        builds = set()
        for name in names.values():
            owner = psycopg.conninfo.make_conninfo(os.environ["TEST_DATABASE_URL"], dbname=name)
            with psycopg.connect(owner) as conn:
                builds |= {row[0] for row in conn.execute("select embedding_model from memory.facts")}
    finally:
        longmemeval.drop_kept(os.environ["TEST_DATABASE_URL"], kept)
    assert builds == {"deepinfra:Qwen/Qwen3-Embedding-4B"}
    assert len(ollama.embedded) == embedded_locally   # the hosted pass never reached Ollama
    sent = [text for _, body in deepinfra.requests for text in body["input"]]
    assert "Which editor does the user use?" in sent and "I edit all my code in vim, every day." in sent
    assert again["embedding"] == "deepinfra" and local["embedding"] == "local"
    assert again["answerable"] == local["answerable"]   # the fakes give both routes the same vectors
    # q2_abs stored no fact (its session is small talk), so there was nothing to move or back up.
    assert sorted(path.name for path in (tmp_path / "hosted").glob("backup-*.json")) == ["backup-q1.json",
                                                                                          "backup-q3.json"]


def test_a_run_on_the_hosted_route_embeds_through_it_from_the_start(tmp_path, dataset, ollama, typesafe, deepinfra,
                                                                     hosted):
    typesafe.default = 0.9
    report = run(tmp_path, dataset, ollama, typesafe, embedding="deepinfra", test_env=hosted)
    assert ollama.embedded == [] and deepinfra.requests
    assert report["embedding_build"] == "deepinfra:Qwen/Qwen3-Embedding-4B"
    assert report["answerable"] == {"count": 2, "recall_at_5": 1.0, "mrr": 1.0}


def test_a_hosted_run_without_a_reference_stops_before_any_question(tmp_path, dataset, ollama, typesafe, deepinfra,
                                                                    hosted, monkeypatch):
    # #239 review: it extracted every turn on Haiku, then failed every store as embed.model_mismatch.
    from custom_build.memory import embedding
    monkeypatch.setattr(embedding, "REFERENCE", tmp_path / "missing.json")
    with pytest.raises(longmemeval.EvalError, match="reference"):
        run(tmp_path, dataset, ollama, typesafe, embedding="deepinfra", test_env=hosted)
    assert ollama.embedded == [] and typesafe.requests == [] and _nothing_left()


def test_a_second_hosted_pass_and_a_kept_backup_are_handled(tmp_path, dataset, ollama, typesafe, deepinfra, hosted):
    # #239 review: stores already moved need no move or backup; a re-embed that fails is an EvalError.
    typesafe.default = 0.9
    kept = tmp_path / "kept"
    try:
        run(tmp_path, dataset, ollama, typesafe, keep=True, ids=["q1"], out=kept)
        (tmp_path / "taken").mkdir()
        (tmp_path / "taken" / "backup-q1.json").write_text("[]", encoding="utf-8")
        with pytest.raises(longmemeval.EvalError, match="already exists"):
            run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=tmp_path / "taken", embedding="deepinfra",
                test_env=hosted)
        first = run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=tmp_path / "first", embedding="deepinfra",
                    test_env=hosted)
        second = run(tmp_path, dataset, ollama, typesafe, ask_kept=kept, out=tmp_path / "second",
                     embedding="deepinfra", test_env=hosted)
    finally:
        longmemeval.drop_kept(os.environ["TEST_DATABASE_URL"], kept)
    assert first["per_question"][0]["reembedded"] == {"facts": 1, "redacted": []}
    assert second["per_question"][0]["reembedded"] is None and not list((tmp_path / "second").glob("backup-*"))


def test_importing_the_runner_writes_nothing_to_the_hermes_home(tmp_path):
    # #239 review: a top-level import of the plugin loaded Hermes, which made folders in $HERMES_HOME.
    home = tmp_path / "home"
    home.mkdir()
    import subprocess
    result = subprocess.run([sys.executable, "-c", "import evals.longmemeval"], cwd=ROOT, capture_output=True,
                            text=True, env={**os.environ, "HERMES_HOME": str(home)}, check=False)
    assert result.returncode == 0, result.stderr
    assert list(home.iterdir()) == []


def test_the_hosted_route_needs_deepinfras_key(dataset, capsys, monkeypatch):
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        longmemeval.main(["--data", str(dataset), "--embedding", "deepinfra"])
    assert "DEEPINFRA_API_KEY" in capsys.readouterr().err


def test_a_turn_with_no_reply_is_not_synced_as_hermes_never_does():
    session = [{"role": "user", "content": "First."}, {"role": "user", "content": "Second."},
               {"role": "assistant", "content": "Reply."}, {"role": "user", "content": "Unanswered."}]
    from evals import replay
    assert [(user, reply) for user, reply, _ in replay.turns(session)] == [("Second.", "Reply.")]
