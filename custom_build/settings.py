"""The build's settings and the per-turn check on them (ARCHITECTURE.md section 5.5).

The startup check reads the same settings before Hermes starts; the plugin's `pre_llm_call` hook
reads them again on every turn, in the process that serves it, which a hand-started Hermes never
checked before.
"""

import psycopg

from . import failures

MACHINE_NAME = "machine_name"   # plugins.entries.custom_build.settings.machine_name in config.yaml
DATABASE_URL = "CUSTOM_BUILD_DATABASE_URL"   # a secret, in the profile's .env
JUDGE_MODEL = "judge_model"   # plugins.entries.custom_build.settings.judge_model: the judge, pinned by version
JUDGE_KEY = "TYPESAFE_API_KEY"   # a secret, in the profile's .env
CONNECT_TIMEOUT = 5   # seconds, well under Hermes's 30-second deadline for a hook


def _usable(value):
    """A setting is usable only as text with more than whitespace in it; anything else, an unquoted
    number or date included, counts as missing (#71)."""
    return isinstance(value, str) and bool(value.strip())


def _connection_string(value):
    """Usable, and a connection string PostgreSQL's client library can parse (#83)."""
    if not _usable(value):
        return False
    try:
        psycopg.conninfo.conninfo_to_dict(value)
    except psycopg.ProgrammingError:   # what conninfo_to_dict raises for a malformed string
        return False
    return True


def missing_settings(ctx):
    """The names of the build's settings that are not set, set to something other than text, or, for the
    connection string, set to text PostgreSQL cannot parse."""
    from agent.secret_scope import get_secret

    found = (("plugins.entries.custom_build.settings." + MACHINE_NAME, _usable(ctx.get_config(MACHINE_NAME))),
             (DATABASE_URL, _connection_string(get_secret(DATABASE_URL))),
             ("plugins.entries.custom_build.settings." + JUDGE_MODEL, _usable(ctx.get_config(JUDGE_MODEL))),
             (JUDGE_KEY, _usable(get_secret(JUDGE_KEY))))
    return [name for name, ok in found if not ok]


def machine(ctx):
    """The machine's name, for the `source` of what it records; "unknown" when it is not usable."""
    value = ctx.get_config(MACHINE_NAME)
    return value if _usable(value) else "unknown"


def plugin_context():
    """A context that reads the build's settings as Hermes does, for the memory-provider load.

    That load's own context forwards registration calls only, so it has no `get_config`; Hermes builds
    a context the same way to forward them (Hermes v0.23.1, `plugins/memory/__init__.py`).
    """
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    return PluginContext(PluginManifest(name="custom_build", key="custom_build"), get_plugin_manager())


def connect():
    """A new connection to the store, or None when no URL is set."""
    from agent.secret_scope import get_secret

    url = get_secret(DATABASE_URL)
    return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) if _usable(url) else None


def check_turn(ctx, **kwargs):
    """The `pre_llm_call` hook: a block naming each missing setting (`config.missing`), else None.

    With a setting missing there is nowhere to record, as in the startup check, so the code is logged
    with events.write_failed.
    """
    if not (missing := missing_settings(ctx)):
        return None
    names = ", ".join(missing)
    failures.record(None, "config.missing", "failed", source=machine(ctx), reason=names,
                    session_id=kwargs.get("session_id"))
    return {"action": "block", "message": f"The custom build blocked this turn: config.missing ({names}). "
                                          "Set each one as text, quoting a value YAML would read as a number "
                                          "or date, and the connection string as one PostgreSQL can parse, "
                                          "then send your message again."}


def crash_block(exc):
    """The block the boundary wrapper returns when the check itself crashed (`guard.crash`)."""
    return {"action": "block", "message": "The custom build blocked this turn: guard.crash in its per-turn "
                                          "settings check; the traceback is in Hermes's errors.log. "
                                          "Send your message again once it is fixed."}
