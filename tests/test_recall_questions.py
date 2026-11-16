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
])
def test_a_question_about_past_work_is_recognised(message):
    assert recall.question_kind(message) == "past_work"


@pytest.mark.parametrize("message", [
    "What is left to do?",
    "what's next",
    "Any open items on the mini?",
    "What's still to do before release?",
    "What's left from last session?",   # open work wins over past work
])
def test_a_question_about_open_work_is_recognised(message):
    assert recall.question_kind(message) == "open_work"


@pytest.mark.parametrize("message", [
    "How many facts are in the store?",
    "Fix the gateway on the mini.",
    "What is the latest version of Postgres?",
    "Install Ollama on the mini.",
    "Did the build pass?",   # a question, but not about past work in memory
])
def test_an_ordinary_message_is_neither(message):
    assert recall.question_kind(message) is None
