"""List the store's events on the maintainer's own machine, and only read them (#255).

The maintainer runs this from a checkout's root, with a viewer login's connection string in
CUSTOM_BUILD_VIEWER_URL. It is never an argument, and never printed or put on the page. See OPERATIONS.md
section 4:

    python -m scripts.events_viewer                 # serves http://127.0.0.1:9120
    python -m scripts.events_viewer --port 9200

The login joins `memory_viewer` (migration 0009), which may select from memory.events and nothing else, so
the page cannot change the store whatever is asked of it. It serves on a loopback address only, has one
route, and has no sign-in: anything running on the same machine as the same user can read it. It runs
outside Hermes with the maintainer reading its output, so it records no failure code.
"""

import argparse
import contextlib
import html
import http.server
import ipaddress
import json
import os
import sys
import urllib.parse
import zoneinfo

import psycopg
from psycopg.rows import dict_row

from custom_build.failures import CATALOG, RESULTS, SEVERITY

URL = "CUSTOM_BUILD_VIEWER_URL"
PORT = 9120
LIMIT = 500   # rows on one page, newest first
WINDOWS = {1: "1 hour", 24: "24 hours", 168: "7 days", 720: "30 days", 2160: "90 days"}   # 90 days: retention's
EASTERN = zoneinfo.ZoneInfo("America/New_York")   # the maintainer's clock, whatever machine serves the page
CONNECT_TIMEOUT = 5   # seconds: nothing waits on this but the maintainer
GUARD_PREFIXES = ("write_gate.", "verify_gate.", "fact_gate.", "recall_gate.", "guard.")
GUARDS = sorted(code for code in CATALOG if code.startswith(GUARD_PREFIXES))
FAILURES = sorted(code for code in SEVERITY if code not in GUARDS)
GROUPS = {"guards": "Guards", "failures": "Failures", "other": "Everything else"}
# The page's one stylesheet. System fonts only: the page asks no other host for anything.
STYLE = """
:root {
  --bg: #eef1f5; --surface: #ffffff; --ink: #10141c; --muted: #566070; --line: #d3d9e2; --hover: #f4f6f9;
  --accent: #b93c0a; --on-ink: #ffffff;
  --failed: #c01f1f; --degraded: #94590a; --ok: #14733a; --empty: #7a8494;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --gutter: clamp(16px, 3vw, 40px);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b0e13; --surface: #12161d; --ink: #e7eaf0; --muted: #909baa; --line: #252c38; --hover: #181d26;
    --accent: #ff9a4a; --on-ink: #0b0e13;
    --failed: #ff7b72; --degraded: #e3a94a; --ok: #56c97f; --empty: #6f7a8a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.5 var(--sans); }
a { color: inherit; text-decoration: none; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.skip { position: absolute; left: var(--gutter); top: -48px; padding: 8px 12px; background: var(--ink); color: var(--on-ink); }
.skip:focus { top: 8px; }

header { display: flex; flex-wrap: wrap; gap: 16px 40px; align-items: end; justify-content: space-between;
  padding: 24px var(--gutter) 18px; border-bottom: 2px solid var(--ink); }
h1 { margin: 0 0 6px; font: 600 12px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
h1::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 10px; background: var(--accent); }
.count { margin: 0; color: var(--muted); }
.count b { margin-right: 10px; color: var(--ink); font: 600 44px/1 var(--mono); letter-spacing: -.04em;
  font-variant-numeric: tabular-nums; }

.seg { display: flex; flex-wrap: wrap; align-items: center; }
.seg > span { margin-right: 10px; color: var(--muted); font: 11px var(--mono); letter-spacing: .1em; text-transform: uppercase; }
.tab { display: inline-flex; align-items: center; gap: 8px; min-height: 32px; margin-left: -1px; padding: 0 12px;
  border: 1px solid var(--line); background: var(--surface); font: 13px var(--mono);
  transition: background-color .1s, color .1s, transform .1s; }
.tab:hover { background: var(--hover); box-shadow: inset 0 -2px var(--accent); }
.tab:active { transform: translateY(1px); }
.tab[aria-current] { position: relative; background: var(--ink); border-color: var(--ink); color: var(--on-ink); }
.n { font: 12px var(--mono); color: var(--muted); font-variant-numeric: tabular-nums; }
[aria-current] > .n { color: inherit; opacity: .7; }

.layout { display: grid; grid-template-columns: 296px minmax(0, 1fr); align-items: start; }
.rail { position: sticky; top: 0; max-height: 100vh; overflow: auto; padding: 20px 16px 20px var(--gutter);
  border-right: 1px solid var(--line); }
.rail ul { list-style: none; margin: 0; padding: 0; }
.rail > ul > li { margin-bottom: 20px; }
.rail a { position: relative; display: flex; justify-content: space-between; align-items: center; gap: 12px;
  min-height: 30px; padding: 0 8px; transition: background-color .1s; }
.rail a:hover { background: var(--surface); }
.rail a[aria-current] { background: var(--ink); color: var(--on-ink); }
.group { margin-bottom: 4px; border-bottom: 1px solid var(--ink); font: 600 11px var(--mono); letter-spacing: .12em;
  text-transform: uppercase; }
.code { font: 12.5px var(--mono); }
.code i { position: absolute; left: 8px; bottom: 2px; width: calc((100% - 16px) * var(--w)); height: 2px;
  background: var(--accent); opacity: .55; }

main { min-width: 0; padding: 20px var(--gutter) 0 24px; }
.bar { display: flex; flex-wrap: wrap; gap: 10px 28px; align-items: center; margin-bottom: 16px; }
.clear { margin-left: auto; color: var(--accent); font: 12px var(--mono); text-decoration: underline; text-underline-offset: 3px; }
.scroll { overflow-x: auto; background: var(--surface); border: 1px solid var(--line); }
table { width: 100%; border-collapse: collapse; }
thead th { padding: 8px 14px; border-bottom: 1px solid var(--line); color: var(--muted); font: 11px var(--mono);
  letter-spacing: .1em; text-transform: uppercase; text-align: left; white-space: nowrap; }
.day th { padding: 18px 14px 6px; border-bottom: 1px solid var(--ink); font: 600 12px var(--mono); text-align: left; }
td { padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:not(.day) { transition: background-color .1s; }
tbody tr:not(.day):hover { background: var(--hover); }
.at { width: 1%; border-left: 3px solid var(--empty); white-space: nowrap; font: 13px var(--mono); font-variant-numeric: tabular-nums; }
.result { width: 1%; white-space: nowrap; color: var(--empty); font: 600 12px var(--mono); }
.failed .at { border-left-color: var(--failed); } .failed .result { color: var(--failed); }
.degraded .at { border-left-color: var(--degraded); } .degraded .result { color: var(--degraded); }
.ok .at { border-left-color: var(--ok); } .ok .result { color: var(--ok); }
.source { white-space: nowrap; font: 13px var(--mono); color: var(--muted); }
code { font: 600 13px var(--mono); white-space: nowrap; }
.means { margin: 2px 0 0; max-width: 32ch; color: var(--muted); font-size: 12.5px; line-height: 1.4; }
.why { min-width: 20ch; overflow-wrap: anywhere; }
.context { display: flex; flex-wrap: wrap; gap: 2px 16px; margin-top: 4px; color: var(--muted); font: 12px/1.6 var(--mono); }
.why > .context:first-child { margin-top: 0; }
.context b { font-weight: 400; color: var(--ink); }
.context b::after { content: "="; color: var(--muted); }

.empty, .problem { padding: 48px 24px; background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--accent);
  color: var(--muted); }
.empty a, .problem a { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }
.problem { max-width: 560px; margin: 12vh auto; }
.problem h1 { color: var(--ink); }
footer { margin-top: 24px; padding: 14px var(--gutter) 28px; border-top: 1px solid var(--line); color: var(--muted);
  font: 12px var(--mono); }

@media (max-width: 860px) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .rail { position: static; max-height: none; padding: 16px var(--gutter) 0; border-right: 0; }
  .rail > ul { display: flex; flex-wrap: wrap; gap: 8px; }
  .rail > ul > li { margin: 0; }
  .rail ul ul { display: none; }   /* a phone gets the three groups; the codes need the width */
  .group { margin: 0; border: 1px solid var(--line); }
  main { padding: 16px var(--gutter) 0; }
  .tab, .rail a { min-height: 44px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


class ViewerError(Exception):
    """The page cannot be served as asked; nothing was started."""


class BadQuery(Exception):
    """The address asks for a filter the page does not have."""


def group_of(code):
    if code in GUARDS:
        return "guards"
    return "failures" if code in FAILURES else "other"


def _where(*, code=None, group=None, source=None, result=None, hours=24):
    clauses, values = ["at > now() - make_interval(hours => %s)"], [hours]
    if code:
        clauses.append("code = %s")
        values.append(code)
    if group == "guards":
        clauses.append("code = any(%s)")
        values.append(GUARDS)
    elif group == "failures":
        clauses.append("code = any(%s)")
        values.append(FAILURES)
    elif group == "other":
        clauses.append("code <> all(%s)")
        values.append(GUARDS + FAILURES)
    if source:
        clauses.append("source = %s")
        values.append(source)
    if result:
        clauses.append("result = %s")
        values.append(result)
    return " and ".join(clauses), values


def events(conn, **filters):
    """The window's rows that pass the filters, newest first, LIMIT at most."""
    where, values = _where(**filters)
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            "select id, at, source, project, session_id, turn, code, result, reason, detail from memory.events"
            f" where {where} order by at desc, id desc limit {LIMIT}", values).fetchall()


