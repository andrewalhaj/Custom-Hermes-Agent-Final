# 0003. Facts are extracted only from the user's messages and tool results

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

The old build's heartbeat read the agent's own replies every 15 minutes and stored sentences that looked like facts. When the agent was wrong, the wrong claim became a stored fact and was recalled in later sessions as if it were known. The judge that gated those writes asked whether a fact was worth keeping, not whether it was true.

## Options

1. **Keep extracting from replies,** with a stricter judge.
2. **Extract only from the user's messages and from tool results** (command output, file contents, documentation lookups), and check each fact against its source.

## Decision

Option 2.

- Every fact records its `basis` (`user_stated`, `tool_observed` or `assistant_inferred`) and its `source_turn`.
- A fact is stored only if a separate check finds its source turn supports it. This is a different question from "is it worth keeping".
- A digest takes the weakest basis of its inputs, so a summary can never upgrade an inferred claim into a stated one.
- If the judge errors, the fact is queued for retry, not stored (`store.gate_error`).

## Consequences

- **Fewer facts are stored,** and recalled notes show their basis.
- **Facts the agent works out itself are no longer kept automatically.** The agent can still store one on purpose, and its basis is recorded.
- **Write-time checks are not perfect.** A 2026 audit study still found 17–21% of admitted memories unsupported. The support check is measured, not assumed to work.

## Sources

- Mem0's extraction prompt: "GENERATE FACTS SOLELY BASED ON THE USER'S MESSAGES" (github.com/mem0ai/mem0, `mem0/configs/prompts.py`).
- Evidence-linked extraction and its audit: arXiv 2605.30771; unsupported admissions: arXiv 2609.36130; provenance laundering in consolidation: arXiv 2607.29167. All three are single-team 2026 preprints.
