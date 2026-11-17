"""Prove the judge client for the supersede decision (issue #113, ARCHITECTURE.md 3.3 and 3.6).

The `typesafe` fixture (conftest.py) stands in for TypeSafe's System One endpoint. The questions'
wording lives in the write path's configuration file, beside the thresholds that read the answers.
"""

import json

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


def test_the_gate_asks_about_each_part_of_the_source_in_a_question_of_its_own(typesafe):
    # #251: the whole turn as state went over the judge's context. The state is the fact alone; the user's
    # message and each tool result have a support question each, with the part's own text in it.
    typesafe.answers = {"worth_keeping": 0.9, "s0": 0.1, "s1": 0.8}
    assert _gate(typesafe) == {"worth_keeping": 0.9, "supported": 0.8}   # supported by any one part
    [request] = typesafe.requests
    assert request["body"]["state"] == {"fact": FACT}
    questions = request["body"]["questions"]
    assert sorted(questions) == ["s0", "s1", "worth_keeping"]
    assert "The user's message: Why did the gateway fail?" in questions["s0"]["instructions"]
    assert "A tool result: User=hermes" in questions["s1"]["instructions"]
    assert all(q["type"] == "noul" for q in questions.values())


def _big_gate(typesafe, results):
    return judge.keep_answers(FACT, {"user": "Why?", "tool_results": results}, url=typesafe.url, key=KEY,
                              model="jev-1.14.0", timeout=2)


def test_a_long_tool_result_is_asked_about_in_pieces_and_none_of_it_is_left_out(typesafe):
    typesafe.default = 0.1
    long = "".join(f"line {n} of the unit file\n" for n in range(3500))   # about 90,000 characters
    _big_gate(typesafe, [long])
    pieces = [text.split("A tool result: ", 1)[1] for text in _texts(typesafe) if "A tool result: " in text]
    assert len(pieces) > 1 and all(len(piece) <= 30_000 for piece in pieces)
    assert "".join(pieces) == long


def _texts(typesafe):
    asked = {}
    for request in typesafe.requests:
        asked.update({name: q["instructions"] for name, q in request["body"]["questions"].items() if name != "worth_keeping"})
    return [asked[name] for name in sorted(asked, key=lambda name: int(name[1:]))]


def test_a_big_turn_is_split_over_requests_that_each_fit_the_judge(typesafe):
    typesafe.default = 0.1   # nothing supports it, so every part is asked about
    _big_gate(typesafe, ["x" * 25_000] * 6)
    assert len(typesafe.requests) > 1
    for request in typesafe.requests:
        assert len(json.dumps(request["body"])) <= 62_000   # the request budget, plus the questions' own wording
    asked = [name for request in typesafe.requests for name in request["body"]["questions"]]
    assert asked.count("worth_keeping") == 1 and len(asked) == len(set(asked)) == 8   # the message, 6 results, the gate


def test_the_gate_stops_asking_once_one_part_supports_the_fact(typesafe):
    typesafe.default = 0.9
    assert _big_gate(typesafe, ["x" * 25_000] * 6) == {"worth_keeping": 0.9, "supported": 0.9}
    assert len(typesafe.requests) == 1


def test_no_request_holds_more_question_text_than_the_request_budget(typesafe):
    # Review of #251: only the first request was held to the budget.
    typesafe.default = 0.1
    _big_gate(typesafe, ["x" * 30_000] * 4)
    for request in typesafe.requests:
        parts = [q["instructions"].split(": ", 1)[1] for name, q in request["body"]["questions"].items()
                 if name != "worth_keeping"]
        assert sum(map(len, parts)) <= 60_000   # the budget is the parts' own text
    assert [len(request["body"]["questions"]) for request in typesafe.requests] == [3, 2, 1]   # the gate, the message and
    # one result; two results; the last result


def test_a_fact_not_worth_keeping_is_not_asked_about_past_its_first_request(typesafe):
    typesafe.answers, typesafe.default = {"worth_keeping": 0.1}, 0.1
    assert _big_gate(typesafe, ["x" * 25_000] * 6)["worth_keeping"] == 0.1
    assert len(typesafe.requests) == 1


@pytest.mark.parametrize("detail", ["refused", {"error_type": "invalid_model"}])
def test_a_400_for_another_reason_is_an_ordinary_judge_error(typesafe, detail):
    # Review of #251: only a request over the judge's context is known to be refused every time.
    typesafe.status, typesafe.detail = 400, detail
    with pytest.raises(judge.JudgeError) as caught:
        _gate(typesafe)
    assert not isinstance(caught.value, judge.JudgeRefused)


def test_a_request_the_judge_refuses_as_too_big_names_its_cause(typesafe):
    typesafe.status, typesafe.detail = 400, {"error_type": "max_tokens_exceeded"}
    with pytest.raises(judge.JudgeRefused, match="max_tokens_exceeded"):
        _gate(typesafe)


def test_a_refusal_never_carries_text_the_judge_sent_back(typesafe):
    typesafe.status, typesafe.detail = 400, {"error_type": "the fact was: User=hermes on the mini"}
    with pytest.raises(judge.JudgeError) as caught:
        _gate(typesafe)
    assert "hermes" not in str(caught.value)


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
