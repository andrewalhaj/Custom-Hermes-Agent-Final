"""The startup check, `hermes custom-build check` (ARCHITECTURE.md section 5.5).

The service that keeps the Hermes gateway running runs this first and starts Hermes only when it
exits 0. Hermes runs a plugin's subcommand inside its own process, after its ordinary plugin
discovery, so the check reads every setting through the function Hermes itself uses for it.
"""

import os
import sys

import psycopg

from . import failures, settings
from .settings import DATABASE_URL

UNATTENDED_MODES = ("cron_mode", "single_query_mode", "unattended_mode")
# Seconds. Below it Hermes may skip a slow guard and let its turn through; at 0 it runs hooks with no
# deadline, so a hung guard hangs the turn. #75 settled on 30 or more (#69).
HOOK_TIMEOUT_FLOOR = 30
# The newest file in migrations/ this plugin was built against (tests/test_schema_level.py keeps them in step).
STORE_LEVEL = 7


def load_problems():
    """What stops the provider load, as read from Hermes (`guard.not_loaded`).

    The ordinary load needs no check here: when it fails, Hermes drops everything the plugin
    registered, this subcommand included, so the check cannot run at all.
    """
    from plugins.memory import load_memory_provider

    if getattr(load_memory_provider("custom_build"), "name", None) != "custom_build":
        return ["Hermes's memory-provider loader did not return the custom_build provider"]
    return []


def unsafe_settings():
    """Hermes settings with a value the build does not support (`config.unsafe`)."""
    from agent.redact import _redact_enabled
    from hermes_cli.config import load_config_readonly
    from hermes_cli.gateway_migrate_guards import auto_migration_opted_out
    from hermes_cli.plugins import _resolve_hook_callback_timeout
    from hermes_constants import get_default_hermes_root
    from tools.approval_context import _binary_approval_mode, _get_approval_mode
    from tools.write_approval import write_approval_enabled
    from utils import TRUTHY_STRINGS, is_truthy_value

    config = load_config_readonly() or {}
    found = []
    if (config.get("memory") or {}).get("provider") != "custom_build":
        found.append("memory.provider is not custom_build")
    found += [f"{part}.write_approval is off" for part in ("memory", "skills") if not write_approval_enabled(part)]
    # The gateway exports the key over HERMES_REDACT_SECRETS for its child processes (gateway/run.py), so either off is off.
    redact_key = (config.get("security") or {}).get("redact_secrets")
    if not _redact_enabled() or (redact_key is not None and str(redact_key).lower() not in TRUTHY_STRINGS):
        found.append("security.redact_secrets (or HERMES_REDACT_SECRETS) is off")
    if _get_approval_mode() == "off":
        found.append("approvals.mode is off")
    if (timeout := _resolve_hook_callback_timeout()) < HOOK_TIMEOUT_FLOOR:
        found.append(f"plugins.hook_callback_timeout is {timeout:g} seconds, below {HOOK_TIMEOUT_FLOOR}")
    found += [f"approvals.{mode} does not read as deny" for mode in UNATTENDED_MODES if _binary_approval_mode(mode) != "deny"]
    if is_truthy_value(os.getenv("HERMES_YOLO_MODE", "")):
        found.append("HERMES_YOLO_MODE is set")
    if not is_truthy_value((config.get("compression") or {}).get("in_place"), default=True):
        found.append("compression.in_place is false")
    if len(profiles := installed_profiles()) > 1:
        found.append(f"the host has {len(profiles)} Hermes profiles ({', '.join(profiles)}), not one")
    # The check runs only at start; with this on, a running gateway also serves a profile added later (#85).
    if not auto_migration_opted_out(get_default_hermes_root()):
        found.append("gateway.auto_multiplex_migration is not false")
    if serves_every_profile_from_start():
        found.append("gateway.multiplex_profiles (or GATEWAY_MULTIPLEX_PROFILES) reads as true")
    return found


def store_behind(conn):
    """Why the store is behind the migrations this plugin needs, or "" when it is not (`db.behind`).

    A store ahead of the plugin passes: each migration works with the plugin deployed before it
    (CONTRIBUTING.md rule C10).
    """
    try:
        row = conn.execute("select level from memory.schema_level").fetchone()
    except psycopg.errors.UndefinedTable:
        return (f"the store has no memory.schema_level, so it is before migration 0006; the plugin needs "
                f"migration {STORE_LEVEL}")
    level = row[0] if row else None
    if level is None or level < STORE_LEVEL:
        return f"the store is at migration {level}; the plugin needs migration {STORE_LEVEL}"
    return ""


def installed_profiles():
    """The names of every Hermes profile on this host, parked and standalone ones included.

    One gateway serves every profile by default, while this check reads one profile's settings, so an
    agent host has one profile (#63). Listed the way Hermes itself lists the profiles it can serve.
    """
    from hermes_cli.profiles import profiles_to_serve

    return [name for name, _ in profiles_to_serve(multiplex=True, include_standalone=True, include_parked=True)]


def serves_every_profile_from_start():
    """True when the gateway would serve every profile from start, which skips the opt-out above (#85).

    Read the way the gateway's boot reads it (`gateway/run.py`): its config, merged from the legacy
    `gateway.json`, `config.yaml` with its managed overlay, and `GATEWAY_MULTIPLEX_PROFILES`.
    """
    from gateway.config import load_gateway_config
    from hermes_cli.gateway_multiplex_mode import settled_explicit_flag

    return bool(settled_explicit_flag(load_gateway_config().multiplex_profiles))


def run(ctx):
    """Run every check; return the exit code: 0 when all pass, 1 otherwise."""
    from agent.secret_scope import get_secret

    machine = settings.machine(ctx)
    url = get_secret(DATABASE_URL)
    if missing := settings.missing_settings(ctx):
        failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing))
        print(f"custom-build check failed: config.missing ({', '.join(missing)}): set each one as text, "
              "quoting a value YAML would read as a number or date, and the connection string as one PostgreSQL "
              "can parse; "
              "not recorded in events: events.write_failed (no connection to the store)", file=sys.stderr)
        return 1

    failed = []

    def check(conn):
        # Each code is written out, so the catalog test can read it (CONTRIBUTING.md rule F6).
        if not_loaded := "; ".join(load_problems()):
            failures.record(conn, "guard.not_loaded", "failed", source=machine, reason=not_loaded)
            failed.append(f"guard.not_loaded ({not_loaded})")
        if unsafe := "; ".join(unsafe_settings()):
            failures.record(conn, "config.unsafe", "failed", source=machine, reason=unsafe)
            failed.append(f"config.unsafe ({unsafe})")
        # The store admits a machine's events only under its login's name (migration 0007, #149).
        if (login := conn.execute("select current_user").fetchone()[0]) != machine:
            named = f"machine_name is {machine!r} but the store's login is {login!r}: they must be the same name"
            failures.record(conn, "config.missing", "failed", source=machine, reason=named)
            failed.append(f"config.missing ({named})")
        if behind := store_behind(conn):
            failures.record(conn, "db.behind", "failed", source=machine, reason=behind)
            failed.append(f"db.behind ({behind}): apply the pending migrations first (OPERATIONS.md section 7)")

    try:
        failures.run_job("startup-check", check, connect=lambda: psycopg.connect(url, autocommit=True), source=machine)
    except SystemExit as stop:
        # The service log shows only stderr, and run_job recorded the cause elsewhere: say which and where (#64).
        print(f"custom-build check failed: {failures.exit_note(stop)}", file=sys.stderr)
        raise
    for line in failed:
        print(f"custom-build check failed: {line}", file=sys.stderr)
    return 1 if failed else 0
