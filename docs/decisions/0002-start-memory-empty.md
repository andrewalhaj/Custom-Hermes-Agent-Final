# 0002. Memory starts from an empty database

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

The Supabase project from the old build holds about 1,700 facts and 15,800 session chunks. About 300 facts were extracted from the agent's own replies, and about 140 have no recorded origin. The old schema has none of the columns the new design needs (`basis`, `source_turn`, `project`).

## Options

1. **Migrate the old rows** into the new tables and backfill the new columns.
2. **Wipe the project** and start empty.

## Decision

Option 2. The project is wiped and rebuilt from this repository's migrations. No backup is kept.

## Consequences

- **No backfill work.** Every row has `basis`, `project` and `source` from day one.
- **No legacy rows of unknown origin** can be recalled as if they were verified.
- **The eval set cannot use old facts as expected answers.** It starts from LongMemEval-S and grows from real misses (decision 0007).
- **The wipe is a one-time operation.** It is tracked in its own issue, not in CONTRIBUTING.md. The old build must be stopped first, or it writes old-format rows into the empty database.
- **The old data cannot be recovered.**
