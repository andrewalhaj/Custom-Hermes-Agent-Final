"""Prove the judge client for the supersede decision (issue #113, ARCHITECTURE.md 3.3 and 3.6).

The `typesafe` fixture (conftest.py) stands in for TypeSafe's System One endpoint. The questions'
wording lives in the write path's configuration file, beside the thresholds that read the answers.
"""

import pytest

from custom_build.memory import judge

KEY = "test-key-not-a-real-one"


def _ask(typesafe, timeout=2):
    return judge.supersede_answers("The mini runs Ubuntu 22.04.", "The mini runs Ubuntu 24.04 now.",
                                   url=typesafe.url, key=KEY, model="jev-1.14.0", timeout=timeout)


def test_both_answers_come_back_by_question(typesafe):
    typesafe.answers = {"same_subject": 0.97, "supersedes": 0.91}
    assert _ask(typesafe) == {"same_subject": 0.97, "supersedes": 0.91}


def test_one_request_carries_both_questions_the_pair_and_the_pinned_model(typesafe):
    typesafe.answers = {"same_subject": 0.5, "supersedes": 0.5}
    _ask(typesafe)
    [request] = typesafe.requests
    assert request["path"] == "/v1/systemone"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert request["body"]["model"] == "jev-1.14.0"
    assert request["body"]["state"] == {"stored": "The mini runs Ubuntu 22.04.", "new": "The mini runs Ubuntu 24.04 now."}
    assert sorted(request["body"]["questions"]) == ["same_subject", "supersedes"]
    assert all(q["type"] == "noul" for q in request["body"]["questions"].values())


@pytest.mark.parametrize("status", [400, 422, 500])
def test_an_http_error_is_a_judge_error(typesafe, status):
    typesafe.status = status
    with pytest.raises(judge.JudgeError):
        _ask(typesafe)


def test_a_judge_that_cannot_be_reached_is_a_judge_error(ollama_down):
    with pytest.raises(judge.JudgeError):
        judge.supersede_answers("a stored fact", "a new fact", url=ollama_down, key=KEY, model="jev-1.14.0", timeout=2)


def test_a_judge_past_its_deadline_is_a_timeout(typesafe):
    typesafe.answers = {"same_subject": 0.9, "supersedes": 0.9}
    typesafe.delay = 1.5
    with pytest.raises(judge.JudgeTimeout):
        _ask(typesafe, timeout=0.5)


@pytest.mark.parametrize("break_it", [
    {"raw": b"<html>not json</html>"},                      # a 200 that is not JSON
    {"raw": b'{"model": "jev-1.14.0", "usage": {}}'},        # no answers
    {"raw": b'{"answers": {"same_subject": {"noul": 0.9}}}'},  # one question unanswered
    {"cut_off": True},                                      # the answer cut off mid-body
    {"hang_up": True},                                      # the connection closed before any answer
])
def test_a_malformed_or_broken_answer_is_a_judge_error(typesafe, break_it):
    typesafe.answers = {"same_subject": 0.9, "supersedes": 0.9}
    for name, value in break_it.items():
        setattr(typesafe, name, value)
    with pytest.raises(judge.JudgeError):
        _ask(typesafe)


def test_the_supersede_decision_needs_both_answers_at_the_threshold():
    assert judge.supersedes({"same_subject": 0.75, "supersedes": 0.75}) is True
    assert judge.supersedes({"same_subject": 0.99, "supersedes": 0.74}) is False
    assert judge.supersedes({"same_subject": 0.74, "supersedes": 0.99}) is False


# The gate on an extracted fact (#148, ARCHITECTURE.md 3.3 step 3): worth keeping, and supported by its source.
FACT = "The mini's gateway runs as the hermes user."
SOURCE = {"user": "Why did the gateway fail?", "tool_results": ["User=hermes\nExecStart=/usr/bin/hermes gateway"]}


def _gate(typesafe):
    return judge.keep_answers(FACT, SOURCE, url=typesafe.url, key=KEY, model="jev-1.14.0", timeout=2)


def test_one_request_asks_both_gate_questions_about_the_fact_and_its_source(typesafe):
    typesafe.answers = {"worth_keeping": 0.9, "supported": 0.8}
    assert _gate(typesafe) == {"worth_keeping": 0.9, "supported": 0.8}
    [request] = typesafe.requests
    assert request["body"]["state"] == {"fact": FACT, "source": SOURCE}
    assert sorted(request["body"]["questions"]) == ["supported", "worth_keeping"]


# ARCHITECTURE.md 3.6 (#152): the gate drops at a drop score (1 - "worth keeping") of 0.70 or higher; the
# support check needs "supported" at 0.75 or higher.
@pytest.mark.parametrize("answers, kept", [
    ({"worth_keeping": 0.9, "supported": 0.8}, True),
    ({"worth_keeping": 0.31, "supported": 0.75}, True),   # drop score 0.69: kept
    ({"worth_keeping": 0.30, "supported": 0.95}, False),  # drop score 0.70: dropped
    ({"worth_keeping": 0.9, "supported": 0.74}, False),   # not supported by its source
])
def test_an_extracted_fact_is_kept_by_the_gate_and_the_support_check(answers, kept):
    assert judge.keeps(answers) is kept


def test_a_gate_failure_is_a_judge_error(typesafe):
    typesafe.status = 500
    with pytest.raises(judge.JudgeError):
        _gate(typesafe)


# The gate on a digest (#155, ARCHITECTURE.md 3.3): one question, dropped at the same drop score (3.6).
DIGEST = "The session moved the mini's gateway to the hermes user.\n\nOpen items:\n- Restart the gateway."


def test_one_request_asks_whether_the_digest_is_worth_keeping(typesafe):
    typesafe.answers = {"worth_keeping": 0.8}
    assert judge.digest_answers(DIGEST, url=typesafe.url, key=KEY, model="jev-1.14.0", timeout=2) == {"worth_keeping": 0.8}
    [request] = typesafe.requests
    assert request["body"]["state"] == {"digest": DIGEST}
    assert list(request["body"]["questions"]) == ["worth_keeping"]


@pytest.mark.parametrize("probability, kept", [(0.31, True), (0.30, False)])
def test_a_digest_is_dropped_at_the_gates_drop_score(probability, kept):
    assert judge.keeps_digest({"worth_keeping": probability}) is kept



# The rerank (4b, #165): one Noul per candidate, each with the candidate's text in its own question.
def test_one_request_asks_how_much_each_note_helps_the_message(typesafe):
    typesafe.answers = {"c0": 0.2, "c1": 0.9}
    answers = judge.relevance_answers("how does the gateway run", ["Fact A.", "Fact B."], url=typesafe.url, key=KEY,
                                      model="jev-1.14.0", timeout=2)
    assert answers == [0.2, 0.9]
    [request] = typesafe.requests
    assert request["body"]["state"] == {"message": "how does the gateway run"}
    assert sorted(request["body"]["questions"]) == ["c0", "c1"]
    assert "Fact A." in request["body"]["questions"]["c0"]["instructions"]


def test_a_rerank_failure_is_a_judge_error(typesafe):
    typesafe.status = 500
    with pytest.raises(judge.JudgeError):
        judge.relevance_answers("m", ["Fact A."], url=typesafe.url, key=KEY, model="jev-1.14.0", timeout=2)
