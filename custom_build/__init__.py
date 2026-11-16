"""custom_build: one Hermes plugin that Hermes loads twice (ARCHITECTURE.md section 1).

`register()` runs under both loads. The ordinary load, from `plugins.enabled`, passes a full plugin
context; the memory-provider load, from `memory.provider`, passes a narrower one without `state`.
Both register the redaction patterns, so the provider never writes without them; only the provider
load registers the provider, and only the ordinary load registers the `hermes custom-build` command.
"""

import psycopg
from agent.memory_provider import MemoryProvider

from . import failures, settings
from .memory import embedding

# Supabase API key formats, which Hermes's redactor does not cover (CONTRIBUTING.md rule L4).
REDACTION_PATTERNS = (r"sb_secret_[A-Za-z0-9_-]{20,}", r"sb_publishable_[A-Za-z0-9_-]{20,}")
START_TIMEOUT = 5   # seconds for each answer from Ollama while the provider starts


def _machine():
    return settings.machine(settings.plugin_context())


class CustomBuildProvider(MemoryProvider):
    """The memory provider. When it starts it checks this machine's embedding build against the store
    (ARCHITECTURE.md section 3.2); storing and recall arrive with their own pull requests."""

    @property
    def name(self) -> str:
        return "custom_build"

    def is_available(self) -> bool:
        return True

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def initialize(self, session_id: str, **kwargs) -> None:
        """Check the embedding build, recording a failed check with its code (#65 keeps is_available() out)."""
        self.embedder = embedding.Embedder(embedding.OLLAMA_URL)
        machine = _machine()
        if missing := settings.missing_settings(settings.plugin_context()):
            failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing),
                            session_id=session_id)
            return
        try:
            conn = settings.connect()
        except psycopg.OperationalError as exc:
            if failures.refused_login(exc):
                failures.record(None, "db.auth", "failed", source=machine, reason=str(exc), session_id=session_id)
            else:
                failures.record(None, "db.unreachable", "failed", source=machine, reason=str(exc), session_id=session_id)
            return
        with conn:
            try:
                self.embedder.check(conn, timeout=START_TIMEOUT)
            except embedding.EmbedUnavailable as exc:
                failures.record(conn, "embed.unavailable", "failed", source=machine, reason=str(exc), session_id=session_id)
            except embedding.ModelMismatch as exc:
                failures.record(conn, "embed.model_mismatch", "failed", source=machine, reason=str(exc),
                                session_id=session_id)

    def get_tool_schemas(self) -> list:
        return []


def _add_subcommands(parser) -> None:
    commands = parser.add_subparsers(dest="custom_build_command", required=True)
    commands.add_parser("check", help="Run the startup check; exits non-zero when Hermes must not start")
    commands.add_parser("heartbeat", help="Record this host's heartbeat; Hermes cron runs it every 6 hours")


def register(ctx) -> None:
    ctx.register_redaction_patterns(REDACTION_PATTERNS)
    # The provider load's context has no `state`; checked on the class, so the ordinary load's
    # cached `state` property is not built as a side effect.
    if not hasattr(type(ctx), "state"):
        ctx.register_memory_provider(CustomBuildProvider())
        return

    from . import failures, settings
    from .guards import write_gate

    @failures.boundary(lambda: settings.machine(ctx), settings.connect, settings.crash_block)
    def check_turn(**kwargs):
        return settings.check_turn(ctx, **kwargs)

    # Before the subcommand: if either fails, Hermes drops the load and the startup check cannot pass.
    ctx.register_hook("pre_llm_call", check_turn)
    write_gate.register(ctx)

    def run_command(args) -> int:
        from . import heartbeat, startup_check

        run = {"check": startup_check.run, "heartbeat": heartbeat.run}[args.custom_build_command]
        return run(ctx)   # Hermes turns a non-zero return into the exit code

    ctx.register_cli_command("custom-build", "The custom build's commands", _add_subcommands, run_command)
