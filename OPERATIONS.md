# Operations

This document describes where the build runs and what must be configured for it to run correctly. It holds no real hostnames, project references, chat IDs or credentials: those are deployment values, set outside the repository. The design is in [ARCHITECTURE.md](ARCHITECTURE.md); the rules for changing anything are in [CONTRIBUTING.md](CONTRIBUTING.md).

Facts about Hermes are stated for **Hermes v0.23.1**. Re-check them before upgrading Hermes.

## 1. Deployment values

These placeholders appear in the documentation. Each deployment sets them in its Hermes profile's `.env` or its own configuration management, never in this repository.

| Placeholder | What it is |
|---|---|
| `<agent-hosts>` | The machines that run Hermes with this build. There can be several. |
| `<machine-name>` | Each agent host's name, written to the `source` column of every row it writes |
| `<job-host>` | One of the agent hosts that is on when scheduled jobs run |
| `<project-ref>` | The Supabase project that holds the memory store |
| `<model-digest>` | The pinned build digest of the embedding model |
| `<judge-model>` | The hosted model used as judge and first-choice reranker |
| `<digest-model>` | The model that writes session digests |
| `<maintenance-role>` | The database role that runs retention and migrations |
| `<operators-channel>` | Where the daily failure report is sent |

## 2. Where it runs

| Part | Where |
|---|---|
| Hermes, its plugins, the guards and the memory provider | Every machine in `<agent-hosts>` |
| Embedding (`qwen3-embedding:4b`), action naming (`qwen2.5:14b`) and the fallback reranker (`bge-reranker-v2-m3`) | Locally, on every machine in `<agent-hosts>` |
| Judge and first-choice reranker | `<judge-model>`, hosted |
| Session digests | `<digest-model>`, hosted |
| The memory store | One Supabase project, `<project-ref>`, shared by every machine in `<agent-hosts>` |
| Scheduled jobs | Hermes cron on `<job-host>`. A machine that sleeps cannot be the only job host: a job that does not run is reported as `job.missed`, but nothing runs it. |

The memory store outlives any one machine and is shared by all of them. What each hosted part receives is listed in ARCHITECTURE.md section 3.7; each deployment confirms that list against its own `<judge-model>` and `<digest-model>`.

## 3. Machine setup

Every machine that runs the agent:

1. **Runs the pinned embedding build.** Its Ollama model digest must equal `<model-digest>`. At startup the provider compares its digest with the one recorded in the store and refuses to run recall if they differ (`embed.model_mismatch`). Upgrading the model is a migration: every vector is re-embedded and the recorded digest changes.
2. **Has its own database role.** The role can read, insert and update the memory tables, and cannot delete rows or change the schema (ARCHITECTURE.md section 4.3). Its credentials live in the profile's `.env`.
3. **Sets its `<machine-name>`**, used in the `source` column of every row it writes. Names are unique across `<agent-hosts>`.
4. **Uses these Hermes settings:**

   | Setting | Value | Why |
   |---|---|---|
   | `memory.write_approval` | `true` | Every write to `MEMORY.md` and `USER.md` waits for a person's approval, so a wrong assumption is never saved unchecked. |
   | `logging.max_size_mb`, `logging.backup_count` | Defaults (5, 3) or larger | Long enough to cover the period between daily reports |
   | `HERMES_PLUGINS_DEBUG` | Never set | Its console copy of plugin logs is not redacted |
   | `security.redact_secrets` | Left on (default) | Turning it off disables every redaction pattern, including the build's own |

## 4. Scheduled jobs

| Job | Runs | What it does | Its own failure |
|---|---|---|---|
| Event retention | Daily | Deletes `events` rows older than 90 days. Misses worth keeping are copied into the eval set before then (ARCHITECTURE.md section 3.8). Runs as `<maintenance-role>`, not a machine's agent role. | `job.failed` |
| Job-health check | Daily, before the report | Flags any job with no fresh success (`job.missed`) and any job that took input but produced nothing for more than one run (`job.no_output`) | Reported by the report's absence |
| Daily failure report | Daily, at a fixed time | Sends the day's failures from `events`, grouped by code and machine, as a dated Markdown file to `<operators-channel>`. With nothing to report it sends a one-line all-clear that names the jobs that ran. | A missing report |

**A missing report is an alarm.** The report always sends something, so silence never means "all clear". If no report arrives by its expected time, the job host, the report job or the channel has failed, and an operator investigates.

The report runs as a Hermes script-only cron job (`--no-agent`): it uses no model, and Hermes delivers the script's output to the configured target. A script that exits non-zero or times out makes Hermes deliver an error alert instead.[1]

## 5. Upgrading Hermes

Before upgrading:

1. Read the release notes for changes to hooks, the memory-provider contract, logging and redaction.
2. Re-check every Hermes fact that ARCHITECTURE.md and this document state for the current version, and update them in the same pull request as the upgrade.
3. Run the full test suite and the eval set on the new version before switching machines over.

## 6. Third-party plugins

None are installed. A third-party plugin runs inside Hermes with the same access as the agent, so any plugin added later is listed here with its pinned commit, what it can access, what it sends off the machine, and how it behaves when it fails.

## Sources

- [1] [Scheduled tasks: no-agent mode (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
