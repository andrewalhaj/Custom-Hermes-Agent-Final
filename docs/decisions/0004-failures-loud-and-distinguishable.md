# 0004. Every failure is loud and distinguishable

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

The old build has 188 catch-everything handlers outside its tests. A pattern-based count puts about 100 of them as silent: they return an empty value, `pass`, `continue`, or count the failure as "skipped". Two real cases:

- **The search returned an empty list on any error.** A database outage and a code bug both looked like "no matches".
- **The July heartbeat incident.** A refactor changed a function's arguments, so every call raised a `TypeError`. A broad handler counted each failure as "skipped". For six days the log said "0 stored, N skipped", which looked like normal deduplication.

Several paths also failed open: the store gate stored a fact when its judge errored, and a guard that crashed mid-turn let the call through.

## Decision

Every point of failure is loud and distinguishable. Rules F1–F6 in CONTRIBUTING.md section 8.5:

- Each failure point has a named code from one catalog.
- A failure, an empty result and a degraded result are different outcomes.
- Every occurrence is logged at WARNING or above and recorded in the `events` table.
- A failure that changed the answer is shown in the turn.
- A failure to record a failure is logged locally.
- A test enforces the catalog, and an unknown failure crashes and is recorded as `unhandled`.

A daily report goes to a dedicated Telegram channel, `<telegram-channel>`, as a dated `.md` file. With nothing to report it sends a one-line all-clear, so a missing report is itself the alarm.

## Consequences

- **Loud does not mean every failure interrupts the chat.** In-turn notices are kept for failures that changed the answer, so they are not ignored. Everything else is in the log, the `events` table and the daily report.
- **A retry that succeeds is recorded as degraded,** so a flaky service shows up as a trend.
- **Guards fail closed.** A crashed guard withholds the reply and says why.
- **Absence is checked.** A job with no fresh success (`job.missed`), or that took input but produced nothing (`job.no_output`), is flagged.
- **Failures inside Hermes itself** are visible only through its hooks and logs. They get codes, but their detail is whatever Hermes reports.
- **No old code is ported unchanged** (rule C9).