def counts(conn, hours):
    """How many rows each code and each machine has in the window, whatever the other filters are."""
    by_code = dict(conn.execute("select code, count(*) from memory.events"
                                " where at > now() - make_interval(hours => %s) group by code", (hours,)).fetchall())
    by_source = dict(conn.execute("select source, count(*) from memory.events"
                                  " where at > now() - make_interval(hours => %s) group by source", (hours,)).fetchall())
    return by_code, by_source


def filters_of(query):
    """The filters an address asks for; BadQuery for one the page does not have."""
    asked = {key: values[-1] for key, values in urllib.parse.parse_qs(query).items()}
    try:
        hours = int(asked.get("hours", 24))
    except ValueError:
        raise BadQuery("hours is not a number") from None
    if hours not in WINDOWS:
        raise BadQuery(f"hours is one of {', '.join(map(str, WINDOWS))}")
    if asked.get("group") not in (None, *GROUPS):
        raise BadQuery(f"group is one of {', '.join(GROUPS)}")
    if asked.get("result") not in (None, *RESULTS):
        raise BadQuery(f"result is one of {', '.join(RESULTS)}")
    return {"hours": hours, "code": asked.get("code"), "group": asked.get("group"),
            "source": asked.get("source"), "result": asked.get("result")}


def _href(filters, **changes):
    kept = {key: value for key, value in {**filters, **changes}.items() if value and (key, value) != ("hours", 24)}
    return "/?" + urllib.parse.urlencode(kept) if kept else "/"


