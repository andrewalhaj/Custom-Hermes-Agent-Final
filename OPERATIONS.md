# Operations

This document describes where the build runs and what must be configured for it to run correctly. It holds no real hostnames, project references, chat IDs or credentials: those are deployment values, set outside the repository. The design is in [ARCHITECTURE.md](ARCHITECTURE.md); the rules for changing anything are in [CONTRIBUTING.md](CONTRIBUTING.md).

Facts about Hermes are stated for **Hermes v0.23.1**. Re-check them before upgrading Hermes.

## 1. Deployment values

These placeholders appear in the documentation. Each deployment sets them in its Hermes profile's `.env` or its own configuration management, never in this repository.

| Placeholder | What it is |
|---|---|
| `<agent-hosts>` | The machines that run Hermes with this build. There can be several. |
| `<machine-name>` | Each agent host's name, written to the `source` column of every row it writes. Set as `plugins.entries.custom_build.settings.machine_name` in the profile's `config.yaml`. |
| `<job-host>` | One of the agent hosts that is on when scheduled jobs run. The first deployment has one agent host, which is also the job host. |
| `<project-ref>` | The Supabase project that holds the memory store |
| `<model-digest>` | The pinned build digest of the embedding model |
| `<judge-model>` | The hosted model used as judge, as reranker and to route each message. It answers typed questions (a pick from a list, a yes or no) with probabilities and does not need to generate text. It is set by version, never by an alias that can move to a newer model, because the thresholds depend on its answers. Set as `plugins.entries.custom_build.settings.judge_model` in the profile's `config.yaml`, with its key in the profile's `.env` as `TYPESAFE_API_KEY`. |
| `<extraction-model>` | The model that proposes facts from user messages and tool results |
| `<digest-model>` | The model that writes session digests |
| `<operators-channel>` | Where the daily failure report and the scheduled jobs' error alerts are sent, as a Hermes cron delivery target (`--deliver`), such as `telegram:<chat-id>`. The first deployment uses a Telegram channel. |

## 2. Where it runs

