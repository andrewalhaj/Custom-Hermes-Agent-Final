"""The build's two settings and the per-turn check on them (ARCHITECTURE.md section 5.5).

The startup check reads the same two settings before Hermes starts; the plugin's `pre_llm_call` hook
reads them again on every turn, so a setting removed while Hermes runs blocks the next turn.
"""

import psycopg

from . import failures

MACHINE_NAME = "machine_name"   # plugins.entries.custom_build.settings.machine_name in config.yaml
DATABASE_URL = "CUSTOM_BUILD_DATABASE_URL"   # a secret, in the profile's .env
CONNECT_TIMEOUT = 5   # seconds, well under Hermes's 30-second deadline for a hook


def missing_settings(ctx):
    """The names of the build's settings that are not set."""
    from agent.secret_scope import get_secret

    found = (("plugins.entries.custom_build.settings." + MACHINE_NAME, ctx.get_config(MACHINE_NAME)),
             (DATABASE_URL, get_secret(DATABASE_URL)))
    return [name for name, value in found if not value]


def store(ctx):
    """The machine's name and a new connection to the store, or None for it when no URL is set."""
    from agent.secret_scope import get_secret

    url = get_secret(DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) if url else None
    return ctx.get_config(MACHINE_NAME) or "unknown", conn


def check_turn(ctx, **kwargs):
    """The `pre_llm_call` hook: a block naming each missing setting (`config.missing`), else None.

    With a setting missing there is nowhere to record, as in the startup check, so the code is logged
    with events.write_failed.
    """
    if not (missing := missing_settings(ctx)):
        return None
    names = ", ".join(missing)
    failures.record(None, "config.missing", "failed", source=ctx.get_config(MACHINE_NAME) or "unknown",
                    reason=names, session_id=kwargs.get("session_id"))
    return {"action": "block", "message": f"The custom build blocked this turn: config.missing ({names}). "
                                          "Set it, then send your message again."}


def crash_block(exc):
    """The block the boundary wrapper returns when the check itself crashed (`guard.crash`)."""
    return {"action": "block", "message": "The custom build blocked this turn: guard.crash in its per-turn "
                                          "settings check; the traceback is in Hermes's errors.log. "
                                          "Send your message again once it is fixed."}
