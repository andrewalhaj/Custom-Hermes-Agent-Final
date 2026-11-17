"""One LongMemEval question replayed through the build, in a process of its own (issue #167).

`ingest` drives the memory provider as Hermes does over the question's chat history: each session starts
the provider with the session's id, each finished user turn is `on_turn_start` then `sync_turn` (extraction,
the judge's gate, the store), and each session ends with `on_session_end` (the digest). `ask` starts a session
for the question and prints what `prefetch()` returns. The provider loads through Hermes's own loader from
the Hermes home in HERMES_HOME, so the replay runs the plugin exactly as an agent host would.

Usage: python -m evals.replay ingest|ask <question.json> <hermes home>
"""

import datetime
import json
import os
import pathlib
import sys

import plugins.memory
import psycopg

DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"   # LongMemEval's haystack_dates


def _modules():
    """The plugin's modules as the provider load imported them, by their last name (for a test's fakes)."""
    return {name.rsplit(".", 1)[-1]: module for name, module in sys.modules.items() if "custom_build" in name}


def turns(session):
    """(user text, assistant reply, the messages so far) for each user turn that got a reply, as Hermes syncs a
    finished turn; Hermes never syncs a turn with no reply (run_agent.py, _sync_external_memory_for_turn)."""
    messages, user = [], None
    for message in session:
        messages.append({"role": message["role"], "content": message["content"]})
        if message["role"] == "user":
            user = message["content"]
        elif user is not None and message["content"]:
            yield user, message["content"], list(messages)
            user = None


# A failure the run must stop at: the account refused (#148's alert), or a plan limit, which an extraction or
# digest call reports as its error (a 429 from the provider). Calling on would only repeat it.
STOP = ("select code from memory.events where code in ('extract.refused', 'digest.refused')"
        " or (code in ('extract.error', 'digest.error') and (reason ilike '%%limit%%' or reason like '%%429%%'))"
        " order by id limit 1")


def _stopped():
    with psycopg.connect(os.environ["CUSTOM_BUILD_DATABASE_URL"]) as conn:
        row = conn.execute(STOP).fetchone()
    return row[0] if row else None


def ingest(provider, home, entry):
    """Replay the history, sessions in date order; the code to stop at, as soon as one is recorded, or None."""
    sessions = sorted(zip(entry["haystack_dates"], entry["haystack_session_ids"], entry["haystack_sessions"],
                          strict=True), key=lambda item: datetime.datetime.strptime(item[0], DATE_FORMAT))
    for _, session_id, session in sessions:
        provider.initialize(session_id=session_id, hermes_home=home, platform="cli", agent_context="primary")
        for number, (user, reply, messages) in enumerate(turns(session), start=1):
            provider.on_turn_start(number, user)
            provider.sync_turn(user, reply, session_id=session_id, messages=messages)
            if stopped := _stopped():
                return stopped
        provider.on_session_end([{"role": message["role"], "content": message["content"]} for message in session])
        if stopped := _stopped():
            return stopped
    return None


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
        print(json.dumps({"ingested": entry["question_id"], "stopped": ingest(provider, home, entry)}))
    else:
        print(json.dumps({"notes": ask(provider, home, entry)}))


if __name__ == "__main__":
    main(sys.argv[1:])
