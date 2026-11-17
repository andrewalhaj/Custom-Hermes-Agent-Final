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
import time
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
    # A machine's login is named as the machine (OPERATIONS.md section 3), and may write events only under
    # that name (#149). The tests' machine is host-a.
    agent, maint = "host-a", f"{name}_maint"
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
    tests need no model. A test sets the models it lists, the length of the vectors it returns, a vector
    for a given text (`vectors`; any other text gets `other`, else a constant one), how many vectors to
    leave out of an answer (`drop`), an HTTP status to answer with instead, and whether to cut the answer
    off mid-body (`cut_off`); `embedded` collects every input sent to `/api/embed`."""

    digest = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
    build = f"qwen3-embedding:4b@sha256:{digest}"

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _OllamaHandler)
        self.models = [{"name": "qwen3-embedding:4b", "model": "qwen3-embedding:4b", "digest": self.digest}]
        self.dimensions = 2560
        self.vectors = {}
        self.other = None
        self.drop = 0
        self.cut_off = False
        self.replaced_during_embed = None   # a digest the model takes on while it embeds
        self.delay = 0                      # seconds before each embedding answer, for deadlines
        self.status = 200
        self.embedded = []
        self.url = f"http://127.0.0.1:{self.server_address[1]}"


class _OllamaHandler(http.server.BaseHTTPRequestHandler):
    def _answer(self, body):
        payload = json.dumps(body if self.server.status == 200 else {"error": "refused"}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload) + (10 if self.server.cut_off else 0)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._answer({"models": self.server.models})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.embedded.append(request["input"])
        time.sleep(self.server.delay)
        if self.server.replaced_during_embed:   # `ollama pull` of a new build while a request runs
            self.server.models = [{**model, "digest": self.server.replaced_during_embed} for model in self.server.models]
        vectors = [self.server.vectors.get(text, self.server.other or [0.01] * self.server.dimensions)
                   for text in request["input"]]
        self._answer({"embeddings": vectors[:len(vectors) - self.server.drop]})

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


class FakeTypeSafe(http.server.ThreadingHTTPServer):
    """A stand-in for TypeSafe's System One endpoint, `POST /v1/systemone`. A test sets the probability
    each named question gets, an HTTP status to answer with instead, a delay longer than the caller's
    deadline, a raw body to send instead of JSON (`raw`), an answer cut off mid-body (`cut_off`), or a
    connection closed before any answer (`hang_up`); `requests` collects each request's headers and body."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _TypeSafeHandler)
        self.answers = {}
        self.status = 200
        self.delay = 0
        self.raw = None
        self.cut_off = False
        self.hang_up = False
        self.refuse_new = None   # answer 422 when the new fact contains this text
        self.default = None      # the probability for a question `answers` does not name, such as a rerank's c0, c1...
        self.choices = {}        # the option a choice question picks, by question name; else its first option
        self.requests = []
        self.url = f"http://127.0.0.1:{self.server_address[1]}"


class _TypeSafeHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        time.sleep(self.server.delay)
        if self.server.hang_up:
            self.close_connection = True
            return
        refused = self.server.refuse_new and self.server.refuse_new in body["state"].get("new", "")
        if self.server.status == 200 and not refused:
            answers = {name: {"type": "noul", "noul": self.server.answers[name] if self.server.default is None
                              else self.server.answers.get(name, self.server.default)}
                       for name, question in body["questions"].items() if question["type"] == "noul"}
            for name, question in body["questions"].items():
                if question["type"] == "choice":   # the pick at 0.9, the rest sharing what is left
                    options = list(question["criteria"])
                    pick = self.server.choices.get(name, options[0])
                    answers[name] = {"type": "choice", "choice": pick, "probabilities": {
                        option: 0.9 if option == pick else round(0.1 / (len(options) - 1), 4) for option in options}}
            payload = {"model": body["model"], "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}
        else:
            payload = {"detail": "refused"}
        data = self.server.raw if self.server.raw is not None else json.dumps(payload).encode()
        self.send_response(422 if refused else self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data) + (10 if self.server.cut_off else 0)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def typesafe():
    """A FakeTypeSafe serving on a free local port for the length of the test."""
    server = FakeTypeSafe()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


class FakeDeepInfra(http.server.ThreadingHTTPServer):
    """A stand-in for DeepInfra's OpenAI-compatible embeddings endpoint (#239). `requests` collects each
    (Authorization header, body); `status` answers with an HTTP error instead, and `fail_on`, a text, answers
    500 to a request that holds it; `vectors` gives a text its own vector, any other gets `other`; `reverse`
    lists the vectors in reverse, each still carrying its index."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _DeepInfraHandler)
        self.requests, self.status, self.vectors, self.other, self.reverse = [], 200, {}, [0.01] * 2560, False
        self.fail_on = None
        self.url = f"http://127.0.0.1:{self.server_address[1]}/v1/openai/embeddings"


class _DeepInfraHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.headers.get("Authorization"), body))
        data = [{"object": "embedding", "index": i, "embedding": self.server.vectors.get(text, self.server.other)}
                for i, text in enumerate(body["input"])]
        if self.server.reverse:
            data.reverse()
        status = 500 if self.server.fail_on in body["input"] else self.server.status
        answer = {"object": "list", "data": data, "model": body["model"]} if status == 200 else {"detail": "refused"}
        payload = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def deepinfra():
    """A FakeDeepInfra serving on a free local port for the length of the test."""
    server = FakeDeepInfra()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()