def _chip(label, href, on, count=None):
    number = f'<span class="n">{count}</span>' if count is not None else ""
    current = ' aria-current="true"' if on else ""
    return f'<a class="tab" href="{html.escape(href)}"{current}>{html.escape(label)}{number}</a>'


def _rail(filters, by_code):
    parts, most = [], max(by_code.values(), default=1)
    for group, title in GROUPS.items():
        codes = sorted(code for code in by_code if group_of(code) == group)
        total = sum(by_code[code] for code in codes)
        on = filters["group"] == group and not filters["code"]
        current = ' aria-current="true"' if on else ""
        parts.append(f'<li><a class="group" href="{html.escape(_href(filters, group=group, code=None))}"{current}>'
                     f'{title}<span class="n">{total}</span></a><ul>')
        for code in codes:
            current = ' aria-current="true"' if filters["code"] == code else ""
            # The line under a code is its share of the busiest code's rows.
            parts.append(f'<li><a class="code" href="{html.escape(_href(filters, code=code, group=None))}"{current}'
                         f' title="{html.escape(CATALOG.get(code, ""))}">{html.escape(code)}'
                         f'<span class="n">{by_code[code]}</span>'
                         f'<i style="--w: {by_code[code] / most:.2f}"></i></a></li>')
        parts.append("</ul></li>")
    return "".join(parts)


def _context(row):
    """What a row carries besides its reason: where it happened, then its detail, one `key value` each."""
    pairs = [(key, row[key]) for key in ("project", "session_id", "turn") if row[key] is not None]
    pairs += sorted((row["detail"] or {}).items())
    return "".join(f"<span><b>{html.escape(str(key))}</b>{html.escape(value if isinstance(value, str) else json.dumps(value))}"
                   "</span>" for key, value in pairs)


