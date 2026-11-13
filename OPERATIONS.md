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
| `<judge-model>` | The hosted model used as judge, as reranker and to route each message. It answers typed questions (a pick from a list, a yes or no) with probabilities and does not need to generate text. It is set by version, never by an alias that can move to a newer model, because the thresholds depend on its answers. |
| `<extraction-model>` | The model that proposes facts from user messages and tool results |
| `<digest-model>` | The model that writes session digests |
| `<maintenance-role>` | The login role the retention job runs as. It is created like a machine's role (section 3), but joins the group role `memory_maintenance`: `create role <maintenance-role> login in role memory_maintenance;`, then `\password <maintenance-role>`. |
| `<operators-channel>` | Where the daily failure report is sent |

## 2. Where it runs

| Part | Where |
|---|---|
| Hermes, its plugins, the guards and the memory provider | Every machine in `<agent-hosts>` |
| Embedding (`qwen3-embedding:4b`) | Locally, on every machine in `<agent-hosts>`. It is the only model an agent host must run, and it meets the recall deadline without a GPU: about 0.3 seconds for a query on a CPU-only host (#15). |
| Bulk embedding, such as the eval set or an import | Any machine set up as in section 3, preferably one with a GPU: a CPU-only host took about 3.6 seconds for each chunk (#15). Every machine runs the same pinned build, so its vectors are comparable with any other machine's (ARCHITECTURE.md section 3.2). |
| Judge, reranker and message routing | `<judge-model>`, hosted. The first deployment uses TypeSafe's Jev 1.14.0 (`jev-1.14.0`), pinned. |
| Fact extraction | `<extraction-model>`, local or hosted. The first deployment uses Claude Haiku 5.5 (`claude-haiku-5-5`), hosted, through the Anthropic API. |
| Session digests | `<digest-model>`, hosted. The first deployment uses the same model as fact extraction. |
| The memory store | One Supabase project, `<project-ref>`, shared by every machine in `<agent-hosts>` |
| Scheduled jobs | Hermes cron on `<job-host>`, and the heartbeat job on every machine in `<agent-hosts>`. A machine that sleeps cannot be the only job host: a job that does not run is reported as `job.missed`, but nothing runs it. |

The memory store outlives any one machine and is shared by all of them. What each hosted part receives is listed in ARCHITECTURE.md section 3.7; each deployment confirms that list against its own `<judge-model>`, `<extraction-model>` and `<digest-model>`.

## 3. Machine setup

Every machine that runs the agent:

1. **Runs the pinned embedding build.** Its Ollama model digest must equal `<model-digest>`. At startup the provider compares its digest with the one recorded in the store and refuses to run recall if they differ (`embed.model_mismatch`). Upgrading the model is a migration: every vector is re-embedded and the recorded digest changes.
2. **Has its own database role.** The role can read, insert and update the memory tables, and cannot delete rows or change the schema (ARCHITECTURE.md section 4.3). It is a login role that joins the group role `memory_agent`, which the migrations create and grant; it carries no privileges of its own. A maintainer creates it once, outside the repository, with a generated password: `create role <machine-name> login in role memory_agent;`, then `\password <machine-name>` in psql, which hashes the password on the maintainer's machine (SCRAM, with the server's default `password_encryption`) and sends only the hash, so the password itself never appears in a statement or a log.[5] Its credentials live in the profile's `.env`. The build reads them through Hermes's secret helper (`get_secret`) and never opens `.env` itself.[2]
3. **Sets its `<machine-name>`**, used in the `source` column of every row it writes. Names are unique across `<agent-hosts>`.
4. **Uses these Hermes settings:**

   | Setting | Value | Why |
   |---|---|---|
   | `memory.provider` | `custom_build` | Hermes runs one external memory provider, and this setting selects it.[2] The startup check fails otherwise. |
   | `memory.write_approval` | `true` | Every write to `MEMORY.md` and `USER.md` waits for a person's approval, so a wrong assumption is never saved unchecked. |
   | `compression.in_place` | `true` (default) | With `false`, compressing a transcript gives the conversation a new session id, and rows written before and after it no longer match (ARCHITECTURE.md section 4.1).[3] The startup check fails otherwise. |
   | `logging.max_size_mb`, `logging.backup_count` | Defaults (5, 3) or larger | Long enough to cover the period between daily reports |
   | `HERMES_PLUGINS_DEBUG` | Never set | Its console copy of plugin logs is not redacted |
   | `HERMES_SAFE_MODE`, or the `--safe-mode` flag that sets it | Never used | It makes Hermes skip plugin discovery, so the ordinary load never happens and the guards' hooks are not registered at all (ARCHITECTURE.md section 1).[3] |
   | `security.redact_secrets` | Left on (default) | Turning it off disables every redaction pattern, including the build's own |
   | `plugins.isolation` | `in_process` (default) | The build's layout is supported only in this mode; `host` runs user plugin code in a separate process (ARCHITECTURE.md section 1).[3] The startup check fails otherwise. |
   | `approvals.mode` | Never `off` | With it off, Hermes's approval gate approves every request without asking, the write gate's included (ARCHITECTURE.md section 5.6).[3] The startup check fails otherwise. |
   | `approvals.cron_mode`, `approvals.single_query_mode`, `approvals.unattended_mode` | `deny` (default) | Nobody can answer in these contexts. Hermes treats several spellings besides `approve` as approve, which lets the write gate's requests through unasked.[3] The startup check fails unless Hermes's own reader reports `deny`. |
5. **Never runs the agent with yolo on, by any route.** The `--yolo` flag, the `HERMES_YOLO_MODE` variable, the `/yolo` command in a CLI or gateway session, and a one-shot run started with `hermes -z` all make Hermes's approval gate approve without asking.[3] The startup check fails when it sees `HERMES_YOLO_MODE` set, and the write gate blocks outright when Hermes reports yolo for a session, however it was turned on (ARCHITECTURE.md section 5.3).
6. **Has the build installed as one Hermes plugin**, which carries the memory provider, the guards and the skills, from its directory in this repository (ARCHITECTURE.md section 1), with Hermes's own commands. Placeholders: `<owner>/<repo>` is this repository on GitHub, and `<commit>` is the full hash of a commit on `main`. The plugin, its directory and its manifest are all named `custom_build`.
   - **Install:** `hermes plugins install <owner>/<repo>#custom_build --ref <commit> --enable --yes-deps`. Hermes copies only that directory into `plugins/custom_build` in the Hermes home, which `hermes update` leaves in place.[2] For a plugin that is also a memory provider, `--enable` selects it as `memory.provider` and does not add it to `plugins.enabled`.[3]
   - **Enable:** `hermes plugins enable custom_build`, which adds it to `plugins.enabled`. Without this step the ordinary load never happens, so the guards' hooks are not registered at all (ARCHITECTURE.md section 1), and the startup check fails.[3]
   - **Update:** install again with `--force --ref <commit> --yes-deps`, then restart Hermes, because a running Hermes keeps the environment it started with. An install pinned with `--ref` refuses `hermes plugins update`, so a host only changes when a new commit is named. `--force` also accepts a caution from Hermes's install scan without asking and without showing what the scan found.[3] This procedure has been run only against a throwaway plugin that declares no packages (#29).
   - **Python packages** are declared in the plugin's manifest. Hermes resolves them when the plugin is selected or enabled, and `--yes-deps` gives the consent it otherwise asks for; this has not yet been run with real packages. Each one comes from a package index: Hermes drops a requirement given as a direct URL without an error.[3] Packages installed by hand with pip are not durable, and a later replacement of Hermes's environment need not keep them.[2]
   - **Private repository:** when an anonymous clone is refused, Hermes tries `GITHUB_TOKEN` or `GH_TOKEN`, then `gh auth token`, then the git credential helper.[3] This has not yet been run against a private repository.
7. **Starts the Hermes gateway through a service that runs the build's startup check first** (ARCHITECTURE.md section 5.5). When the check fails, the service does not start Hermes. A session started by hand skips the check. The check is a script in the plugin's directory and runs through the job wrapper, so a crash in it is recorded as a scheduled job's is, and a failed check records its own code (ARCHITECTURE.md section 5.5). How the service finds the Python that Hermes runs on is open (#29).

## 4. Scheduled jobs

| Job | Runs | What it does | Its own failure |
|---|---|---|---|
| Retention | Daily | Deletes `events` rows and session chunks older than 90 days. Misses worth keeping are copied into the eval set before then (ARCHITECTURE.md section 3.8). Runs as `<maintenance-role>`, not a machine's agent role. | `job.failed` |
| Heartbeat | Daily, on every agent host | Writes one `host.heartbeat` event | `job.failed`; a heartbeat that never runs is caught by the job-health check |
| Job-health check | Daily, before the report | Flags any job with no fresh success (`job.missed`) and any job that took input but produced nothing for more than one run (`job.no_output`), and records `host.missing` for any machine in `<agent-hosts>` with no heartbeat in the last 24 hours (ARCHITECTURE.md section 6.3). A host's missing heartbeat is reported as `host.missing` only, not also as `job.missed`. | Reported by the report's absence |
| Daily failure report | Daily, at a fixed time | Sends the day's failures from `events`, grouped by code and machine, and every "always" answer given to the write gate that day, as a dated Markdown file to `<operators-channel>`. With nothing to report it sends a one-line all-clear that names the jobs that ran. | A missing report |

Every job runs through the job wrapper (ARCHITECTURE.md section 6.1), so a crash is recorded in `events` as `unhandled` and `job.failed` before the job exits non-zero.

**A missing report is an alarm.** The report always sends something, so silence never means "all clear". If no report arrives by its expected time, the job host, the report job or the channel has failed, and an operator investigates.

The report runs as a Hermes script-only cron job (`--no-agent`): it uses no model, and Hermes delivers the script's output to the configured target. A script that exits non-zero or times out makes Hermes deliver an error alert instead.[1]

## 5. Upgrading Hermes

Before upgrading:

1. Read the release notes for changes to hooks, plugin loading, the memory-provider contract, logging and redaction.
2. Re-check every Hermes fact that ARCHITECTURE.md and this document state for the current version, and update them in the same pull request as the upgrade.
3. Run the full test suite and the eval set on the new version before switching machines over. That includes the parts of the build that rest on Hermes behaviour the developer guide does not describe (ARCHITECTURE.md section 5.6).

## 6. Plugins from outside this repository

**Third-party plugins.** None are installed. A third-party plugin runs inside Hermes with the same access as the agent, so any plugin added later is listed here with its pinned commit, what it can access, what it sends off the machine, and how it behaves when it fails.

**Plugins bundled with Hermes.** These ship with Hermes and are maintained by Nous. They are not part of the build. Bundled platforms and backends load without `hermes plugins enable` and work once they are configured; a standalone bundled plugin such as `security-guidance` has to be enabled.[3]

| Bundled plugin | Used | Why |
|---|---|---|
| The messaging platform that carries `<operators-channel>` | Yes, configured | The daily failure report is delivered through it (section 4) |
| `security-guidance` | Yes, enabled in its default warn mode | It adds a warning to the result of a file write whose content matches a known-dangerous code pattern. It runs on the machine and sends nothing off it.[3] It only warns, so it is not one of the build's guards (ARCHITECTURE.md section 5). Its block mode stays off, because its patterns have false positives. |
| A web search or page-fetch backend | Each deployment's choice | The build does not ship one. A deployment that configures one adds what that backend receives to its own list of what leaves the host (ARCHITECTURE.md section 3.7). |
| Any other memory provider | Never | Hermes runs one external memory provider, and it is this build's |

## 7. Applying a migration

The files in `migrations/` are applied to `<project-ref>` in file-name order, each once, after its pull request merges. A maintainer applies each one, or approves applying it, through the Supabase Management API's `POST /v1/projects/<project-ref>/database/migrations`, with the file's text as the query and its file name as the name. Supabase runs it and records it in the project's migration history, `supabase_migrations.schema_migrations`.[4] Read the history back afterwards: the newest entry is the file just applied. Then read back which role can grant each group role (`pg_auth_members`), because that role is the one that creates the login roles in section 3. The tests apply every file to an empty Postgres 17 as a superuser. That catches a file that is wrong in itself, but not one that fails only under Supabase's own roles and extensions; the first apply to the project shows that.

## Sources

- [1] [Scheduled tasks: no-agent mode (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [2] [Build a Hermes plugin (Hermes Agent developer guide)](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [3] Hermes v0.23.1 source: `plugins/security-guidance/`, `tools/approval.py`, `tools/approval_context.py`, `gateway/session_finite.py`, `hermes_cli/cli_session_mixin.py`, `hermes_cli/config_defaults.py`, `hermes_cli/git_credentials.py`, `hermes_cli/main.py`, `hermes_cli/plugin_isolation.py`, `hermes_cli/plugins.py`, `hermes_cli/plugins_cmd.py`, `hermes_cli/plugins_cmd_install.py`, `hermes_cli/plugins_cmd_update.py`, `hermes_cli/plugins_discovery.py`, `hermes_cli/plugins_ledger.py`, `hermes_cli/plugins_loader.py`, `plugins/memory/__init__.py`, `pm/plugin_declarations.py`
- [4] [Supabase for Platforms: make database changes (Supabase documentation)](https://supabase.com/docs/guides/integrations/supabase-for-platforms#make-database-changes)
- [5] [psql: `\password` (PostgreSQL 17 documentation)](https://www.postgresql.org/docs/17/app-psql.html)
