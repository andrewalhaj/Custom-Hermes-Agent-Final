"""Prove the events page (#255): `python -m scripts.events_viewer` lists memory.events on localhost, and only reads.

Its login joins `memory_viewer` (migration 0009), which may select from memory.events and nothing else. The
`db` fixture gives a throwaway store with such a login.
"""

import threading
import urllib.error
import urllib.request

import psycopg
import pytest

from scripts import events_viewer


def _add(db, code, *, source="host-a", result="failed", reason=None, ago="1 minute", detail="{}"):
    with psycopg.connect(db["owner"]) as owner:
        owner.execute(
            "insert into memory.events (at, source, code, result, reason, detail)"
            " values (now() - %s::interval, %s, %s, %s, %s, %s::jsonb)", (ago, source, code, result, reason, detail))


@pytest.fixture
def page(db):
    """The page, served on a free loopback port with the viewer's login; `get(path)` returns status and body."""
    server = events_viewer.server("127.0.0.1", 0, db["viewer"])
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(path="/", method="GET"):
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, answer.read().decode()
        except urllib.error.HTTPError as refused:
            return refused.code, refused.read().decode()

    yield get
    server.shutdown()
    server.server_close()


def test_the_viewer_login_reads_events(db):
    _add(db, "job.failed")
    with psycopg.connect(db["viewer"]) as conn:
        assert conn.execute("select code from memory.events").fetchall() == [("job.failed",)]


@pytest.mark.parametrize("statement", [
    "insert into memory.events (source, code, result) values ('host-a', 'job.failed', 'failed')",
    "update memory.events set reason = 'changed'",
    "delete from memory.events",
    "truncate memory.events",
    "select count(*) from memory.facts",
])
def test_the_viewer_login_does_nothing_else(db, statement):
    with psycopg.connect(db["viewer"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_events_come_newest_first(db):
    _add(db, "job.failed", ago="3 hours")
    _add(db, "db.unreachable", ago="1 hour")
    with psycopg.connect(db["viewer"]) as conn:
        assert [row["code"] for row in events_viewer.events(conn)] == ["db.unreachable", "job.failed"]


def test_events_are_filtered_by_code_machine_result_and_window(db):
    _add(db, "job.failed", source="host-a", result="failed")
    _add(db, "job.failed", source="host-b", result="failed")
    _add(db, "job.succeeded", source="host-a", result="ok")
    _add(db, "job.failed", source="host-a", result="failed", ago="3 days")
    with psycopg.connect(db["viewer"]) as conn:
        assert len(events_viewer.events(conn, hours=24)) == 3
        assert len(events_viewer.events(conn, hours=168)) == 4
        assert len(events_viewer.events(conn, code="job.failed")) == 2
        assert len(events_viewer.events(conn, source="host-b")) == 1
        assert [row["code"] for row in events_viewer.events(conn, result="ok")] == ["job.succeeded"]


def test_the_guards_are_a_group_of_their_own(db):
    for code in ("write_gate.would_ask", "verify_gate.would_block", "fact_gate.would_withhold",
                 "recall_gate.armed", "guard.bypassed"):
        _add(db, code, result="ok")
    _add(db, "job.failed")
    with psycopg.connect(db["viewer"]) as conn:
        assert sorted(row["code"] for row in events_viewer.events(conn, group="guards")) == [
            "fact_gate.would_withhold", "guard.bypassed", "recall_gate.armed", "verify_gate.would_block",
            "write_gate.would_ask"]
        assert [row["code"] for row in events_viewer.events(conn, group="failures")] == ["job.failed"]


def test_the_page_lists_a_row_with_what_its_code_means(db, page):
    _add(db, "write_gate.would_ask", result="ok", detail='{"rule": "skills"}')
    status, body = page("/?hours=24")
    assert status == 200
    assert "write_gate.would_ask" in body
    assert "host-a" in body
    assert "skills" in body
    assert events_viewer.CATALOG["write_gate.would_ask"] in body


def test_the_page_filters_by_its_query(db, page):
    _add(db, "job.failed", reason="the report job crashed")
    _add(db, "db.unreachable", reason="the store did not answer")
    _, body = page("/?code=db.unreachable")
    assert "the store did not answer" in body
    assert "the report job crashed" not in body


def test_stored_text_is_escaped(db, page):
    _add(db, "job.failed", reason="<script>alert(1)</script>")
    _, body = page()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


@pytest.mark.parametrize("query", ["?hours=abc", "?hours=-5", "?hours=99999999", "?group=nonsense", "?result=nonsense"])
def test_a_query_it_does_not_know_is_refused(page, query):
    status, _ = page("/" + query)
    assert status == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_it_has_no_route_that_writes(db, page, method):
    status, _ = page("/", method=method)
    assert status in (405, 501)
    with psycopg.connect(db["owner"]) as owner:
        assert owner.execute("select count(*) from memory.events").fetchone() == (0,)


def test_any_other_path_is_not_found(page):
    assert page("/api/env")[0] == 404


def test_the_connection_string_is_never_on_the_page(db, page):
    password = psycopg.conninfo.conninfo_to_dict(db["viewer"])["password"]
    _add(db, "job.failed")
    assert password not in page()[1]


def test_a_store_that_does_not_answer_shows_the_error_and_not_the_connection_string():
    url = "postgresql://viewer:not-a-real-password@127.0.0.1:1/nowhere?connect_timeout=2"
    server = events_viewer.server("127.0.0.1", 0, url)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=10)
        body = refused.value.read().decode()
    finally:
        server.shutdown()
        server.server_close()
    assert refused.value.code == 502
    assert "OperationalError" in body
    assert "not-a-real-password" not in body


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_it_serves_on_a_loopback_address_only(host):
    with pytest.raises(events_viewer.ViewerError, match="loopback"):
        events_viewer.server(host, 0, "postgresql://unused")


def test_without_its_connection_string_it_stops_and_names_the_variable(monkeypatch, capsys):
    monkeypatch.delenv(events_viewer.URL, raising=False)
    with pytest.raises(SystemExit) as stop:
        events_viewer.main([])
    assert stop.value.code == 2
    assert events_viewer.URL in capsys.readouterr().err
