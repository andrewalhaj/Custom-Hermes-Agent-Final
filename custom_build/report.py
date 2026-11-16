"""The daily failure report, `hermes custom-build report` (OPERATIONS.md section 4, ARCHITECTURE.md 6.2).

Hermes cron runs it daily at 07:00 on the job host and delivers what it prints to `<operators-channel>`.
With failures in the last 24 hours, it writes them to a dated Markdown file in Hermes's document cache
and prints a one-line summary with a `MEDIA:` line, which makes Hermes send the file as an attachment.
With none, it prints a one-line all-clear naming the jobs that ran. Either way it records job.succeeded,
so the job-health check knows it ran; a day with no report at all is the alarm.
"""

import datetime
import re

from . import failures, jobs

DAY = "24 hours"   # the window the report covers, ending when it runs


def run(ctx):
    """Send the day's report; return the exit code, as every job does (`jobs.run`)."""
    return jobs.run(ctx, "report", compose)


def compose(conn, machine):
    """Print the report for Hermes to deliver, then record its success; return whether every row was written."""
    from agent.redact import redact_sensitive_text

    # The job-health check cannot report its own absence, so the report does (OPERATIONS.md section 4).
    checked = conn.execute("select 1 from memory.events where code = 'job.succeeded' and detail->>'job' = 'job-health'"
                           f" and at > now() - interval '{DAY}' limit 1").fetchone()
    if not checked and not failures.record(conn, "job.missed", "failed", source=machine, detail={"job": "job-health"}):
        return False   # the day's record is incomplete: print nothing, so Hermes alerts (events.write_failed)
    rows = conn.execute("select code, source, at, reason, detail, session_id from memory.events"
                        f" where result in ('failed', 'degraded') and at > now() - interval '{DAY}'"
                        " order by code, source, at desc").fetchall()
    day = datetime.datetime.now().astimezone().date().isoformat()
    if rows:
        path = _write(day, machine, rows, redact_sensitive_text)
        codes = len({row[0] for row in rows})
        print(redact_sensitive_text(f"Failure report for {day}: {len(rows)} failures under {codes} codes.", force=True))
        print(f"MEDIA:{path}")
    else:
        print(redact_sensitive_text(f"All clear for {day}: no failures in the last 24 hours. Ran: {_ran(conn)}.",
                                    force=True))
    return failures.record(conn, "job.succeeded", "ok", source=machine,
                           detail={"job": "daily-report", "input": len(rows), "output": 1})


def _ran(conn):
    """The jobs that ran in the day: each heartbeat's hosts, then every job that recorded job.succeeded."""
    hosts = [h for (h,) in conn.execute("select distinct source from memory.events where code = 'host.heartbeat'"
                                         f" and at > now() - interval '{DAY}' order by source")]
    names = [j for (j,) in conn.execute("select distinct detail->>'job' from memory.events where code = 'job.succeeded'"
                                         f" and at > now() - interval '{DAY}' order by 1")]
    found = ([f"heartbeat ({', '.join(hosts)})"] if hosts else []) + names
    return ", ".join(found) if found else "nothing"


def _write(day, machine, rows, redact):
    """Write the failures, grouped by code then machine, to failures-<day>.md; return its path."""
    from gateway.platforms.base import DOCUMENT_CACHE_DIR

    lines = [f"# Failure report, {day}", "",
             f"The last 24 hours of `events`, read on {machine}: {len(rows)} failures.", ""]
    for code in dict.fromkeys(row[0] for row in rows):
        shown = "yes" if code in failures.SHOWN_IN_TURN else "no"
        lines += [f"## {code}: severity {failures.SEVERITY.get(code, 'unknown')}, shown in the turn: {shown}", "",
                  "| Machine | Count | Latest | Latest reason | Names |", "|---|---|---|---|---|"]
        for source in dict.fromkeys(row[1] for row in rows if row[0] == code):
            group = [row for row in rows if row[0] == code and row[1] == source]
            names = sorted({str(value) for _, _, _, _, detail, session in group
                            for value in (detail.get("host"), detail.get("job"), session) if value})
            latest = group[0]
            lines.append(f"| {source} | {len(group)} | {latest[2]:%Y-%m-%d %H:%M %Z} | {_cell(latest[3])} | "
                         f"{_cell(', '.join(names))} |")
        lines.append("")
    path = DOCUMENT_CACHE_DIR / f"failures-{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact("\n".join(lines), force=True), encoding="utf-8")   # it leaves the host (rule L4)
    return path


def _cell(text):
    """A value as one Markdown table cell."""
    return re.sub(r"[\r\n]+", " ", text or "").replace("\\", "\\\\").replace("|", "\\|")
