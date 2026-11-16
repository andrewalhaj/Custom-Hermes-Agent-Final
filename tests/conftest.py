"""The throwaway database the store tests run against (issue #33).

`db` gives each test an empty database with every migration applied in order, and two login roles
that join the migration's group roles, the way a maintainer adds a machine. The tests need a
Postgres 17 server named by TEST_DATABASE_URL, a superuser connection; they fail, never skip,
without it. The group roles are server-wide, and each test drops them when it ends, so the server
must be a throwaway one used by one test run at a time. A role left over from an interrupted run
makes the migration fail loudly instead of being dropped silently.
"""

import http.server
import json
import os
import pathlib
import threading
import uuid

import psycopg
import pytest
from psycopg import sql

MIGRATIONS = sorted((pathlib.Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))
GROUP_ROLES = ("memory_agent", "memory_maintenance")


def _drop_roles(admin, names):
    for name in names:
        admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(name)))


@pytest.fixture
def db():
    """Connection strings for the database's owner, an agent login and a maintenance login."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set: these tests need a Postgres 17 superuser connection")
    if not MIGRATIONS:
        pytest.fail("no migration files found under migrations/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    agent, maint = f"{name}_agent", f"{name}_maint"
    password = uuid.uuid4().hex   # a throwaway login's password, so servers that require one (CI's) accept it
    info = psycopg.conninfo.conninfo_to_dict(url)

    def dsn(**kw):
        return psycopg.conninfo.make_conninfo(**{**info, "dbname": name, **kw})

    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(dsn(), autocommit=True) as owner:
                for migration in MIGRATIONS:
                    owner.execute(migration.read_text(encoding="utf-8"))
                for login, group in ((agent, "memory_agent"), (maint, "memory_maintenance")):
                    owner.execute(sql.SQL("create role {} login password {} in role {}").format(
                        sql.Identifier(login), sql.Literal(password), sql.Identifier(group)))
            yield {"owner": dsn(), "agent": dsn(user=agent, password=password),
                   "maint": dsn(user=maint, password=password)}
        finally:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
            _drop_roles(admin, (agent, maint, *GROUP_ROLES))


class FakeOllama(http.server.ThreadingHTTPServer):
    """A stand-in for the two Ollama endpoints the build calls, `/api/tags` and `/api/embed`, so the
    tests need no model. A test sets the models it lists, the length of the vectors it returns and an
    HTTP status to answer with instead; `embedded` collects every input sent to `/api/embed`."""

    digest = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
    build = f"qwen3-embedding:4b@sha256:{digest}"

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _OllamaHandler)
        self.models = [{"name": "qwen3-embedding:4b", "model": "qwen3-embedding:4b", "digest": self.digest}]
        self.dimensions = 2560
        self.status = 200
        self.embedded = []
        self.url = f"http://127.0.0.1:{self.server_address[1]}"


class _OllamaHandler(http.server.BaseHTTPRequestHandler):
    def _answer(self, body):
        payload = json.dumps(body if self.server.status == 200 else {"error": "refused"}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._answer({"models": self.server.models})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.embedded.append(request["input"])
        self._answer({"embeddings": [[0.01] * self.server.dimensions for _ in request["input"]]})

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    """A FakeOllama serving on a free local port for the length of the test."""
    server = FakeOllama()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def ollama_down():
    """The URL of a local port nothing listens on: an Ollama that cannot be reached."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    server.server_close()
    return f"http://127.0.0.1:{server.server_address[1]}"
