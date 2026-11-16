"""Prove the pattern filter on facts stored on purpose (issue #113, ARCHITECTURE.md 3.3 and 3.6).

The minimum length and the narration patterns live in the write path's configuration file, so a
change to either is a pull request (CONTRIBUTING.md rule C8).
"""

import pytest

from custom_build.memory import write_filter


def test_a_plain_fact_passes():
    assert write_filter.refusal("The mini's gateway runs as the hermes user.") is None


@pytest.mark.parametrize("text", ["", "   ", "short", "  nine ch  "])
def test_text_under_the_minimum_length_is_refused(text):
    assert write_filter.refusal(text).startswith("shorter than 10 characters")


@pytest.mark.parametrize("text", [
    "I felt the deploy was risky, so I slowed down.",
    "I caught myself retrying the same command.",
    "Let me check the gateway logs next.",
    "I'm going to restart the service now.",
    "I will now open the pull request.",
])
def test_narration_is_refused(text):
    assert write_filter.refusal(text).startswith("narration")


def test_the_configuration_file_holds_the_minimum_and_the_patterns():
    config = write_filter.load()
    assert config["min_length"] == 10
    assert config["narration"], "no narration patterns configured"