def _rows(rows):
    """The table's rows, under one heading for each day."""
    parts, day = [], None
    for row in rows:
        at = row["at"].astimezone(EASTERN)
        if at.date() != day:
            day = at.date()
            parts.append(f'<tr class="day"><th colspan="5" scope="colgroup">{at:%A} {at.day} {at:%B %Y}</th></tr>')
        clock = f"{at:%I:%M:%S %p %Z}".lstrip("0")   # 3:05:09 PM EST
        parts.append(
            f'<tr class="{row["result"]}"><td class="at"><time datetime="{at.isoformat()}">{clock}</time></td>'
            f'<td class="result">{row["result"]}</td>'
            f'<td><code>{html.escape(row["code"])}</code>'
            f'<p class="means">{html.escape(CATALOG.get(row["code"], "Not in the catalog"))}</p></td>'
            f'<td class="source">{html.escape(row["source"])}</td>'
            f'<td class="why">{html.escape(row["reason"] or "")}<div class="context">{_context(row)}</div></td></tr>')
    return "".join(parts)


def page(rows, by_code, by_source, filters):
    """The whole page, as HTML. Every stored value is escaped."""
    window = WINDOWS[filters["hours"]]
    total = sum(by_code.values())
    windows = "".join(_chip(label, _href(filters, hours=hours), filters["hours"] == hours)
                      for hours, label in WINDOWS.items())
    sources = _chip("all", _href(filters, source=None), not filters["source"]) + "".join(
        _chip(source, _href(filters, source=source), filters["source"] == source, count)
        for source, count in sorted(by_source.items()))
    results = _chip("any", _href(filters, result=None), not filters["result"]) + "".join(
        _chip(result, _href(filters, result=result), filters["result"] == result) for result in RESULTS)
    cut = f" Showing the newest {LIMIT}." if len(rows) == LIMIT else ""
    shown = f"<b>{len(rows)}</b>of {total} events in the last {window}.{cut}"
    if rows:
        table = ('<div class="scroll"><table><thead><tr><th>Time</th><th>Result</th><th>Code</th><th>Machine</th>'
                 "<th>Reason and detail</th></tr></thead><tbody>" + _rows(rows) + "</tbody></table></div>")
    else:
        wider = next((hours for hours in WINDOWS if hours > filters["hours"]), None)
        table = '<p class="empty">Nothing was recorded for these filters.' + (
            f' <a href="{html.escape(_href(filters, hours=wider))}">Look back {WINDOWS[wider]}</a>' if wider else "") + "</p>"
    clear = "" if _href(filters) == "/" else '<a class="clear" href="/">Clear filters</a>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Events</title><style>{STYLE}</style></head>
<body><a class="skip" href="#events">Skip to the events</a>
<header><div><h1>Events</h1><p class="count">{shown}</p></div>
<nav aria-label="Time window" class="seg"><span>Window</span>{windows}</nav></header>
<div class="layout">
<nav aria-label="Codes" class="rail"><ul>{_rail(filters, by_code)}</ul></nav>
<main id="events"><div class="bar"><div class="seg" role="group" aria-label="Machine"><span>Machine</span>{sources}</div>
<div class="seg" role="group" aria-label="Result"><span>Result</span>{results}</div>{clear}</div>
{table}</main></div>
<footer>Read-only. Times are Eastern. Reload the page for newer rows.</footer></body></html>"""


def _problem(title, text):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<style>{STYLE}</style></head><body><main class="problem"><h1>{html.escape(title)}</h1>'
            f'<p>{html.escape(text)}</p><p><a href="/">Back to the events</a></p></main></body></html>')


def server(host, port, url):
    """An HTTP server for the page on a loopback address; ViewerError for any other address."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ViewerError(f"{host!r} is not a loopback address: the page has no sign-in, so it serves on "
                          "127.0.0.1 only")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            address = urllib.parse.urlsplit(self.path)
            if address.path != "/":
                return self._send(404, _problem("Not found", "The page has one address, /."))
            try:
                filters = filters_of(address.query)
            except BadQuery as bad:
                return self._send(400, _problem("Unknown filter", str(bad)))
            try:
                with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
                    body = page(events(conn, **filters), *counts(conn, filters["hours"]), filters)
            except psycopg.Error as failed:
                # The class only: a driver's message can carry the host and the login's name.
                return self._send(502, _problem("The store did not answer", type(failed).__name__))
            return self._send(200, body)

        def _send(self, status, body):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return http.server.ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    url = os.environ.get(URL)
    if not url:
        print(f"{URL} is not set: it holds the viewer login's connection string", file=sys.stderr)
        raise SystemExit(2)
    try:
        serving = server(args.host, args.port, url)
    except ViewerError as refused:
        print(refused, file=sys.stderr)
        raise SystemExit(2) from None
    print(f"Events: http://{args.host}:{serving.server_address[1]}  (Ctrl-C stops it)")
    with serving, contextlib.suppress(KeyboardInterrupt):
        serving.serve_forever()


if __name__ == "__main__":
    main()
