"""custom_build: one Hermes plugin that Hermes loads twice (ARCHITECTURE.md section 1).

`register()` runs under both loads. The ordinary load, from `plugins.enabled`, passes a full plugin
context; the memory-provider load, from `memory.provider`, passes a narrower one without `state`.
Both register the redaction patterns, so the provider never writes without them; only the provider
load registers the provider, and only the ordinary load registers the `hermes custom-build` command.
"""

import json

import psycopg
from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret

from . import failures, settings
from .memory import embedding, judge, recall, store, tool

# Supabase API key formats, which Hermes's redactor does not cover (CONTRIBUTING.md rule L4).
REDACTION_PATTERNS = (r"sb_secret_[A-Za-z0-9_-]{20,}", r"sb_publishable_[A-Za-z0-9_-]{20,}")
START_TIMEOUT = 5   # seconds for each answer from Ollama while the provider starts


def _machine():
    return settings.machine(settings.plugin_context())


def _judge():
    return store.Judge(url=judge.URL, key=get_secret(settings.JUDGE_KEY),
                       model=settings.plugin_context().get_config(settings.JUDGE_MODEL))


class CustomBuildProvider(MemoryProvider):
    """The memory provider. When it starts it checks this machine's embedding build against the store
    (ARCHITECTURE.md section 3.2) and retries facts queued for the judge; its `store_fact` tool stores a
    fact on purpose (3.3). Recall arrives with its own pull request."""

    session_id = None
    turn = None
    primary = False

    @property
    def name(self) -> str:
        return "custom_build"

    def is_available(self) -> bool:
        return True

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def initialize(self, session_id: str, **kwargs) -> None:
        """Check the embedding build, recording a failed check with its code (#65 keeps is_available() out),
        then retry the facts queued for the judge."""
        self.session_id = session_id
        self.embedder = embedding.Embedder(embedding.OLLAMA_URL)
        # Hermes asks providers to write only in a primary session, not a subagent, cron run or flush.
        self.primary = kwargs.get("agent_context", "primary") == "primary"
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
            if self.embedder.build is not None and self.primary:
                tool.retry_queued(conn, embedder=self.embedder, judge_at=_judge())

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self.turn = turn_number   # a fact records the user turn it was stored in

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self.session_id = new_session_id

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def on_memory_write(self, action: str, target: str, content: str, metadata: dict | None = None) -> None:
        """Mirror a committed write to MEMORY.md or USER.md into the facts store (ARCHITECTURE.md section 2)."""
        machine = _machine()
        if missing := settings.missing_settings(settings.plugin_context()):
            failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing),
                            session_id=self.session_id)
            return
        tool.mirror(action, content, metadata or {}, machine=machine, session_id=self.session_id, turn=self.turn,
                    embedder=self.embedder, judge_at=_judge(), connect=settings.connect)

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: list | None = None, **kwargs) -> None:
        """Extract facts from the finished turn (ARCHITECTURE.md 3.3). Hermes runs this on its background
        worker; the assistant's reply is never read."""
        if not self.primary:
            return
        machine = _machine()
        if missing := settings.missing_settings(settings.plugin_context()):
            failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing),
                            session_id=self.session_id)
            return
        tool.extract(user_content, messages or [], machine=machine, session_id=self.session_id, turn=self.turn,
                     embedder=self.embedder, judge_at=_judge(), connect=settings.connect)

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def on_session_end(self, messages: list) -> None:
        """Write the session's digest if it is long (ARCHITECTURE.md 3.3). Hermes calls this at a session's end, and
        also at each compression and when the gateway evicts an idle agent, where the session goes on (#156)."""
        if not self.primary:
            return
        machine = _machine()
        if missing := settings.missing_settings(settings.plugin_context()):
            failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing),
                            session_id=self.session_id)
            return
        tool.write_digest(messages or [], machine=machine, session_id=self.session_id, turn=self.turn,
                          embedder=self.embedder, judge_at=_judge(), connect=settings.connect)

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """The recalled notes for the coming turn (ARCHITECTURE.md 3.5), or a failure with its code."""
        machine = _machine()
        if missing := settings.missing_store_settings(settings.plugin_context()):
            failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing),
                            session_id=self.session_id)
            return recall.failed("config.missing")
        return tool.recall_notes(query, machine=machine, session_id=self.session_id, turn=self.turn,
                                 embedder=self.embedder, connect=settings.connect)

    def get_tool_schemas(self) -> list:
        return [tool.SCHEMA, tool.SEARCH_SCHEMA]

    @failures.boundary(_machine, settings.connect, failures.PROVIDER)
    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name not in (tool.SCHEMA["name"], tool.SEARCH_SCHEMA["name"]):
            return super().handle_tool_call(tool_name, args, **kwargs)
        machine = _machine()
        if missing := settings.missing_settings(settings.plugin_context()):
            failures.record(None, "config.missing", "failed", source=machine, reason=", ".join(missing),
                            session_id=self.session_id)
            return json.dumps({"error": "config.missing"})
        if tool_name == tool.SEARCH_SCHEMA["name"]:
            outcome = tool.memory_search(args, machine=machine, session_id=self.session_id, turn=self.turn,
                                         embedder=self.embedder, connect=settings.connect)
        else:
            outcome = tool.store_fact(args, machine=machine, session_id=self.session_id, turn=self.turn,
                                      embedder=self.embedder, judge_at=_judge(), connect=settings.connect)
        return json.dumps(outcome, default=str)   # fact ids are UUIDs


def _add_subcommands(parser) -> None:
    commands = parser.add_subparsers(dest="custom_build_command", required=True)
    commands.add_parser("check", help="Run the startup check; exits non-zero when Hermes must not start")
    commands.add_parser("heartbeat", help="Record this host's heartbeat; Hermes cron runs it every 6 hours")
    commands.add_parser("job-health", help="Record missed jobs, jobs with no output and missing hosts; daily")
    commands.add_parser("report", help="Send the day's failure report, or an all-clear; daily at 07:00")


def register(ctx) -> None:
    ctx.register_redaction_patterns(REDACTION_PATTERNS)
    # The provider load's context has no `state`; checked on the class, so the ordinary load's
    # cached `state` property is not built as a side effect.
    if not hasattr(type(ctx), "state"):
        ctx.register_memory_provider(CustomBuildProvider())
        return

    from . import failures, settings
    from .guards import fact_gate, verify_gate, write_gate
    from .memory import digest, extraction

    ctx.register_auxiliary_task(extraction.TASK, display_name="Custom build: fact extraction",
                                description="Proposes facts from each turn's user message and tool results (#148)",
                                defaults=extraction.DEFAULTS)
    ctx.register_auxiliary_task(digest.TASK, display_name="Custom build: session digest",
                                description="Summarises a long session at its end (#155)",
                                inherit_from=extraction.TASK)

    @failures.boundary(lambda: settings.machine(ctx), settings.connect, settings.crash_block)
    def check_turn(**kwargs):
        return settings.check_turn(ctx, **kwargs)

    # Before the subcommand: if any fails, Hermes drops the load and the startup check cannot pass.
    ctx.register_hook("pre_llm_call", check_turn)
    write_gate.register(ctx)
    verify_gate.register(ctx)
    fact_gate.register(ctx)

    def run_command(args) -> int:
        from . import heartbeat, job_health, report, startup_check

        run = {"check": startup_check.run, "heartbeat": heartbeat.run,
               "job-health": job_health.run, "report": report.run}[args.custom_build_command]
        return run(ctx)   # Hermes turns a non-zero return into the exit code

    ctx.register_cli_command("custom-build", "The custom build's commands", _add_subcommands, run_command)
