"""Prove the events page (#255): `python -m scripts.events_viewer` lists memory.events on localhost, and only reads.

Its login joins `memory_viewer` (migration 0009), which may select from memory.events and nothing else. The
`db` fixture gives a throwaway store with such a login.
"""

import datetime
import re
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

    def get(path="/", method="GET", host=None):
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", method=method,
                                         headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, answer.read().decode()
        except urllib.error.HTTPError as refused:
            return refused.code, refused.read().decode()

    get.port = server.server_address[1]
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
    "select level from memory.schema_level",
    "create table memory.mine (id integer)",
])
def test_the_viewer_login_does_nothing_else(db, statement):
    with psycopg.connect(db["viewer"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_the_viewer_roles_grants_are_exactly_these(db):
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute(
            "select table_schema, table_name, privilege_type from information_schema.role_table_grants"
            " where grantee = 'memory_viewer'").fetchall() == [("memory", "events", "SELECT")]


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
    # The group is the four gates' rows. A guard that crashed or was bypassed is a failure, and listed with them.
    for code in ("write_gate.would_ask", "verify_gate.would_block", "fact_gate.would_withhold", "recall_gate.armed"):
        _add(db, code, result="ok")
    _add(db, "job.failed")
    _add(db, "guard.crash")
    _add(db, "guard.bypassed")
    with psycopg.connect(db["viewer"]) as conn:
        assert sorted(row["code"] for row in events_viewer.events(conn, group="guards")) == [
            "fact_gate.would_withhold", "recall_gate.armed", "verify_gate.would_block", "write_gate.would_ask"]
        assert sorted(row["code"] for row in events_viewer.events(conn, group="failures")) == [
            "guard.bypassed", "guard.crash", "job.failed"]


def test_one_page_lists_the_newest_rows_up_to_its_limit(db, monkeypatch):
    monkeypatch.setattr(events_viewer, "LIMIT", 2)
    for minutes in (1, 2, 3):
        _add(db, "job.failed", reason=f"{minutes} ago", ago=f"{minutes} minutes")
    with psycopg.connect(db["viewer"]) as conn:
        assert [row["reason"] for row in events_viewer.events(conn)] == ["1 ago", "2 ago"]


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


def test_every_stored_field_and_the_query_are_escaped_wherever_they_land(db, page):
    # A code and a machine also land in an address and a title; a detail's keys and values in the row.
    mark = '"><script>x</script>'
    _add(db, mark, source=mark, reason=mark, detail='{"\\"><script>k</script>": "\\"><script>v</script>"}')
    for path in ("/", "/?code=%22%3E%3Cscript%3Ey%3C%2Fscript%3E&source=%22%3E%3Cscript%3Ez%3C%2Fscript%3E"):
        status, body = page(path)
        assert status == 200
        assert "<script" not in body


def test_a_detail_that_is_not_an_object_is_still_listed(db, page):
    _add(db, "job.failed", detail='["a", 1]')
    status, body = page()
    assert status == 200
    assert "[&quot;a&quot;, 1]" in body


@pytest.mark.parametrize("query", ["?hours=abc", "?hours=-5", "?hours=99999999", "?group=nonsense", "?result=nonsense",
                                   "?code=%00", "?source=%00"])
def test_a_query_it_does_not_know_is_refused(page, query):
    status, _ = page("/" + query)
    assert status == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_it_has_no_route_that_writes(db, page, method):
    _add(db, "job.failed", reason="as written")
    status, _ = page("/", method=method)
    assert status in (405, 501)
    with psycopg.connect(db["owner"]) as owner:
        assert owner.execute("select reason from memory.events").fetchall() == [("as written",)]


@pytest.mark.parametrize("host", ["evil.example", "evil.example:9120", "127.0.0.1.evil.example:9120", "10.0.0.5:9120"])
def test_a_request_under_another_hosts_name_is_refused(db, page, host):
    # A page in the maintainer's browser whose name resolves to 127.0.0.1 would otherwise read the events (DNS
    # rebinding): the browser sends that name as Host.
    _add(db, "job.failed", reason="for the maintainer only")
    status, body = page("/", host=host)
    assert status == 400
    assert "for the maintainer only" not in body


def test_a_request_to_localhost_by_name_is_served(page):
    assert page("/", host=f"localhost:{page.port}")[0] == 200


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


def test_times_are_eastern_on_a_12_hour_clock():
    # 20:05:09 UTC in November is 3:05:09 PM EST; 03:30 UTC is still the evening before in New York.
    def row(at):
        return {"id": 1, "at": datetime.datetime.fromisoformat(at), "source": "host-a", "project": None,
                "session_id": None, "turn": None, "code": "job.failed", "result": "failed", "reason": None, "detail": {}}

    filters = events_viewer.filters_of("")
    body = events_viewer.page([row("2026-11-17T20:05:09+00:00"), row("2026-11-17T03:30:00+00:00")], {}, {}, {}, filters)
    assert "3:05:09 PM EST" in body
    assert "10:30:00 PM EST" in body
    assert "Monday, November 16, 2026" in body
    assert "20:05:09" not in body


# The page behaves as a filtered list usually does (#255): nothing about it should surprise its reader.

def test_each_filters_counts_follow_the_other_filters(db):
    # A count beside a filter is how many rows choosing it would list, so it obeys every filter but its own.
    _add(db, "job.failed", source="host-a")
    _add(db, "job.failed", source="host-a")
    _add(db, "job.failed", source="host-b")
    _add(db, "job.succeeded", source="host-a", result="ok")
    with psycopg.connect(db["viewer"]) as conn:
        by_code, by_source, by_result = events_viewer.counts(conn, events_viewer.filters_of("source=host-b"))
        assert by_code == {"job.failed": 1}
        assert by_source == {"host-a": 3, "host-b": 1}
        assert by_result == {"failed": 1}
        by_code, by_source, by_result = events_viewer.counts(conn, events_viewer.filters_of("code=job.failed"))
        assert by_code == {"job.failed": 3, "job.succeeded": 1}
        assert by_source == {"host-a": 2, "host-b": 1}
        assert by_result == {"failed": 3}


def test_clearing_the_filters_keeps_the_time_window(db, page):
    _add(db, "job.failed")
    _, body = page("/?hours=168&code=job.failed")
    assert '<a class="clear" href="/?hours=168">' in body
    assert '<a class="clear"' not in page("/?hours=168")[1]


def test_a_chosen_code_with_no_rows_is_still_listed_as_chosen(page):
    _, body = page("/?code=db.auth")
    assert re.search(r'<a class="code"[^>]*aria-current="true"[^>]*>db\.auth', body)


def test_a_rows_code_and_machine_filter_by_themselves(db, page):
    _add(db, "job.failed", source="host-a")
    _, body = page()
    row = body[body.index("<tbody>"):]
    assert 'href="/?code=job.failed"' in row
    assert 'href="/?source=host-a"' in row


def test_the_page_says_when_it_was_read(page):
    # It does not refresh itself, so it says how old it is.
    assert re.search(r"As of \d{1,2}:\d\d:\d\d [AP]M E[SD]T", page()[1])
