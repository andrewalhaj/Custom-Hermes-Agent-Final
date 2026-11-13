# 0006. Drop the skills store and the memory-cap guard

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

Two parts of the old build duplicate something Hermes already does.

- **Skills store.** The old recall searched skill names and descriptions by word overlap. Hermes already puts an index of every skill's name and description in the system prompt and tells the agent to scan it before replying. Skill loads were 1.5% of tool results over 14 days.
- **Memory-cap guard.** The guard blocked most tools once `MEMORY.md` or `USER.md` reached 80% of its cap: 29 blocked calls in 14 days. Hermes's memory tool already refuses a write over the cap and tells the agent to consolidate in the same turn.

## Decision

Drop both. Recall searches facts, references and sessions only. Hermes enforces the memory caps.

## Consequences

- **Less code, and less in each turn's recalled notes.**
- **Skill use depends on Hermes's own index.** If skill loads stay low, that is a question for the eval set and for skill descriptions, not for a second search.
- **A memory file can fill up to its cap** before anything stops writes. Hermes then refuses the write, and the failure is recorded as `memory.write_rejected`.

## Sources

- Hermes docs: prompt assembly (`<available_skills>` index); memory ("does not auto-compact… the memory tool returns an error").
- Old-build guard counts from its logs and gateway journal, 14 days to 2026-11-11.
