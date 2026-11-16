"""The heartbeat, `hermes custom-build heartbeat` (ARCHITECTURE.md section 6.3, OPERATIONS.md section 4).

Hermes cron runs it every 6 hours on every agent host, through its wrapper in the Hermes home's
`scripts/` folder. Each run records one host.heartbeat event; the job-health check reports a host with
none in the last 24 hours as host.missing. Each run also records guard.bypassed, once, for every local
session that ran without the build: one started in safe mode or with the profile's settings ignored (#23).
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
        written = failures.run_job("heartbeat", lambda conn: beat(conn, machine), connect=settings.connect, source=machine)
    except SystemExit as stop:
        print(f"custom-build heartbeat failed: {failures.exit_note(stop)}", file=sys.stderr)
        raise
    if not written:
        print("custom-build heartbeat failed: events.write_failed (the store did not take the heartbeat); "
              "the details are in Hermes's errors.log", file=sys.stderr)
        return 1
    return 0


def beat(conn, machine):
    """Record this host's heartbeat, then each bypass session not yet recorded; return whether the heartbeat was written.

    The heartbeat goes first, so a scan that fails still leaves the host's beat in the store.
    """
    written = failures.record(conn, "host.heartbeat", "ok", source=machine)
    recorded = {sid for (sid,) in conn.execute(
        "select session_id from memory.events where code = 'guard.bypassed' and source = %s", (machine,))}
    for sid, flag in bypassed_sessions():
        if sid not in recorded:
            failures.record(conn, "guard.bypassed", "failed", source=machine, session_id=sid, reason=flag)
    return written


def bypassed_sessions():
    """(session id, the flag) for each local session whose launch policy set safe_mode or ignore_user_config.

    Hermes keeps each local session's policy in its state database, and the build is in none of these
    sessions, so they are found here, after the fact (#23). Each is read the way Hermes reads one,
    `restore_policy(local_receipt(...))`, through a read-only SessionDB; Hermes lists no receipts, so the
    ids come from one query shaped like its own (`hermes_state_local.moved_receipts`). A home with no
    state.db has opened no local session. Safe mode always sets ignore_user_config, which
    restore_policy enforces.
    """
    from gateway.session_policy import restore_policy
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from hermes_state_local import POLICY_PREFIX, local_receipt

    path = get_hermes_home() / "state.db"
    if not path.exists():
        return []
    db = SessionDB(path, read_only=True)
    try:
        ids = [row[0] for row in db._read_all("select substr(key, ?) from state_meta where key glob ?",
                                               (len(POLICY_PREFIX) + 1, POLICY_PREFIX + "*"))]
        policies = [(sid, restore_policy(local_receipt(db, sid)["policy"])) for sid in ids]
    finally:
        db.close()
    return [(sid, "safe_mode" if p.safe_mode else "ignore_user_config") for sid, p in policies if p.ignore_user_config]
