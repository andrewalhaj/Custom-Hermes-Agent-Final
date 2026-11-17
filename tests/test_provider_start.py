"""Prove the provider's check of its embedding build when it starts (issue #111, ARCHITECTURE.md 2 and 3.2).

Each case copies the plugin into a throwaway Hermes home and, in a process of its own, loads it
through Hermes v0.23.1's memory-provider loader and calls `initialize()` as Hermes does when an agent
starts. Ollama is the `ollama` fixture (conftest.py), reached over HTTP from that process.
"""

import json
import pathlib
import shutil
import subprocess
import sys

import psycopg
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"

SCENARIO = r"""
import json, logging, sys
import plugins.memory

logging.basicConfig(level=logging.INFO, stream=sys.stderr)   # the test reads the log lines from stderr
provider = plugins.memory.load_memory_provider("custom_build")
# The provider load imports the plugin as _hermes_user_memory.custom_build__source_<hash>.
loaded = [m for name, m in sys.modules.items() if "custom_build" in name and name.endswith(".memory.embedding")]
assert len(loaded) == 1, sorted(n for n in sys.modules if "custom_build" in n)
loaded[0].OLLAMA_URL = sys.argv[1]
if len(sys.argv) > 4:   # the hosted route's fake and its recorded reference (#239)
    import pathlib
    loaded[0].HOSTED_URL, loaded[0].REFERENCE = sys.argv[4], pathlib.Path(sys.argv[5])
if sys.argv[2] == "crash":
    def crash(self, conn, *, timeout):
        raise KeyError("boom")
    loaded[0].Embedder.check = crash
try:
    provider.initialize(session_id="s1", hermes_home=sys.argv[3], platform="cli")
    raised = None
except KeyError as exc:
    raised = type(exc).__name__
print(json.dumps({"raised": raised, "build": provider.embedder.build}))
"""


def start(tmp_path, db, ollama_url, *, database_url=True, mode="plain", proxy=None, hosted=None):
    """Load the provider and run initialize(); return (what it raised, its log output, events rows).

    What it raised is a dict with `raised` and the embedder's checked `build`. `hosted`, (the fake's URL, the
    reference file), sets `embedding: deepinfra` and its key (#239)."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    settings = {"machine_name": "host-a", "judge_model": "jev-1.14.0", **({"embedding": "deepinfra"} if hosted else {})}
    config = {"memory": {"provider": "custom_build"}, "plugins": {"entries": {"custom_build": {"settings": settings}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    env = {"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "TYPESAFE_API_KEY": "test-key-not-a-real-one"}
    if hosted:
        env["DEEPINFRA_API_KEY"] = "test-key-not-a-real-one"
    if database_url:
        env["CUSTOM_BUILD_DATABASE_URL"] = db["agent"] if database_url is True else database_url
    if proxy:
        env["http_proxy"] = env["HTTP_PROXY"] = proxy
    result = subprocess.run([sys.executable, "-c", SCENARIO, ollama_url, mode, str(home),
                             *([hosted[0], str(hosted[1])] if hosted else [])], capture_output=True,
                            text=True, env=env, cwd=tmp_path, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, source, session_id from memory.events order by id").fetchall()
    return report, result.stderr, rows


def _codes(output):
    return [json.loads(line[line.index("{"):])["code"] for line in output.splitlines() if '{"code"' in line]


def _store_fact(dsn, build):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "insert into memory.facts (body, body_hash, kind, origin, basis, source, embedding, embedding_model)"
            " values ('A stored fact.', 'h', 'fact', 'explicit', 'user_stated', 'host-b', %s, %s)",
            ("[" + ",".join(["0.01"] * 2560) + "]", build),
        )


def test_a_matching_build_records_nothing(tmp_path, db, ollama):
    _store_fact(db["owner"], ollama.build)
    report, output, rows = start(tmp_path, db, ollama.url)
    assert report == {"raised": None, "build": ollama.build}, output
    assert rows == [], output


def test_the_hosted_setting_checks_the_hosted_build_when_the_provider_starts(tmp_path, db, ollama_down, deepinfra):
    # #239: with `embedding: deepinfra` the provider needs no Ollama (none listens here) and checks the
    # hosted build by its reference vector.
    reference = tmp_path / "embed_reference.json"
    reference.write_text(json.dumps({"text": "The reference sentence.", "vector": deepinfra.other,
                                     "threshold": 0.999}), encoding="utf-8")
    report, output, rows = start(tmp_path, db, ollama_down, hosted=(deepinfra.url, reference))
    assert report == {"raised": None, "build": "deepinfra:Qwen/Qwen3-Embedding-4B"}, output
    assert rows == [], output
    assert [body["input"] for _, body in deepinfra.requests] == [["The reference sentence."]]


def test_a_refused_hosted_key_at_start_is_recorded_as_embed_refused(tmp_path, db, ollama_down, deepinfra):
    deepinfra.status = 401
    reference = tmp_path / "embed_reference.json"
    reference.write_text(json.dumps({"text": "x", "vector": deepinfra.other, "threshold": 0.999}), encoding="utf-8")
    report, output, rows = start(tmp_path, db, ollama_down, hosted=(deepinfra.url, reference))
    assert report["build"] is None
    assert rows == [("embed.refused", "failed", "host-a", "s1")], output


def test_a_store_of_another_build_records_a_model_mismatch(tmp_path, db, ollama):
    _store_fact(db["owner"], "qwen3-embedding:4b@sha256:another")
    report, output, rows = start(tmp_path, db, ollama.url)
    assert report["raised"] is None
    assert rows == [("embed.model_mismatch", "failed", "host-a", "s1")], output


def test_an_ollama_that_cannot_be_reached_is_recorded_as_unavailable(tmp_path, db, ollama_down):
    report, output, rows = start(tmp_path, db, ollama_down)
    assert report["raised"] is None
    assert rows == [("embed.unavailable", "failed", "host-a", "s1")], output


def test_without_the_connection_string_it_logs_config_missing(tmp_path, db, ollama):
    report, output, rows = start(tmp_path, db, ollama.url, database_url=False)
    assert report["raised"] is None
    assert _codes(output) == ["config.missing", "events.write_failed"]
    assert rows == []


def test_an_unreachable_store_is_logged_as_db_unreachable(tmp_path, db, ollama):
    report, output, rows = start(tmp_path, db, ollama.url,
                                 database_url="postgresql://u:p@localhost:1/x?connect_timeout=3")
    assert report["raised"] is None
    assert _codes(output) == ["db.unreachable", "events.write_failed"]
    assert rows == []


def test_a_crash_is_recorded_as_unhandled_and_re_raised(tmp_path, db, ollama):
    report, output, rows = start(tmp_path, db, ollama.url, mode="crash")
    assert report["raised"] == "KeyError"
    assert rows == [("unhandled", "failed", "host-a", "s1")], output


def test_a_refused_login_is_logged_as_db_auth(tmp_path, db, ollama):
    wrong = psycopg.conninfo.make_conninfo(db["agent"], password="not-the-password")
    report, output, rows = start(tmp_path, db, ollama.url, database_url=wrong)
    assert report["raised"] is None
    assert _codes(output) == ["db.auth", "events.write_failed"]
    assert rows == []


def test_ollama_is_reached_directly_even_with_a_proxy_in_the_environment(tmp_path, db, ollama, ollama_down):
    # Ollama is on this machine: its requests, with the text they embed, never go through a proxy.
    report, output, rows = start(tmp_path, db, ollama.url, proxy=ollama_down)
    assert report == {"raised": None, "build": ollama.build}, output
    assert rows == []
