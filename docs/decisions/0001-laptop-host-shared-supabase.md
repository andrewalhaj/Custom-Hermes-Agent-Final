# 0001. Hermes runs on the laptop; memory is one shared Supabase project

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

The old build ran Hermes on one machine (`<lh01>`) and its models on another (`<lh02>`), with memory in Supabase. The new plan moves the agent to a laptop. Memory has to outlive the laptop and be shared with other machines.

## Options

1. **Everything on the laptop, memory in local Postgres.** Fastest search, nothing leaves the laptop, works offline. Lost if the laptop is lost, and other machines cannot reach it.
2. **Agent on the laptop, memory in the existing paid Supabase project.** Survives the laptop, shared by every machine, managed backups. Every search is a network round trip, and stored text sits with a third-party host.
3. **Agent on the laptop, memory in Azure Database for PostgreSQL in the company tenant.** Survives the laptop, Entra sign-in, data stays in company infrastructure. Needs a tenant change made by a person, and its pgvector is 0.8.0 with a reported HNSW issue on some hardware.

## Decision

Option 2. Hermes, its plugins and the local models run on the laptop. Memory is one Supabase project, `<project-ref>`, used by every machine that runs the agent.

## Consequences

- **Every machine embeds with the same model build.** The build is pinned by its digest, `<model-digest>`, and recorded in `embedding_model`. A machine whose digest differs refuses to run recall and says why (`embed.model_mismatch`).
- **Every row records which machine wrote it** (`source`). Scope is the project plus the machine.
- **Each machine has its own database role**, which can read, insert and update but not delete or change the schema.
- **Tables live in a schema the Supabase Data API does not expose**, so the API keys cannot reach them.
- **Scheduled jobs run on a machine that is on at the time**, `<job-host>`. A laptop that sleeps cannot be the only job host.
- **Search costs a network round trip.** The old build measured 0.53–0.77 s per search through Supabase.
- **Stored facts and session text sit with Supabase.** The data that leaves each machine is listed in CONTRIBUTING.md section 4.7.

## Sources

- CONTRIBUTING.md sections 3 and 5.
- Supabase pricing and compute pages; Azure Database for PostgreSQL April 2026 release notes (pgvector 0.8.0.2).
