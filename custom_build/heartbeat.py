"""The heartbeat, `hermes custom-build heartbeat` (ARCHITECTURE.md section 6.3, OPERATIONS.md section 4).

Hermes cron runs it every 6 hours on every agent host, through its wrapper in the Hermes home's
`scripts/` folder. Each run records one host.heartbeat event; the job-health check reports a host with
none in the last 24 hours as host.missing.
"""

import sys

from . import failures, settings


def run(ctx):
    """Record one host.heartbeat event; return the exit code, 0 or 1.

    It prints nothing when it succeeds, so Hermes cron delivers nothing. Every failure exits non-zero, so
    Hermes cron delivers its error alert, and prints one line to stderr that the alert carries, naming
    the failure code and where its details are (#64):
    - a missing setting is config.missing, logged with events.write_failed, as in the startup check;
    - a connection that fails to open is db.auth or db.unreachable, logged by the job wrapper;
    - a row the store does not take is events.write_failed;
    - a crash is unhandled and job.failed, recorded by the job wrapper.
    """
    machine = settings.machine(ctx)
    if missing := settings.missing_settings(ctx):
        names = ", ".join(missing)
        failures.record(None, "config.missing", "failed", source=machine, reason=names)
        print(f"custom-build heartbeat failed: config.missing ({names}); not recorded in events: "
              "events.write_failed (no connection to the store)", file=sys.stderr)
        return 1
    try:
        written = failures.run_job("heartbeat", lambda conn: failures.record(conn, "host.heartbeat", "ok", source=machine),
                                   connect=settings.connect, source=machine)
    except SystemExit as stop:
        print(f"custom-build heartbeat failed: {failures.exit_note(stop)}", file=sys.stderr)
        raise
    if not written:
        print("custom-build heartbeat failed: events.write_failed (the store did not take the heartbeat); "
              "the details are in Hermes's errors.log", file=sys.stderr)
        return 1
    return 0
