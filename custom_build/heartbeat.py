"""The heartbeat, `hermes custom-build heartbeat` (ARCHITECTURE.md section 6.3, OPERATIONS.md section 4).

Hermes cron runs it every 6 hours on every agent host, through its wrapper in the Hermes home's
`scripts/` folder. Each run records one host.heartbeat event; the job-health check reports a host with
none in the last 24 hours as host.missing. Each run also records guard.bypassed, once, for every local
session that ran without the build: one started in safe mode or with the profile's settings ignored (#23).
"""

import sys

from . import failures, settings


def run(ctx):
    """Record one host.heartbeat event, then any session that ran without the build; return the exit code, 0 or 1.

    It prints nothing when it succeeds, so Hermes cron delivers nothing. Every failure exits non-zero, so
    Hermes cron delivers its error alert, and prints one line to stderr that the alert carries, naming
    the failure code and where its details are (#64):
    - a missing setting is config.missing, logged with events.write_failed, as in the startup check;
    - a connection that fails to open is db.auth or db.unreachable, logged by the job wrapper;
    - a row the store does not take is events.write_failed;
    - a crash is unhandled and job.failed, recorded by the job wrapper; so is a state.db Hermes cannot
      open or read, after the heartbeat itself is recorded.
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
    """(session id, reason) for each local session that ran without the build, from its launch policy (#23).

    Hermes keeps each local session's policy in its state database, and the build is in none of these
    sessions, so they are found here, after the fact. The database is opened read-only through Hermes's
    own SessionDB, and each policy is checked with Hermes's own reader, `restore_policy`. Hermes lists
    no receipts, so they come from one query shaped like its own (`hermes_state_local.moved_receipts`),
    which also reads each value, so a session deleted mid-scan cannot fail it. The reason is the flag
    set, `safe_mode` or `ignore_user_config`; safe mode always sets the second, which restore_policy
    enforces. A receipt that cannot be read (Hermes leaves a malformed one in place) is reported too, as
    "unreadable launch policy", because the build cannot show it ran there. A home with no state.db has
    opened no local session.
    """
    import json

    from gateway.session_policy import restore_policy
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_runtime import RuntimeStoreError

    path = get_hermes_home() / "state.db"
    if not path.exists():
        return []
    db = SessionDB(path, read_only=True)
    try:
        rows = db._read_all("select substr(key, ?), value from state_meta where key glob ?",
                            (len(POLICY_PREFIX) + 1, POLICY_PREFIX + "*"))
    finally:
        db.close()
    found = []
    for sid, value in rows:
        try:
            policy = restore_policy(json.loads(value)["policy"])
        except (ValueError, KeyError, TypeError, RuntimeStoreError):   # not JSON, no policy, or one Hermes refuses
            found.append((sid, "unreadable launch policy"))
            continue
        if policy.ignore_user_config:
            found.append((sid, "safe_mode" if policy.safe_mode else "ignore_user_config"))
    return found
