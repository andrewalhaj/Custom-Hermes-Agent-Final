"""How a scheduled job runs as a `hermes custom-build <job>` subcommand (OPERATIONS.md section 4)."""

import sys

from . import failures, settings


def run(ctx, name, job, *, missing=()):
    """Run job(conn, machine) through the job wrapper; return the exit code, 0 or 1.

    `job` returns whether its own row was written. `missing` names job-specific settings that are missing
    or invalid, checked with the store's own before anything connects.

    It prints nothing when it succeeds, so Hermes cron delivers nothing. Every failure exits non-zero, so
    Hermes cron delivers its error alert, and prints one line to stderr that the alert carries, naming
    the failure code and where its details are (#64):
    - a missing setting is config.missing, logged with events.write_failed, as in the startup check;
    - a connection that fails to open is db.auth or db.unreachable, logged by the job wrapper;
    - a row the store does not take is events.write_failed;
    - a crash is unhandled and job.failed, recorded by the job wrapper.
    """
    machine = settings.machine(ctx)
    if missing := [*settings.missing_store_settings(ctx), *missing]:
        names = ", ".join(missing)
        failures.record(None, "config.missing", "failed", source=machine, reason=names)
        print(f"custom-build {name} failed: config.missing ({names}); not recorded in events: "
              "events.write_failed (no connection to the store)", file=sys.stderr)
        return 1
    try:
        written = failures.run_job(name, lambda conn: job(conn, machine), connect=settings.connect, source=machine)
    except SystemExit as stop:
        print(f"custom-build {name} failed: {failures.exit_note(stop)}", file=sys.stderr)
        raise
    if not written:
        print(f"custom-build {name} failed: events.write_failed (the store did not take its row); "
              "the details are in Hermes's errors.log", file=sys.stderr)
        return 1
    return 0
