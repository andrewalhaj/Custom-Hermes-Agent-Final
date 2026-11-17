"""Prove the questions recall treats differently (issue #158, 4a-iii; ARCHITECTURE.md 3.5 points 3-4, 5.3).

The patterns are the recall gate's, in custom_build/guards/recall_gate.toml, so the search and the gate
agree on what a question about past work is. The end-to-end cases are in test_recall_delivery.py.
"""

import pytest

from custom_build.memory import recall


@pytest.mark.parametrize("message", [
    "What did we decide last time about the store?",
    "Did we already fix the gateway?",
    "Remember the backup job we set up?",
    "Where did we leave off?",
    "What did I say about Ollama earlier?",
    "Have we tried pinning the model before?",
    # #158 review: plain questions the first patterns missed
    "What were we working on?",
    "Where were we?",
    "Remind me what we did on the backup.",
    "Have we seen this error before",
    "What did we conclude about the judge?",
])
def test_a_question_about_past_work_is_recognised(message):
    assert recall.question_kind(message) == "past_work"


@pytest.mark.parametrize("message", [
    "What is left to do?",
    "what's next",
    "Any open items on the mini?",
    "What's still to do before release?",
    "What's left from last session?",   # open work wins over past work
    # #158 review: plain questions the first patterns missed
    "Whats next",
    "What remains to be done?",
    "What else is left?",
    "anything left to do?",
    "What do we still need to do?",
    "What's on the todo list?",
])
def test_a_question_about_open_work_is_recognised(message):
    assert recall.question_kind(message) == "open_work"


@pytest.mark.parametrize("message", [
    "How many facts are in the store?",
    "Fix the gateway on the mini.",
    "What is the latest version of Postgres?",
    "Install Ollama on the mini.",
    "Did the build pass?",   # a question, but not about past work in memory
    # #158 review: ordinary messages the first patterns took for open work, which replaced the search
    "Explain what next() does in Python",
    "In this code, what's next(iter(x)) return?",
    "what is pending in kubectl get pods -A",
    "Show me what's remaining in the queue",
    "The function returns what is left over after division",
    "what are left and right joins",
    "List open tasks in Jira for sprint 3",
    "Why are there open items in the GitHub milestone?",
    # ...and for past work, which the recall gate will turn into required searches
    "Remember to run the migrations before you deploy.",
    "Please remember: never push to main.",
    "Compute precision and recall for the classifier.",
    "Write a function to recall the last 10 items.",
    "Did you set the env var?",
    "Have you tried restarting it?",
    "Did I install it right?",
    "Revert to the previous session token.",
    "Can you check if the job ran last week",
    "Traceback: ValueError: was it set previously?",
    "git log --since yesterday? or before?",
])
def test_an_ordinary_message_is_neither(message):
    assert recall.question_kind(message) is None


@pytest.mark.parametrize("message", ["what " * 50000, "did we " * 30000, "remember " * 30000, "- " * 100000])
def test_a_long_message_is_matched_in_linear_time(message):
    # A pasted log reaches prefetch whole, and the recall gate will match every message the same way.
    import time
    started = time.monotonic()
    recall.question_kind(message)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("body, items", [
    ("Did the work.\n\nOpen items:\n- Restart the gateway.\n- Check its logs.", ["Restart the gateway.", "Check its logs."]),
    # an item the model wrote over two lines, and one with a nested step: both stay one item
    ("Did the work.\n\nOpen items:\n- Restart the gateway\nthen check its logs.\n- Move the unit\n  - sub step",
     ["Restart the gateway then check its logs.", "Move the unit - sub step"]),
    # a summary that mentions open items, and lists bullets of its own, before the real section
    ("We listed Open items:\n- not an item\n\nOpen items:\n- The real item.", ["The real item."]),
    ("Nothing was left open.", []),
    ("Did the work.\n\nOpen items:\n- \n- Real.", ["Real."]),
])
def test_a_digests_open_items_are_read_as_written(body, items):
    digest = {"body": body, "kind": "digest", "origin": "digest", "basis": "assistant_inferred",
              "stored": "2026-11-16", "last_verified": None}
    assert [item["body"] for item in recall.open_items([digest])] == items
