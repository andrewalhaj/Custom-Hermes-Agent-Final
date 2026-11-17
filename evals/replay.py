"""One LongMemEval question replayed through the build, in a process of its own (issue #167).

`ingest` drives the memory provider as Hermes does over the question's chat history: each session starts
the provider with the session's id, each finished user turn is `on_turn_start` then `sync_turn` (extraction,
the judge's gate, the store), and each session ends with `on_session_end` (the digest). `ask` starts a session
for the question and prints what `prefetch()` returns. The provider loads through Hermes's own loader from
the Hermes home in HERMES_HOME, so the replay runs the plugin exactly as an agent host would.

Usage: python -m evals.replay ingest|ask <question.json> <hermes home>
"""

import json
import pathlib
import sys

import plugins.memory


def _modules():
    """The plugin's modules as the provider load imported them, by their last name (for a test's fakes)."""
    return {name.rsplit(".", 1)[-1]: module for name, module in sys.modules.items() if "custom_build" in name}


def turns(session):
    """(user text, assistant reply, the messages so far) for each user turn, as Hermes syncs a finished turn.
    A user message with no reply before the next one is synced with an empty reply."""
    messages, user = [], None
    for message in session:
        if message["role"] == "user" and user is not None:
            yield user, "", list(messages)
        messages.append({"role": message["role"], "content": message["content"]})
        if message["role"] == "user":
            user = message["content"]
        elif user is not None:
            yield user, message["content"], list(messages)
            user = None
    if user is not None:
        yield user, "", list(messages)


def ingest(provider, home, entry):
    for session_id, session in zip(entry["haystack_session_ids"], entry["haystack_sessions"], strict=True):
        provider.initialize(session_id=session_id, hermes_home=home, platform="cli", agent_context="primary")
        for number, (user, reply, messages) in enumerate(turns(session), start=1):
            provider.on_turn_start(number, user)
            provider.sync_turn(user, reply, session_id=session_id, messages=messages)
        provider.on_session_end([{"role": message["role"], "content": message["content"]} for message in session])


def ask(provider, home, entry):
    session_id = f"eval-{entry['question_id']}"
    provider.initialize(session_id=session_id, hermes_home=home, platform="cli", agent_context="primary")
    provider.on_turn_start(1, entry["question"])
    return provider.prefetch(entry["question"], session_id=session_id)


def main(argv, patch=None):
    """`patch(modules)` runs after the provider loads; a test points the plugin at its fakes with it."""
    mode, question, home = argv
    entry = json.loads(pathlib.Path(question).read_text(encoding="utf-8"))
    provider = plugins.memory.load_memory_provider("custom_build")
    if patch:
        patch(_modules())
    if mode == "ingest":
        ingest(provider, home, entry)
        print(json.dumps({"ingested": entry["question_id"]}))
    else:
        print(json.dumps({"notes": ask(provider, home, entry)}))


if __name__ == "__main__":
    main(sys.argv[1:])
