# 0005. Log through Hermes's logger, with an `events` table

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

Hermes v0.23.1 already owns logging for everything that runs inside it:

- It writes `agent.log`, `errors.log` (WARNING and above) and, in gateway mode, `gateway.log` under `~/.hermes/logs/`.
- It rotates them: 5 MB and 3 backups by default (`logging.max_size_mb`, `logging.backup_count`).
- It passes every line through a redacting formatter.
- It has a reader, `hermes logs`, that filters by session, component and time.

What Hermes does not provide:

- It does not redact Supabase's `sb_secret_` and `sb_publishable_` key formats.
- It does not redact anything a plugin writes somewhere other than a log, such as a Supabase row or a judge request.
- It has no structured format.
- It keeps no durable record of guard decisions.
- It logs a repeated identical error once at WARNING and then at DEBUG, so a guard that keeps failing goes quiet.

General advice to write JSON to stdout and leave files to the service manager fits a standalone service. It does not fit plugins running inside Hermes's process: a plugin that adds its own handlers bypasses Hermes's redaction.

## Decision

- **Plugins log through Hermes:** `logging.getLogger(__name__)`, with no handlers and no `basicConfig`. Rules L1–L5 in CONTRIBUTING.md section 9.4.
- **Each event is one line,** with its fields as one JSON object in the message.
- **Text is scrubbed before every write,** not only before logging. The plugin registers redaction patterns for the Supabase key formats.
- **No content goes in logs.** Logs carry IDs, counts, scores and hashes.
- **Guard decisions, recall events and failures also go into an `events` table in Supabase.** Logs rotate and live on one machine, but memory is shared. Rows keep 90 days. Rows promoted into the eval set are kept.

## Consequences

- **"What failed this week, on which machine" is one query.**
- **Recall misses can be rebuilt for the eval set** from the IDs plus the transcript, without storing query text twice.
- **pgaudit is not enabled.** It would mostly duplicate the `events` table. It can be added if database-side proof is ever needed.
- **`HERMES_PLUGINS_DEBUG` is never set on a shared machine,** because its console copy is not redacted.

## Sources

- Hermes source: `hermes_logging.py` (log files, rotation, `RedactingFormatter`); `agent/redact.py` (`redact_sensitive_text`, no `sb_secret_` pattern); `hermes_cli/plugins_context.py` (`register_redaction_patterns`).
- OWASP Logging Cheat Sheet (when, where, who and what; data to exclude; log injection).
- Python 3.12 logging HOWTO (module loggers; library code adds no handlers).
- OpenTelemetry GenAI conventions (content off by default; retrieval documents as id and score). The conventions are still in Development status, so only their field names are used.
