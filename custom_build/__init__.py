"""custom_build: one Hermes plugin that Hermes loads twice (ARCHITECTURE.md section 1).

`register()` runs under both loads. The ordinary load, from `plugins.enabled`, passes a full plugin
context; the memory-provider load, from `memory.provider`, passes a narrower one without `state`.
Both register the redaction patterns, so the provider never writes without them; only the provider
load registers the provider.
"""

from agent.memory_provider import MemoryProvider

# Supabase API key formats, which Hermes's redactor does not cover (CONTRIBUTING.md rule L4).
REDACTION_PATTERNS = (r"sb_secret_[A-Za-z0-9_-]{20,}", r"sb_publishable_[A-Za-z0-9_-]{20,}")


class CustomBuildProvider(MemoryProvider):
    """The memory provider. It stores and recalls nothing yet; recall arrives with the facts store."""

    @property
    def name(self) -> str:
        return "custom_build"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> list:
        return []


def register(ctx) -> None:
    ctx.register_redaction_patterns(REDACTION_PATTERNS)
    # The provider load's context has no `state`; checked on the class, so the ordinary load's
    # cached `state` property is not built as a side effect.
    if not hasattr(type(ctx), "state"):
        ctx.register_memory_provider(CustomBuildProvider())
