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


def test_the_supersede_decision_needs_both_answers_at_the_threshold():
    assert judge.supersedes({"same_subject": 0.75, "supersedes": 0.75}) is True
    assert judge.supersedes({"same_subject": 0.99, "supersedes": 0.74}) is False
    assert judge.supersedes({"same_subject": 0.74, "supersedes": 0.99}) is False
