"""The heartbeat, `hermes custom-build heartbeat` (ARCHITECTURE.md section 6.3, OPERATIONS.md section 4).

Hermes cron runs it every 6 hours on every agent host, through its wrapper in the Hermes home's
`scripts/` folder. Each run records one host.heartbeat event; the job-health check reports a host with
none in the last 24 hours as host.missing.
"""

from . import failures, settings


def run(ctx):
    """Record one host.heartbeat event; return the exit code, 0 or 1.

    It prints nothing, so Hermes cron delivers nothing when it succeeds. A missing setting stops it with
    config.missing, logged with events.write_failed as in the startup check, and returns 1; so does a
    row the store does not take, logged as events.write_failed. A crash is recorded by the job wrapper
    as unhandled and job.failed, and exits non-zero. Either way Hermes cron delivers its error alert.
    """
    machine = settings.machine(ctx)
    if missing := settings.missing_settings(ctx):
        failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing))
        return 1
    written = failures.run_job("heartbeat", lambda conn: failures.record(conn, "host.heartbeat", "ok", source=machine),
                               connect=settings.connect, source=machine)
    return 0 if written else 1