| Part | Where |
|---|---|
| Hermes, its plugins, the guards and the memory provider | Every machine in `<agent-hosts>` |
| Embedding (`qwen3-embedding:4b`) | Locally, on every machine in `<agent-hosts>`. It is the only model an agent host must run, and it meets the recall deadline without a GPU: about 0.3 seconds for a query on a CPU-only host (#15). |
| Bulk embedding, such as the eval set or an import | Any machine set up as in section 3, preferably one with a GPU: a CPU-only host took about 3.6 seconds for each chunk (#15). The eval set runs on a maintainer's machine with a GPU, against its own throwaway Postgres, never the shared store. Every machine runs the same pinned build, so its vectors are comparable with any other machine's (ARCHITECTURE.md section 3.2). |
| Judge, reranker and message routing | `<judge-model>`, hosted. The first deployment uses TypeSafe's Jev 1.14.0 (`jev-1.14.0`), pinned. |
| Fact extraction | `<extraction-model>`, local or hosted. The first deployment uses Claude Haiku (the `haiku` alias), hosted, through the Claude subscription: the build's auxiliary task `custom_build_extraction` defaults to the provider `claude-subscription-directsdk-experimental` (section 6), which drives the Claude Code CLI logged in on the host. A profile can route it elsewhere with its own `auxiliary.custom_build_extraction` block. Anthropic's terms say subscription sign-in is for ordinary use of its own applications; the maintainer accepted that risk on #148. `extract.refused` in the daily report is the sign that Anthropic refused the account. |
| Session digests | `<digest-model>`, hosted. The first deployment uses the same model as fact extraction, through the build's auxiliary task `custom_build_digest`, which inherits `custom_build_extraction`'s route, so one `auxiliary.custom_build_extraction` override moves both; an `auxiliary.custom_build_digest` block moves the digest alone. `digest.refused` in the daily report is the same sign as `extract.refused` (#155). |
| The memory store | One Supabase project, `<project-ref>`, shared by every machine in `<agent-hosts>` |
| Scheduled jobs | Hermes cron on `<job-host>`, the heartbeat job on every machine in `<agent-hosts>`, and retention inside the database, run by Supabase's `pg_cron` (section 4). A machine that sleeps cannot be the only job host: a job that does not run is reported as `job.missed`, but nothing runs it. |

The memory store outlives any one machine and is shared by all of them. What each hosted part receives is listed in ARCHITECTURE.md section 3.7; each deployment confirms that list against its own `<judge-model>`, `<extraction-model>` and `<digest-model>`.

## 3. Machine setup

Every machine that runs the agent:

1. **Runs the pinned embedding build.** Its Ollama model digest must equal `<model-digest>`. Ollama listens on its default address, `127.0.0.1:11434`, where the provider reaches it directly, never through a proxy the environment names; an Ollama anywhere else shows only as `embed.unavailable`. At startup the provider compares its digest with the one recorded in the store and refuses to run recall if they differ (`embed.model_mismatch`). Upgrading the model is a migration: every vector is re-embedded and the recorded digest changes. It is not a file in `migrations/`, and rule C10 (CONTRIBUTING.md section 5.1) does not cover it: every host moves to the new build with it.
2. **Has its own database role.** The role can read and insert rows in the memory tables, and update those that take updates, and cannot delete rows or change the schema (ARCHITECTURE.md section 4.3). It is a login role that joins the group role `memory_agent`, which the migrations create and grant; it carries no privileges of its own. A maintainer creates it once, outside the repository, with a generated password: `create role <machine-name> login in role memory_agent;`, then `\password <machine-name>` in psql, which hashes the password on the maintainer's machine (SCRAM, with the server's default `password_encryption`) and sends only the hash, so the password itself never appears in a statement or a log.[5][6] Its connection string lives in the profile's `.env` as `CUSTOM_BUILD_DATABASE_URL`. The build reads them through Hermes's secret helper (`get_secret`) and never opens `.env` itself.[2]
3. **Sets its `<machine-name>`** in `config.yaml`, at `plugins.entries.custom_build.settings.machine_name`, used in the `source` column of every row it writes. Names are unique across `<agent-hosts>`. It also sets the judge's model, at `plugins.entries.custom_build.settings.judge_model` (`<judge-model>`), and keeps the judge's key in the profile's `.env` as `TYPESAFE_API_KEY`, read like the connection string. Without any of these, or without `CUSTOM_BUILD_DATABASE_URL`, the startup check fails with `config.missing`, and inside Hermes every turn is blocked. For extraction through the Claude subscription (section 2), the host also has the `claude-subscription-directsdk` plugin installed (section 6) and the Claude Code CLI, 2.1.293 or newer for Haiku 5.5, logged in as the user Hermes runs as; without them each turn's extraction is recorded as `extract.error`, or `extract.refused` when the CLI is logged out.
4. **Uses these Hermes settings:**

   | Setting | Value | Why |
   |---|---|---|
   | `memory.provider` | `custom_build` | Hermes runs one external memory provider, and this setting selects it.[2] The startup check fails otherwise. |
   | `skills.write_approval` | `true` | Every change Hermes's skill-writing tool makes, in a turn or in its background review, is staged until a person approves it with `/skills approve` (ARCHITECTURE.md section 5.3, #42). It holds that tool only; other tools that write files can still change a skill's files, which the write gate covers (#80). Unset means off; the startup check fails unless it reads as on (#81).[3] |
   | `memory.write_approval` | `true` | Every write to `MEMORY.md` and `USER.md` waits for a person's approval, so a wrong assumption is never saved unchecked. The startup check fails unless it reads as on (#81). |
   | `compression.in_place` | `true` (default) | With `false`, compressing a transcript gives the conversation a new session id, and rows written before and after it no longer match (ARCHITECTURE.md section 4.1).[3] The startup check fails otherwise. |
   | `logging.max_size_mb`, `logging.backup_count` | Defaults (5, 3) or larger | Long enough to cover the period between daily reports |
   | `HERMES_PLUGINS_DEBUG` | Never set | Its console copy of plugin logs is not redacted |
   | `HERMES_SAFE_MODE`, or the `--safe-mode` flag that sets it | Never used | It makes Hermes skip plugin discovery, so the ordinary load never happens and the guards' hooks are not registered at all (ARCHITECTURE.md section 1).[3] |
   | `security.redact_secrets` | Left on (default) | Turning it off disables Hermes's redaction except in calls that pass `force=True`. The build's own redaction passes it, so it stays on either way. The startup check fails when the key is off, or when `HERMES_REDACT_SECRETS` turns redaction off; the gateway copies the key over that variable for the processes it starts (#81).[3] |
   | `gateway.auto_multiplex_migration` | `false` | A gateway that started with one profile then keeps serving only that profile when another appears, and the next restart runs the startup check, which refuses the second profile (item 9). This holds only while `gateway.multiplex_profiles` does not read as true. With it true, by any route the gateway reads (`config.yaml` or its managed overlay, the legacy `gateway.json`, or `GATEWAY_MULTIPLEX_PROFILES`), the gateway serves every profile from start and never reads this setting. Hermes writes it into `config.yaml` itself when its gateway turns on serving every profile on its own, and when `hermes gateway migrate --multiplex` runs.[3] The startup check fails unless this setting reads as false and that one does not read as true (#85). |
   | `plugins.isolation` | `in_process` (default) | The build's layout is supported only in this mode; `host` runs user plugin code in a separate process (ARCHITECTURE.md section 1).[3] Under `host`, Hermes registers no `hermes custom-build` command, so the startup check cannot run and the service does not start Hermes. |
   | `approvals.mode` | Never `off` | With it off, Hermes's approval gate approves every request without asking, the write gate's included (ARCHITECTURE.md section 5.6).[3] The startup check fails otherwise. |
   | `plugins.hook_callback_timeout` | 30 (default) or more | Hermes skips a `pre_llm_call` hook that runs past it and lets the turn go ahead, so a lower value can let a guard's turn through (ARCHITECTURE.md section 5.5).[3] The startup check fails otherwise (#69). |
   | `approvals.cron_mode`, `approvals.single_query_mode`, `approvals.unattended_mode` | `deny` (default) | Nobody can answer in these contexts. Hermes treats several spellings besides `approve` as approve, which lets the write gate's requests through unasked.[3] The startup check fails unless Hermes's own reader reports `deny`. |
5. **Never runs the agent with yolo on, by any route.** The `--yolo` flag, the `HERMES_YOLO_MODE` variable, the `/yolo` command in a CLI or gateway session, and a one-shot run started with `hermes -z` all make Hermes's approval gate approve without asking.[3] The startup check fails when it sees `HERMES_YOLO_MODE` set, and the write gate blocks outright when Hermes reports yolo for a session, however it was turned on (ARCHITECTURE.md section 5.3).
6. **Has the build installed as one Hermes plugin**, which carries the memory provider, the guards and the skills, from its directory in this repository (ARCHITECTURE.md section 1), with Hermes's own commands. Placeholders: `<owner>/<repo>` is this repository on GitHub, and `<commit>` is the full hash of a commit on `main`. The plugin, its directory and its manifest are all named `custom_build`.
   - **Install:** `hermes plugins install <owner>/<repo>#custom_build --ref <commit> --enable --yes-deps`. Hermes copies only that directory into `plugins/custom_build` in the Hermes home, which `hermes update` leaves in place.[2] For a plugin that is also a memory provider, `--enable` selects it as `memory.provider` and does not add it to `plugins.enabled`.[3] When Hermes's install scan returns a caution, the install prints the findings and asks before going on; with no terminal to answer, it refuses. Run it in a terminal to answer; `--force` accepts a caution without showing them (Update, below).[3]
   - **Enable:** `hermes plugins enable custom_build`, which adds it to `plugins.enabled`. Without this step the ordinary load never happens, so the guards' hooks are not registered at all (ARCHITECTURE.md section 1), and the startup check fails.[3]
   - **Update:** install again with `--force --ref <commit> --yes-deps`, then restart Hermes, because a running Hermes keeps the environment it started with. Where the gateway runs as the service (item 7), the restart is `systemctl restart hermes-gateway`, which runs the startup check on the new code first; the old gateway has already stopped, so a failed check leaves the host offline, retrying about every 5 minutes, until it is fixed or the previous commit is installed again. Run `hermes custom-build check` by hand before the restart. An install pinned with `--ref` refuses `hermes plugins update`, so a host only changes when a new commit is named. `--force` also accepts a caution from Hermes's install scan without asking and without showing what the scan found.[3] The first agent host was updated this way, with the plugin's packages already installed: the selection and settings were kept, and the service's restart ran the check (#61). An update whose packages change has not been run (#73).
   - **Python packages** are declared in the plugin's manifest. Hermes resolves them when the plugin is selected or enabled, and `--yes-deps` gives the consent it otherwise asks for. The first install on an agent host resolved and installed the plugin's packages this way, with no prompt (#29). Each one comes from a package index: Hermes drops a requirement given as a direct URL without an error.[3] Packages installed by hand with pip are not durable, and a later replacement of Hermes's environment need not keep them.[2]
   - **The repository is public,** so Hermes clones it anonymously, as the first install and update on an agent host did (#29, #61); no GitHub token is set on an agent host.
7. **Starts the Hermes gateway through a service that runs the build's startup check first** (ARCHITECTURE.md section 5.5). When the check fails, the service does not start Hermes. A session started by hand skips the check. The check is `hermes custom-build check`, run through the same command Hermes's own unit uses to start the gateway, so it runs on Hermes's interpreter and packages. It runs through the job wrapper, so a crash in it is recorded as a scheduled job's is, and a failed check records its own code (ARCHITECTURE.md section 5.5). The check also fails with `db.behind` when the store is behind the migrations the plugin was built against, so a plugin updated before its migrations are applied leaves the host offline until they are: apply them first (section 7).
   - **The service:** Hermes's own unit, `hermes-gateway.service` for the default profile, enabled at boot. It is set up in this order, with no Hermes client open:
     1. `hermes gateway stop`, which stops a gateway a client started (`gateway run --idle-exit`). Once the unit exists, the same command stops the unit and leaves such a gateway running.[3] The gateway is offline from here until step 4.
     2. `hermes gateway install --system --run-as-user <service-user> --no-start-now --start-on-login`, where `<service-user>` is the account whose home holds both the Hermes home and the Hermes install: Hermes rewrites the unit's paths into that account's home. Without `--run-as-user`, Hermes takes the account from the environment and refuses when that is `root`, before it writes the unit; with `--run-as-user root` it installs and warns that root is not recommended on a bare-metal host.[3] The first agent host runs it as `root`, where Hermes is installed (#60).
     3. The drop-in below, then `systemctl daemon-reload`.
     4. `systemctl start hermes-gateway`.

     With the drop-in in place, a Hermes client no longer starts a gateway itself: it refuses a unit that has an `ExecStartPre=` line, reports the gateway inaccessible and starts nothing. Only systemd starts the gateway, and always through the check.[3]
   - **The drop-in**, `/etc/systemd/system/hermes-gateway.service.d/10-custom-build-check.conf`, beside Hermes's unit, so it survives Hermes regenerating the unit:

     ```ini
     [Service]
     TimeoutStartSec=10min
     ExecStartPre=/bin/sh -c '"$$@" || { sleep 300; exit 1; }' custom-build-check <the command from ExecStart=, ending in "custom-build" "check" in place of "gateway" "run">
     ```

     The command is copied from the unit's `ExecStart=` because it differs between kinds of install, and it is copied again after every Hermes update that rewrites the unit; a stale copy fails closed. It is passed to the shell as arguments, not inside the shell's quotes, so systemd parses it exactly as it parses `ExecStart=`; `$$` is systemd's escape for a literal `$`.[7]
   - **Why the check waits before it fails:** Hermes's unit has `Restart=always`, `RestartSec=5` and no start limit, and a failing `ExecStartPre=` triggers that restart; `RestartPreventExitStatus=` does not apply to it.[7] Without the wait, a host whose check keeps failing would run it, and write a row to `events`, every 5 seconds. With it, the check is retried about every 5 minutes, and a host recovers by itself once a temporary cause, such as an unreachable store, goes away. A passing check under the service has been run on the first agent host (#29); a failing one has not, since it takes the gateway offline. `TimeoutStartSec=` is raised above the wait because systemd times `ExecStartPre=` against it.[8] systemd's own backoff (`RestartSteps=`) is not used: its documentation does not say when the delay resets, so it could slow every later restart of a healthy gateway.[7]
8. **Runs Linux with systemd.** The service runs the startup check before the gateway through a systemd drop-in (item 7). On macOS Hermes runs the gateway under launchd, which has nothing like `ExecStartPre=`, so a Mac is not an agent host (#62).[3]
9. **Runs one Hermes profile.** With two or more profiles on a host, Hermes by default runs one gateway that serves them all, while the startup check reads one profile's settings, so an agent host has one profile, and the startup check fails with `config.unsafe` when it has more (#63).[3]

## 4. Scheduled jobs

| Job | Runs | What it does | Its own failure |
|---|---|---|---|
| Retention | Daily at 03:30 GMT | Deletes `events` rows older than 90 days, except `guard.bypassed` rows; session chunks too, once their table exists. Misses worth keeping are copied into the eval set before then (ARCHITECTURE.md section 3.8). Runs inside the database: a function a migration adds, scheduled by Supabase's `pg_cron`, so no host holds a login that can delete (ARCHITECTURE.md section 4.3). `pg_cron` 1.6.4 is available on the project but not yet installed (#92). | A run records `job.succeeded` naming `retention`; a missing one is `job.missed`. A failed run rolls back, and its error is in `pg_cron`'s run history |
| Heartbeat | Every 6 hours, on every agent host (#43) | Writes one `host.heartbeat` event, and records `guard.bypassed`, once, for each local session started in safe mode or with the profile's settings ignored, which ran with no part of the build, or whose launch policy it cannot read (ARCHITECTURE.md section 5.6, #23) | Exits non-zero, so Hermes sends its error alert, which names the code: `config.missing`, `db.auth` or `db.unreachable`, `events.write_failed`, or `unhandled` and `job.failed`. A heartbeat that never runs is caught by the job-health check |
| Job-health check | Daily, before the report | Flags any job with no fresh success (`job.missed`) and any job that took input but produced nothing for more than one run (`job.no_output`), and records `host.missing` for any machine in `<agent-hosts>` with no heartbeat in the last 24 hours (ARCHITECTURE.md section 6.3). A host's missing heartbeat is reported as `host.missing` only, not also as `job.missed`. A job counts as having run when it records `job.succeeded`, naming itself; the check records one too. Its list of jobs holds the daily report and retention (#134, #138, #95). | A failed run exits non-zero, so Hermes sends its error alert, which names the code, as the heartbeat's does. A day it does not run at all records no `job.succeeded` for it, for the report to notice |
| Daily failure report | Daily at 07:00, the job host's local time | Sends the last 24 hours' failures from `events`, grouped by code and machine, the sessions recorded as `guard.bypassed` among them, with each code's severity and whether it is shown in the turn, as a dated Markdown file to `<operators-channel>`: it writes a file named `doc_<id>_failures-<date>.md` to Hermes's document cache and prints a `MEDIA:` line, which Hermes sends as an attachment. With nothing to report it sends a one-line all-clear that names the jobs that ran; Hermes wraps either in its own header and footer unless `cron.wrap_response` is `false`. A day the job-health check did not run is recorded as `job.missed` for it, and reported. The write gate's "always" answers are not reported yet: no code records them while the gate runs in `log` mode (#140). | A missing report. A failed run exits non-zero, so Hermes sends its error alert, naming the code, instead of the report |

Every job other than retention runs through the job wrapper (ARCHITECTURE.md section 6.1), so a crash is recorded in `events` as `unhandled` and `job.failed` before the job exits non-zero.

**A missing report is an alarm.** The report always sends something, so silence never means "all clear". If no report arrives by its expected time, the job host, the report job or the channel has failed, and an operator investigates.

Every job other than retention is a `hermes custom-build <job>` subcommand, shipped and updated with the plugin. Hermes requires a script-only job to name a file in the Hermes home's `scripts/` folder, so each job has a wrapper there, a few lines of Python that call its subcommand and never change. The wrappers ship in the plugin's own `scripts/` folder and are copied into the Hermes home's once. Hermes runs a Python script on its own interpreter, whatever the kind of install, so a wrapper names no path to Hermes.[3]

**Setting up the heartbeat** on each agent host, in its Hermes home (#106):

1. Copy `plugins/custom_build/scripts/custom-build-heartbeat.py` into `scripts/`, creating that folder if it does not exist.
2. `hermes cron create "every 6h" --name custom-build-heartbeat --script custom-build-heartbeat.py --no-agent --deliver <operators-channel>`
3. `hermes cron run <job-id>`, with the id step 2 printed, runs it at once rather than 6 hours later and prints `Ran now: succeeded`; `events` then holds a `host.heartbeat` row with the host's name.

A run that succeeds prints nothing, so Hermes sends nothing; a failed run sends Hermes's error alert to `<operators-channel>`.[1] The alert carries the start of the line the job printed, which names the failure code; the whole line is in the run's record under `cron/output/` in the Hermes home.

**Setting up the job-health check** on the job host, in its Hermes home (#134):

1. Set `plugins.entries.custom_build.settings.agent_hosts` in `config.yaml` to the list of every `<machine-name>` in `<agent-hosts>`. Without it, or with an entry that is not a name, the check stops with `config.missing`.
2. Copy `plugins/custom_build/scripts/custom-build-job-health.py` into `scripts/`.
3. `hermes cron create "45 6 * * *" --name custom-build-job-health --script custom-build-job-health.py --no-agent --deliver <operators-channel>`, so it runs before the 07:00 report on the same clock.
4. `hermes cron run <job-id>` runs it once now; `events` then holds a `job.succeeded` row naming `job-health`.

**Setting up the report** on the job host, in its Hermes home (#138):

1. Copy `plugins/custom_build/scripts/custom-build-report.py` into `scripts/`.
2. `hermes cron create "0 7 * * *" --name custom-build-report --script custom-build-report.py --no-agent --deliver <operators-channel>`, on the same clock as the job-health check, 15 minutes after it.
3. `hermes cron run <job-id>` sends today's report, or the all-clear, now.

**Setting up retention** on `<project-ref>`, by a maintainer, once `0004_memory_events_retention.sql` is applied (section 7) (#95):

1. Confirm an export of `memory.events` exists: the first run deletes every row older than 90 days (CONTRIBUTING.md rule C7).
2. Turn on `pg_cron` from the Supabase dashboard, or with the SQL in its documentation.[10]
3. As the role that applied the migration (Supabase's `postgres`), the one the migration lets run the function, schedule it: `select cron.schedule('memory-retention', '30 3 * * *', $$select memory.retain_events()$$);`. `pg_cron` runs a job with the permissions of the role that scheduled it, and reads the schedule in GMT unless `cron.timezone` says otherwise, so this runs at 03:30 GMT.[9] Its run history is in `cron.job_run_details`, which nothing clears.[10]
4. Run `select memory.retain_events();` once by hand, as the same role; `events` then holds a `job.succeeded` row naming `retention`, from `database`.

The report runs as a Hermes script-only cron job (`--no-agent`): it uses no model, and Hermes delivers the script's output to the configured target. A script that exits non-zero or times out makes Hermes deliver an error alert instead.[1]

## 5. Upgrading Hermes

Before upgrading:

1. Read the release notes for changes to hooks, plugin loading, the memory-provider contract, logging and redaction.
2. Re-check every Hermes fact that ARCHITECTURE.md and this document state for the current version, and update them in the same pull request as the upgrade.
3. Export the new tag's dependency pins into `hermes-requirements.txt` and change the pinned tag and commit in the CI workflow (CONTRIBUTING.md 8.2).
4. Run the full test suite and the eval set on the new version before switching machines over. That includes the parts of the build that rest on Hermes behaviour the developer guide does not describe (ARCHITECTURE.md section 5.6).

The same applies to every other direct dependency: read its release notes in its official documentation, below, and re-check what the documents state about it. Versions are pinned in `pyproject.toml`, `custom_build/plugin.yaml`, `hermes-requirements.txt`, the CI workflow and section 2, not in this list. Hermes's own dependencies are Hermes's to track.

- Hermes Agent: [documentation](https://hermes-agent.nousresearch.com/docs), [releases](https://github.com/NousResearch/hermes-agent/releases)
- Python: [documentation](https://docs.python.org/3.14/)
- psycopg: [documentation](https://www.psycopg.org/psycopg3/docs/)
- PostgreSQL: [documentation](https://www.postgresql.org/docs/17/)
- pgvector: [README](https://github.com/pgvector/pgvector)
- Supabase: [documentation](https://supabase.com/docs)
- TypeSafe (Jev): [documentation](https://docs.typesafe.ai)
- Anthropic (Claude Haiku): [documentation](https://platform.claude.com/docs/en/home)
- Ollama and `qwen3-embedding`: [documentation](https://docs.ollama.com/), [model page](https://ollama.com/library/qwen3-embedding)
- GitHub Actions: [documentation](https://docs.github.com/en/actions), [`actions/checkout`](https://github.com/actions/checkout), [`actions/setup-python`](https://github.com/actions/setup-python)
- ruff: [documentation](https://docs.astral.sh/ruff/)
- pytest: [documentation](https://docs.pytest.org/)
- setuptools (the build backend in `pyproject.toml`): [documentation](https://setuptools.pypa.io/)
- PyYAML: [documentation](https://pyyaml.org/wiki/PyYAMLDocumentation)
- LongMemEval (eval data): [dataset card](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)

## 6. Plugins from outside this repository

**Third-party plugins.** A third-party plugin runs inside Hermes with the same access as the agent, so each is listed here with its pinned commit, what it can access, what it sends off the machine, and how it behaves when it fails.

| Plugin | Pin | Access | Sends off the machine | When it fails |
|---|---|---|---|---|
| `claude-subscription-directsdk` (Nous's official plugins, catalog tier `official`) | `NousResearch/hermes-official-plugins` at `6313fad`, as Hermes v0.23.1's catalog lists it | The agent's, plus the Claude Code CLI's login on the host | What the build's extraction task sends it (ARCHITECTURE.md section 3.7), to Anthropic, billed to the subscription's Agent SDK allowance | It raises with an HTTP-like status; the build records 401, 402 or 403 as `extract.refused` and anything else as `extract.error`, and loses only that turn's extracted facts |

**Plugins bundled with Hermes.** These ship with Hermes and are maintained by Nous. They are not part of the build. Bundled platforms and backends load without `hermes plugins enable` and work once they are configured; a standalone bundled plugin such as `security-guidance` has to be enabled.[3]

| Bundled plugin | Used | Why |
|---|---|---|
| The messaging platform that carries `<operators-channel>` | Yes, configured | The daily failure report and the scheduled jobs' error alerts are delivered through it (section 4) |
| `security-guidance` | Yes, enabled in its default warn mode | It adds a warning to the result of a file write whose content matches a known-dangerous code pattern. It runs on the machine and sends nothing off it.[3] It only warns, so it is not one of the build's guards (ARCHITECTURE.md section 5). Its block mode stays off, because its patterns have false positives. |
| A web search or page-fetch backend | Each deployment's choice | The build does not ship one. A deployment that configures one adds what that backend receives to its own list of what leaves the host (ARCHITECTURE.md section 3.7). |
| Any other memory provider | Never | Hermes runs one external memory provider, and it is this build's |

## 7. Applying a migration

The files in `migrations/` are applied to `<project-ref>` in file-name order, each once, after its pull request merges. A maintainer applies each one, or approves applying it, through the Supabase Management API's `POST /v1/projects/<project-ref>/database/migrations`, with the file's text as the query and its file name without `.sql` as the name, such as `0004_memory_events_retention`, which is how the first four are recorded (read from the store on 2026-11-16). Supabase runs it and records it in the project's migration history, `supabase_migrations.schema_migrations`.[4] Read the history back afterwards: the newest entry is the file just applied. Then read back which role can grant each group role (`pg_auth_members`), because that role is the one that creates the login roles in section 3. Apply a migration before installing or updating any host's plugin to a commit that needs it. Rule C10 (CONTRIBUTING.md section 5.1) keeps every migration working with the plugin deployed before it, the expand-and-contract pattern,[11] so hosts are updated afterwards, one at a time, and a plugin can be rolled back to its previous commit without touching the store. The tests apply every file to an empty Postgres 17 as a superuser. That catches a file that is wrong in itself, but not one that fails only under Supabase's own roles and extensions; the first apply to the project shows that.

## 8. Deploying to an agent host

`python scripts/deploy.py <commit>`, run from a checkout on the operator's machine, applies pending migrations the way section 7 describes and does section 3's Update step for one agent host, in a fixed order, and stops at the first step that fails. `<commit>` is the full hash of a commit on `main`.

1. **Refuses an unproven commit:** one that is not on `origin/main`, or whose CI runs did not all succeed. No run yet is a refusal.
2. **Reads before it changes anything:** the store's migration history, and the commit the host runs now, from the record Hermes keeps of a pinned install (`plugins/.install-metadata.json` in the Hermes home).[3] If either cannot be read, nothing has changed. A host with no pinned install is refused: the first install is done by hand (section 3).
3. **Applies the pending migrations,** in file-name order, only after it lists them and the operator types `yes`, and reads the history back after each. It sends each file's name without `.sql`, which is how the first four are recorded (read from the store on 2026-11-16), and matches a recorded name with or without that ending. It does not do section 7's read of `pg_auth_members`.
4. **Updates the host:** the Update command from section 3, then `hermes custom-build check` before any restart, then the restart, then a smoke test: the service is active and `hermes custom-build heartbeat` exits 0, so the new code loads and reaches the store.
5. **Rolls back** when step 4 fails: it installs the previous commit, checks, restarts and smoke-tests again, and says whether that passed. The one exception is an install that failed with a known result and left the previous commit in place, which Hermes's own record shows: nothing is restarted then. An install that timed out, or whose ssh connection failed, may still finish on the host, so it is rolled back; if it then finishes after the rollback, the host is on the new commit unchecked, so read the install record by hand after either case. A migration is never rolled back.

It exits 0 when deployed, 1 when it stopped with the host unchanged (its last line names the migrations it had already applied, which stay applied, and any it sent whose read-back failed, which the store may hold: read the history by hand then), 2 when it rolled back, 3 when the rollback itself failed, which leaves the host for the operator to check by hand, and 64 when it is not given exactly one commit. It runs outside Hermes, so it writes nothing to `events` itself; the heartbeat in its smoke test does.

The script reads these from the environment and refuses when one is missing. None of them belongs in this repository (section 1).

| Variable | What it is |
|---|---|
| `DEPLOY_REPO` | `<owner>/<repo>`, as in section 3's install command |
| `DEPLOY_SSH` | The ssh target for the account that owns the Hermes home on the agent host |
| `DEPLOY_HERMES` | The full path of the `hermes` command on the host, as in the unit's `ExecStart=` |
| `DEPLOY_HERMES_HOME` | The full path of the Hermes home on the host, not one that starts with `~` |
| `DEPLOY_UNIT` | The gateway's systemd unit |
| `DEPLOY_RESTART` | The whole command that restarts that unit, run on the operator's machine. Restarting a system unit needs root, so this is usually an ssh command with a login that may do it. It is run without a shell. |
| `SUPABASE_PROJECT_REF` | `<project-ref>` |
| `SUPABASE_ACCESS_TOKEN` | A Management API token for that project. The script never prints it. |

Under the service, a startup check that fails waits 5 minutes before the unit's start fails (section 3), so a restart onto a bad commit takes that long to report, and the rollback follows it. Stopping the script with Ctrl-C ends it without a rollback, and piping its output to a program that exits can end it with an exit code that is not one of those above: in either case check the host by hand. The script has been run against fakes only; it has not yet updated a host (#181). It does not register scheduled jobs (section 4), upgrade Hermes (section 5), or cover an update whose Python packages change (#73).

## Sources

- [1] [Scheduled tasks: no-agent mode (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [2] [Build a Hermes plugin (Hermes Agent developer guide)](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [3] Hermes v0.23.1 source: `plugins/security-guidance/`, `tools/approval.py`, `tools/approval_context.py`, `gateway/session_finite.py`, `hermes_cli/cli_session_mixin.py`, `hermes_cli/config_defaults.py`, `gateway/config.py`, `gateway/run.py`, `hermes_cli/gateway.py`, `hermes_cli/gateway_migrate.py`, `hermes_cli/gateway_migrate_guards.py`, `hermes_cli/gateway_runtime.py`, `hermes_cli/gateway_runtime_service.py`, `hermes_cli/gateway_runtime_service_identity.py`, `hermes_cli/main.py`, `hermes_cli/plugin_isolation.py`, `hermes_cli/plugins.py`, `hermes_cli/plugins_cmd.py`, `hermes_cli/plugins_cmd_install.py`, `hermes_cli/plugins_cmd_update.py`, `hermes_cli/plugins_discovery.py`, `hermes_cli/plugins_ledger.py`, `hermes_cli/plugins_loader.py`, `plugins/memory/__init__.py`, `pm/plugin_declarations.py`, `hermes_cli/gateway_launchd.py`, `hermes_cli/gateway_multiplex_mode.py`, `hermes_cli/plugins_dispatch.py`, `tools/skill_manager_tool.py`, `cron/scheduler_script.py`, `gateway/run_profile_reconcile.py`, `agent/redact.py`, `hermes_cli/plugins_cmd_git.py`
- [4] [Supabase for Platforms: make database changes (Supabase documentation)](https://supabase.com/docs/guides/integrations/supabase-for-platforms#make-database-changes)
- [5] [psql: `\password` (PostgreSQL 17 documentation)](https://www.postgresql.org/docs/17/app-psql.html)
- [6] [libpq: `PQencryptPasswordConn` (PostgreSQL 17 documentation)](https://www.postgresql.org/docs/17/libpq-misc.html)
- [7] [systemd.service: command lines, Restart=, RestartPreventExitStatus=, RestartSteps= (systemd documentation)](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
- [8] [systemd v255 source: `service_enter_start_pre` in `src/core/service.c`](https://github.com/systemd/systemd/blob/v255/src/core/service.c)
- [9] [pg_cron README: `cron.timezone` and the permissions a job runs with (Citus Data)](https://github.com/citusdata/pg_cron)
- [10] [Cron quickstart: `cron.job_run_details` (Supabase documentation)](https://supabase.com/docs/guides/cron/quickstart)
- [11] [Parallel Change, also known as expand and contract (martinfowler.com)](https://martinfowler.com/bliki/ParallelChange.html)
