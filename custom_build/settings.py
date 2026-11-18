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
AGENT_HOSTS = "agent_hosts"   # plugins.entries.custom_build.settings.agent_hosts on the job host: each agent host's name
EMBEDDING = "embedding"   # plugins.entries.custom_build.settings.embedding: "local" (unset) or "deepinfra" (#239)
EMBEDDING_ROUTES = ("local", "deepinfra")
EMBEDDING_KEY = "DEEPINFRA_API_KEY"   # a secret, in the profile's .env, needed with embedding: deepinfra
OLLAMA_URL = "ollama_url"   # plugins.entries.custom_build.settings.ollama_url: the local route's Ollama, if not this machine's (#261)
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


def missing_store_settings(ctx):
    """The names of the two settings anything that writes to the store needs, the heartbeat included, that
    are not set, set to something other than text, or, for the connection string, set to text PostgreSQL
    cannot parse."""
    from agent.secret_scope import get_secret

    found = (("plugins.entries.custom_build.settings." + MACHINE_NAME, _usable(ctx.get_config(MACHINE_NAME))),
             (DATABASE_URL, _connection_string(get_secret(DATABASE_URL))))
    return [name for name, ok in found if not ok]


def missing_embedding_settings(ctx):
    """The embedding route, when it names none the build has, and the hosted route's key, when it is chosen
    and not set (#239). Recall checks these too: Hermes runs prefetch before the turn check."""
    from agent.secret_scope import get_secret

    route, url = ctx.get_config(EMBEDDING), ctx.get_config(OLLAMA_URL)
    found = (("plugins.entries.custom_build.settings." + EMBEDDING, route is None or route in EMBEDDING_ROUTES),
             (EMBEDDING_KEY, route != "deepinfra" or _usable(get_secret(EMBEDDING_KEY))),
             ("plugins.entries.custom_build.settings." + OLLAMA_URL,
              url is None or (_usable(url) and url.strip().startswith(("http://", "https://")))))
    return [name for name, ok in found if not ok]


def missing_settings(ctx):
    """Every build setting that is missing or invalid: the store's two, the judge's model and key, then the
    embedding route's."""
    from agent.secret_scope import get_secret

    found = (("plugins.entries.custom_build.settings." + JUDGE_MODEL, _usable(ctx.get_config(JUDGE_MODEL))),
             (JUDGE_KEY, _usable(get_secret(JUDGE_KEY))))
    return (missing_store_settings(ctx) + [name for name, ok in found if not ok]
            + missing_embedding_settings(ctx))


def embedder(ctx):
    """The embedder the `embedding` setting names (#239)."""
    from agent.secret_scope import get_secret

    from .memory import embedding

    url = ctx.get_config(OLLAMA_URL)
    return embedding.for_setting(ctx.get_config(EMBEDDING), get_secret(EMBEDDING_KEY), url.strip() if url else None)


def agent_hosts(ctx):
    """The job host's list of agent host names, or None unless it is a non-empty list of distinct usable
    names with no padding, each written as its host's `machine_name` is."""
    value = ctx.get_config(AGENT_HOSTS)
    ok = (isinstance(value, list) and value and all(_usable(name) and name == name.strip() for name in value)
          and len(set(value)) == len(value))
    return value if ok else None


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
