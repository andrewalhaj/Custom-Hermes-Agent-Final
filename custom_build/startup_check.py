"""The startup check, `hermes custom-build check` (ARCHITECTURE.md section 5.5).

The service that keeps the Hermes gateway running runs this first and starts Hermes only when it
exits 0. Hermes runs a plugin's subcommand inside its own process, after its ordinary plugin
discovery, so the check reads every setting through the function Hermes itself uses for it.
"""

import os
import sys

import psycopg

from . import failures

MACHINE_NAME = "machine_name"   # plugins.entries.custom_build.settings.machine_name in config.yaml
DATABASE_URL = "CUSTOM_BUILD_DATABASE_URL"   # a secret, in the profile's .env
UNATTENDED_MODES = ("cron_mode", "single_query_mode", "unattended_mode")


def load_problems():
    """What stops either of the plugin's two loads, as read from Hermes (`guard.not_loaded`)."""
    from hermes_cli.plugins import get_plugin_manager
    from plugins.memory import load_memory_provider

    problems = []
    entry = next((p for p in get_plugin_manager().list_plugins() if p["name"] == "custom_build"), None)
    if entry is None or entry["error"] or not entry["enabled"]:
        problems.append(f"the ordinary load failed: {entry['error'] if entry else 'no plugin entry'}")
    if getattr(load_memory_provider("custom_build"), "name", None) != "custom_build":
        problems.append("Hermes's memory-provider loader did not return the custom_build provider")
    return problems


def unsafe_settings():
    """Hermes settings with a value the build does not support (`config.unsafe`)."""
    from hermes_cli.config import load_config_readonly
    from tools.approval_context import _binary_approval_mode, _get_approval_mode
    from utils import is_truthy_value

    config = load_config_readonly() or {}
    found = []
    if (config.get("memory") or {}).get("provider") != "custom_build":
        found.append("memory.provider is not custom_build")
    if _get_approval_mode() == "off":
        found.append("approvals.mode is off")
    found += [f"approvals.{mode} does not read as deny" for mode in UNATTENDED_MODES if _binary_approval_mode(mode) != "deny"]
    if is_truthy_value(os.getenv("HERMES_YOLO_MODE", "")):
        found.append("HERMES_YOLO_MODE is set")
    if not is_truthy_value((config.get("compression") or {}).get("in_place"), default=True):
        found.append("compression.in_place is false")
    return found


def run(ctx):
    """Run every check; return the exit code: 0 when all pass, 1 otherwise."""
    from agent.secret_scope import get_secret

    machine = ctx.get_config(MACHINE_NAME)
    url = get_secret(DATABASE_URL)
    missing = [name for name, value in (("plugins.entries.custom_build.settings." + MACHINE_NAME, machine),
                                        (DATABASE_URL, url)) if not value]
    if missing:
        failures.record(None, "config.missing", "failed", source=machine or "unknown", reason=", ".join(missing))
        print(f"custom-build check failed: config.missing ({', '.join(missing)}); "
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

    failures.run_job("startup-check", check, connect=lambda: psycopg.connect(url, autocommit=True), source=machine)
    for line in failed:
        print(f"custom-build check failed: {line}", file=sys.stderr)
    return 1 if failed else 0
