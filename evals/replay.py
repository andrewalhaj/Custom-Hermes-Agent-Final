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
from agent.redact import redact_sensitive_text
from agent.runtime_cwd import resolve_agent_cwd
from hermes_cli.plugins import discover_plugins

DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"   # LongMemEval's haystack_dates


def _modules():
    """The plugin's modules as the provider load imported them, by their last name (for a test's fakes). The
    plugin is in memory twice, from discovery and from the provider load; the later load wins the name, so
    this relies on main() discovering plugins before it loads the provider."""
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


# A failure that repeats: when every one of a question's first extractions failed, whatever the reason, the
# rest would fail the same way and the run would measure nothing (#171).
FIRST_CALLS = 5


def _stopped(synced):
    """The code to stop at, after `synced` turns of this question."""
    with psycopg.connect(os.environ["CUSTOM_BUILD_DATABASE_URL"]) as conn:
        if row := conn.execute(STOP).fetchone():
            return row[0]
        if synced == FIRST_CALLS and conn.execute(
                "select count(*) from memory.events where code = 'extract.error'").fetchone()[0] >= FIRST_CALLS:
            return "extract.error"
    return None


def ingest(provider, home, entry):
    """Replay the history, sessions in date order; the code to stop at, as soon as one is recorded, or None."""
    sessions = sorted(zip(entry["haystack_dates"], entry["haystack_session_ids"], entry["haystack_sessions"],
                          strict=True), key=lambda item: datetime.datetime.strptime(item[0], DATE_FORMAT))
    synced = 0
    for _, session_id, session in sessions:
        provider.initialize(session_id=session_id, hermes_home=home, platform="cli", agent_context="primary")
        for number, (user, reply, messages) in enumerate(turns(session), start=1):
            provider.on_turn_start(number, user)
            provider.sync_turn(user, reply, session_id=session_id, messages=messages)
            synced += 1
            if stopped := _stopped(synced):
                return stopped
        provider.on_session_end([{"role": message["role"], "content": message["content"]} for message in session])
        if stopped := _stopped(synced):
            return stopped
    return None


def ask(provider, home, entry):
    session_id = f"eval-{entry['question_id']}"
    provider.initialize(session_id=session_id, hermes_home=home, platform="cli", agent_context="primary")
    provider.on_turn_start(1, entry["question"])
    return provider.prefetch(entry["question"], session_id=session_id)


def pool(provider, entry):
    """The candidates recall searched for the question, each with the judge's score and no floor applied (#234),
    as [{"body", "score"}]; a score is None when the judge could not answer. One more judge call, no model call.
    The query, project and number wanted are recall's own (tool.recall_notes)."""
    modules = _modules()
    settings, judge = modules["settings"], modules["judge"]
    query = redact_sensitive_text(entry["question"], force=True)
    limits = modules["write_path"].load()["recall"]
    wanted = limits["results_past_work"] if modules["recall"].question_kind(query) == "past_work" else limits["results"]
    embedding = modules["embedding"]
    try:
        with settings.connect() as conn:
            found = modules["search"].candidates(conn, provider.embedder, query,
                                                 modules["project"].key(resolve_agent_cwd()), wanted=wanted, timeout=5)
    except (embedding.EmbedUnavailable, embedding.ModelMismatch, psycopg.Error):
        return []   # the search's own failures: recall failed too, and the runner counts that, not a miss
    if not found:
        return []
    try:
        scores = judge.relevance_answers(query, [candidate["body"] for candidate in found], url=judge.URL,
                                         key=os.environ["TYPESAFE_API_KEY"],
                                         model=settings.plugin_context().get_config(settings.JUDGE_MODEL),
                                         timeout=judge.DEADLINE)
    except (judge.JudgeError, judge.JudgeTimeout):
        scores = [None] * len(found)
    return [{"body": candidate["body"], "score": score} for candidate, score in zip(found, scores, strict=True)]


def main(argv, patch=None):
    """`patch(modules)` runs after the provider loads; a test points the plugin at its fakes with it."""
    mode, question, home = argv
    entry = json.loads(pathlib.Path(question).read_text(encoding="utf-8"))
    discover_plugins()   # as Hermes starts: the plugin registers its extraction and digest routes
    provider = plugins.memory.load_memory_provider("custom_build")
    if patch:
        patch(_modules())
    if mode == "ingest":
        print(json.dumps({"ingested": entry["question_id"], "stopped": ingest(provider, home, entry)}))
    else:
        notes = ask(provider, home, entry)   # first, as Hermes asks it
        print(json.dumps({"notes": notes, "pool": pool(provider, entry)}))


if __name__ == "__main__":
    main(sys.argv[1:])
