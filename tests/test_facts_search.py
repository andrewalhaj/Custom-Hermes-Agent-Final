"""Prove the facts search: memory.search_facts and the memory_search tool (issue #158, 4a-i;
ARCHITECTURE.md 3.4 steps 1-3 and 6, 3.5 point 7, defaults in 3.6).

The `db` fixture gives an empty store with every migration applied; `ollama` an embedding service whose
vector for each text the test chooses. Facts are written as the owner, with chosen vectors, and searched
as a machine's login, as the provider searches.
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

from custom_build.memory import embedding, search

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
BASE = [1.0] + [0.0] * 2559


def _at(cosine):
    """A unit vector whose cosine with BASE is `cosine`."""
    return [cosine, math.sqrt(1 - cosine ** 2)] + [0.0] * 2558


def _add(owner, body, vector, *, project="p1", kind="fact", superseded=False, retired=False):
    fact = owner.execute(
        "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding, embedding_model)"
        " values (%s, %s, %s, %s, 'user_stated', %s, 'host-a', %s, 'qwen3-embedding:4b@test') returning id",
        (body, uuid.uuid4().hex, kind, "digest" if kind == "digest" else "explicit", project, str(vector))).fetchone()[0]
    if superseded:
        owner.execute("update memory.facts set superseded_by = %s where id = %s",
                      (_add(owner, body + " (newer)", _at(0.0), project=project), fact))
    if retired:
        owner.execute("update memory.facts set retired_at = now(), retired_reason = 'test' where id = %s", (fact,))
    return fact


def _search(db, query, vector=BASE, *, project="p1", pool=15):
    with psycopg.connect(db["agent"]) as conn:
        return conn.execute("select body, by_meaning, by_words from memory.search_facts(%s::halfvec, %s, %s, %s, 20)",
                            (str(vector), query, project, pool)).fetchall()


@pytest.fixture
def owner(db):
    with psycopg.connect(db["owner"], autocommit=True) as conn:
        yield conn


def test_a_machines_login_may_search_and_no_one_else(db):
    signature = "memory.search_facts(public.halfvec, text, text, integer, integer)"
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select has_function_privilege('memory_agent', %s, 'execute'),"
                            " has_function_privilege('memory_maintenance', %s, 'execute')",
                            (signature, signature)).fetchone() == (True, False)
        assert conn.execute("select count(*) from aclexplode((select proacl from pg_proc where proname = 'search_facts'))"
                            " where grantee = 0").fetchone() == (0,)   # 0 is PUBLIC


def test_a_fact_found_both_ways_ranks_above_one_found_by_meaning_alone(db, owner):
    _add(owner, "The mini runs Ubuntu 24.04.", _at(0.99))           # nearest by meaning, no shared word
    _add(owner, "The gateway on the mini restarts nightly.", _at(0.98))   # second by meaning, and by words
    assert _search(db, "gateway") == [("The gateway on the mini restarts nightly.", 2, 1),
                                      ("The mini runs Ubuntu 24.04.", 1, None)]


def test_a_fact_found_by_words_alone_is_returned(db, owner):
    _add(owner, "The gateway on the mini restarts nightly.", _at(0.0))
    rows = _search(db, "gateway", pool=1)   # the pool holds one row by meaning; it is the only fact
    assert [row[0] for row in rows] == ["The gateway on the mini restarts nightly."]


def test_any_of_the_querys_words_matches(db, owner):
    # 3.4: words joined with OR, so a long question still matches.
    _add(owner, "The gateway on the mini restarts nightly.", _at(0.0))
    rows = _search(db, "why does the gateway keep failing after the upgrade", _at(0.0))
    assert rows[0][0] == "The gateway on the mini restarts nightly." and rows[0][2] == 1


def test_a_superseded_or_retired_fact_is_never_returned(db, owner):
    _add(owner, "Old gateway user.", BASE, superseded=True)
    _add(owner, "Retired gateway user.", BASE, retired=True)
    assert [row[0] for row in _search(db, "gateway")] == ["Old gateway user. (newer)"]


def test_a_projects_search_includes_facts_with_no_project_and_not_other_projects(db, owner):
    # #142: mirrored built-in memory has no project and belongs to every project's search.
    _add(owner, "In p1.", BASE, project="p1")
    _add(owner, "In no project.", _at(0.9), project=None)
    _add(owner, "In p2.", _at(0.95), project="p2")
    assert sorted(row[0] for row in _search(db, "x", project="p1")) == ["In no project.", "In p1."]


def test_a_session_with_no_project_searches_every_project(db, owner):
    for name, project in (("In p1.", "p1"), ("In p2.", "p2"), ("In no project.", None)):
        _add(owner, name, BASE, project=project)
    assert sorted(row[0] for row in _search(db, "x", project=None)) == ["In no project.", "In p1.", "In p2."]


def test_the_pool_bounds_each_list(db, owner):
    for n in range(5):
        _add(owner, f"Fact {n}.", _at(0.9 - n / 10))
    assert len(_search(db, "x", pool=3)) == 3


def test_search_returns_the_wanted_number_with_provenance(db, owner, ollama):
    for n in range(8):
        _add(owner, f"The gateway fact {n}.", _at(0.99 - n / 100))
    ollama.vectors = {"gateway": BASE}
    with psycopg.connect(db["agent"]) as conn:
        results = search.search(conn, embedding.Embedder(ollama.url), "gateway", "p1", wanted=5, timeout=2)
    assert len(results) == 5
    first = results[0]
    assert first["body"] == "The gateway fact 0." and first["found_by"] == ["meaning", "words"]
    assert first["ranked_by"] == "fusion"
    assert {"origin", "basis", "kind", "stored", "last_verified"} <= set(first)


TOOL_SCENARIO = r"""
import json, sys
import plugins.memory
provider = plugins.memory.load_memory_provider("custom_build")
modules = {name.rsplit(".", 1)[-1]: m for name, m in sys.modules.items() if "custom_build" in name}
modules["embedding"].OLLAMA_URL = sys.argv[1]
provider.initialize(session_id="s1", hermes_home=sys.argv[3], platform="cli", agent_context="primary")
print(json.dumps(json.loads(provider.handle_tool_call("memory_search", {"query": sys.argv[2]}))))
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


def tool(home, db, ollama_url, query):
    env = {"HERMES_HOME": str(home), "HOME": str(home.parent), "PATH": "/usr/bin:/bin",
           "CUSTOM_BUILD_DATABASE_URL": db["agent"], "TYPESAFE_API_KEY": "test-key-not-a-real-one"}
    result = subprocess.run([sys.executable, "-c", TOOL_SCENARIO, ollama_url, query, str(home)], capture_output=True,
                            text=True, env=env, cwd=home.parent / "work", timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _codes(db):
    with psycopg.connect(db["owner"]) as conn:
        return [row[0] for row in conn.execute("select code from memory.events order by id")]


def test_the_tool_searches_the_sessions_project(home, db, owner, ollama):
    _add(owner, "The gateway runs as hermes.", BASE, project="github.com/Owner/Repo")
    _add(owner, "Another project's gateway.", BASE, project="github.com/Other/Repo")
    ollama.vectors = {"gateway": BASE}
    outcome = tool(home, db, ollama.url, "gateway")
    assert [r["body"] for r in outcome["results"]] == ["The gateway runs as hermes."]


def test_the_tool_reports_an_embedding_failure_never_an_empty_result(home, db, ollama_down):
    outcome = tool(home, db, ollama_down, "gateway")
    assert outcome == {"error": "embed.unavailable"}
    assert _codes(db)[-1] == "embed.unavailable"
