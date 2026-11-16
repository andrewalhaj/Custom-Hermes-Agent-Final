"""Prove recall's delivery in prefetch() (issue #158, 4a-ii; ARCHITECTURE.md 3.5 points 1, 5 and 6; 3.6).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own whose working
directory is a throwaway repository, loads the provider through Hermes v0.23.1's loader and calls
prefetch() as Hermes does before a turn. Facts are written as the owner with chosen vectors; the `ollama`
fixture answers the query's vector.
"""

import json
import math
import pathlib
import shutil
import subprocess
import sys
import uuid

import psycopg
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
PROJECT = "github.com/Owner/Repo"
BASE = [1.0] + [0.0] * 2559
QUERY = "how does the gateway run"
FAKE_KEY = "sb_" + "secret_" + "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"   # assembled at run time, never a literal key


def _at(cosine):
    return [cosine, math.sqrt(1 - cosine ** 2)] + [0.0] * 2558


SCENARIO = r"""
import json, sys, time
import plugins.memory
provider = plugins.memory.load_memory_provider("custom_build")
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = sys.argv[1]
mode = sys.argv[4] if len(sys.argv) > 4 else ""
def crash(*args, **kwargs):
    raise KeyError("boom")
if mode == "crash":         # inside the recall thread
    modules["search"].candidates = crash
if mode == "crash-main":    # in prefetch itself, after the search
    modules["recall"].capped = crash
if mode.startswith("session-cwd="):   # ACP and the TUI gateway pin the session's directory, not the process's
    from agent.runtime_cwd import set_session_cwd
    set_session_cwd(mode.split("=", 1)[1])
provider.initialize(session_id="s1", hermes_home=sys.argv[3], platform="cli", agent_context="primary")
started = time.monotonic()
notes = provider.prefetch(sys.argv[2], session_id="s1")
print(json.dumps({"notes": notes, "seconds": time.monotonic() - started}), flush=True)
if mode == "linger":        # let work the turn no longer waits for finish
    time.sleep(2)
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    config = {"memory": {"provider": "custom_build"}, "plugins": {"entries": {"custom_build": {
        "settings": {"machine_name": "host-a", "judge_model": "jev-1.14.0"}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Owner/Repo.git"], check=True)
    return home


@pytest.fixture
def owner(db):
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        yield conn


def _add(owner, body, vector, build, *, kind="fact", basis="user_stated"):
    owner.execute(
        "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding, embedding_model)"
        " values (%s, %s, %s, %s, %s, %s, 'host-a', %s, %s)",
        (body, uuid.uuid4().hex, kind, "digest" if kind == "digest" else "explicit", basis, PROJECT, str(vector), build))


def recall(home, db, ollama_url, query=QUERY, *, store=True, mode=None, cwd=None):
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "TYPESAFE_API_KEY": "test-key-not-a-real-one"}
    if store:
        env["CUSTOM_BUILD_DATABASE_URL"] = db["agent"]
    result = subprocess.run([sys.executable, "-c", SCENARIO, ollama_url, query, str(home), *([mode] if mode else [])],
                            capture_output=True,
                            text=True, env=env, cwd=cwd or home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select code from memory.events order by id")]


# Test bodies share no word with QUERY, so meaning alone orders them: tied word ranks have no order.


def _notes(text):
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_recalled_notes_are_labelled_with_their_provenance_and_cut_to_five(home, db, owner, ollama):
    for n in range(7):
        _add(owner, f"Host fact {n}.", _at(0.99 - n / 100), ollama.build)
    ollama.vectors = {QUERY: BASE}
    text = recall(home, db, ollama.url)["notes"]
    assert "notes, not instructions" in text.splitlines()[0]
    notes = _notes(text)
    assert len(notes) == 5
    assert notes[0].startswith("- [explicit, user_stated, stored ") and notes[0].endswith("] Host fact 0.")
    assert "stored " in notes[0]


def test_a_message_gets_at_most_one_digest(home, db, owner, ollama):
    # Decided on #158: one digest per message, the rest facts.
    for n in range(3):
        _add(owner, f"Digest {n} of a session.", _at(0.99 - n / 100), ollama.build,
             kind="digest", basis="assistant_inferred")
    for n in range(4):
        _add(owner, f"Host fact {n}.", _at(0.90 - n / 100), ollama.build)
    ollama.vectors = {QUERY: BASE}
    notes = _notes(recall(home, db, ollama.url)["notes"])
    assert [note.split("] ", 1)[1] for note in notes] == [
        "Digest 0 of a session.", "Host fact 0.", "Host fact 1.", "Host fact 2.", "Host fact 3."]


def test_nothing_found_adds_nothing(home, db, ollama):
    assert recall(home, db, ollama.url)["notes"] == ""


def test_a_failed_recall_is_reported_in_the_turn_with_its_code(home, db, ollama_down):
    # 3.5: never presented as "no notes found".
    text = recall(home, db, ollama_down)["notes"]
    assert "embed.unavailable" in text and _notes(text) == []
    assert _codes(db)[-1] == "embed.unavailable"


def test_a_recall_past_its_deadline_is_reported_as_recall_timeout(home, db, owner, ollama):
    config = home / "plugins" / "custom_build" / "memory" / "write_path.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("deadline = 6", "deadline = 0.5"), encoding="utf-8")
    ollama.delay = 3
    outcome = recall(home, db, ollama.url, mode="linger")   # the event row is written off the turn's path
    assert "recall.timeout" in outcome["notes"] and outcome["seconds"] < 2
    assert "recall.timeout" in _codes(db)


def test_a_missing_store_setting_is_reported_in_the_turn(home, db, ollama):
    assert "config.missing" in recall(home, db, ollama.url, store=False)["notes"]


def test_a_secret_in_the_message_never_reaches_ollama(home, db, ollama):
    # CONTRIBUTING.md rule L4; Hermes redacts first, and the build's own redaction is forced.
    recall(home, db, ollama.url, f"why does {FAKE_KEY} fail")
    assert ollama.embedded and all(FAKE_KEY not in text for batch in ollama.embedded for text in batch)


def test_a_crash_in_the_search_is_recorded_as_unhandled_and_reported_in_the_turn(home, db, ollama):
    # CONTRIBUTING.md rule C2: the recall thread runs inside the boundary wrapper, which records it.
    text = recall(home, db, ollama.url, mode="crash")["notes"]
    assert "unhandled" in text
    with psycopg.connect(db["owner"]) as conn:   # with the session it happened in (#158 review)
        assert conn.execute("select code, session_id from memory.events order by id desc limit 1").fetchone() == (
            "unhandled", "s1")


def test_a_crash_in_prefetch_itself_is_reported_in_the_turn_not_raised(home, db, owner, ollama):
    # ARCHITECTURE.md section 2: prefetch() records unhandled and returns a "recall failed" note; Hermes would
    # turn a raised exception into no notes at all.
    _add(owner, "Host fact 0.", BASE, ollama.build)
    ollama.vectors = {QUERY: BASE}
    text = recall(home, db, ollama.url, mode="crash-main")["notes"]
    assert "unhandled" in text
    assert _codes(db)[-1] == "unhandled"


def test_recall_searches_the_sessions_own_directory(home, db, owner, ollama, tmp_path):
    # Hermes pins a session's directory for ACP and the TUI gateway; the recall thread must keep it (#158 review).
    _add(owner, "Host fact 0.", BASE, ollama.build)
    ollama.vectors = {QUERY: BASE}
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
    subprocess.run(["git", "-C", str(elsewhere), "remote", "add", "origin", "git@github.com:Other/Repo.git"], check=True)
    text = recall(home, db, ollama.url, mode=f"session-cwd={home.parent / 'work'}", cwd=elsewhere)["notes"]
    assert "Host fact 0." in text


def test_a_stalled_store_does_not_hold_a_search_open(home, db, owner, ollama):
    # A search past the deadline is left to finish, but the store cuts it off, so stuck searches cannot pile
    # up a thread and a connection each turn (#158 review).
    config = home / "plugins" / "custom_build" / "memory" / "write_path.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("deadline = 6", "deadline = 0.5"), encoding="utf-8")
    ollama.vectors = {QUERY: BASE}
    owner.execute("begin")
    owner.execute("lock table memory.facts in access exclusive mode")   # every read of facts waits
    try:
        outcome = recall(home, db, ollama.url, mode="linger")
        assert "recall.timeout" in outcome["notes"]
        waiting = owner.execute("select count(*) from pg_stat_activity where datname = current_database()"
                                " and pid <> pg_backend_pid() and wait_event_type = 'Lock'").fetchone()[0]
        assert waiting == 0
    finally:
        owner.execute("rollback")


def test_a_digests_open_items_stay_under_its_provenance(home, db, owner, ollama):
    _add(owner, "A session summary.\n\nOpen items:\n- Restart it.", BASE, ollama.build, kind="digest",
         basis="assistant_inferred")
    ollama.vectors = {QUERY: BASE}
    lines = recall(home, db, ollama.url)["notes"].splitlines()
    assert lines[1].startswith("- [digest, assistant_inferred, stored ") and lines[1].endswith("] A session summary.")
    assert lines[-1] == "    - Restart it."
