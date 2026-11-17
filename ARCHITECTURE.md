# Architecture

This document describes how the custom build is put together: the memory provider, retrieval, the memory store, the guards and failure handling. It describes the design, not the rules for changing it; those are in [CONTRIBUTING.md](CONTRIBUTING.md). Where the build runs and how it is operated is in [OPERATIONS.md](OPERATIONS.md).

Facts about Hermes itself are stated for **Hermes v0.23.1**, read from its documentation and source. Re-check them before upgrading Hermes.

**Contents**

1. [Overview](#1-overview)
2. [The memory-provider contract](#2-the-memory-provider-contract)
3. [Retrieval](#3-retrieval)
4. [The memory store](#4-the-memory-store)
5. [Guards](#5-guards)
6. [Failure handling](#6-failure-handling)
7. [Logging](#7-logging)

## 1. Overview

The build extends Hermes through its documented extension points and leaves the Hermes core unmodified, so upstream updates can still be taken.[1]

| Part | Hermes extension point | What it does here |
|---|---|---|
| Memory provider | A memory provider plugin. Hermes allows one external provider at a time, calls it before each turn, and exposes its tools to the agent.[2][3] | Stores and searches long-term memory (sections 2–4) |
| Guards | Plugin hooks that run before the model is called, before each tool call and on the final reply.[4] | Force lookups and stop unsafe actions (section 5) |
| Skills | Hermes skills. Hermes lists every skill's name and description in the prompt itself, so recall does not search them. | Procedures the agent follows for recurring tasks. The build ships none at first; a skill is added, through a pull request, when a recurring task needs one. |
| Instruction files | `AGENTS.md`, loaded from the directory a session runs in, and `SOUL.md`, loaded only from the Hermes home directory.[5] | The agent's operating rules and identity. This repository contains neither file (CONTRIBUTING.md section 9). |

Hermes's built-in memory stays on. It is two size-limited files, `MEMORY.md` (2,200 characters) and `USER.md` (1,375 characters), loaded into the prompt at the start of each session.[6] They hold standing rules and preferences. Identity is `SOUL.md`, not memory. Everything else lives in the memory provider.

Memory is shared. Several machines run the agent against one memory store, so every stored row records the project it belongs to and the machine that wrote it.

**One plugin.** The memory provider, the four guards, the skills and the failure module (section 6.1) ship as a single Hermes plugin named `custom_build`: one directory of that name at the root of this repository, with each code part a subpackage that reaches the failure module by a relative import. The pull request that adds the directory's first file adds its line to the `.gitignore` allow-list (CONTRIBUTING.md section 4.4). Hermes loads that directory twice:

- as an ordinary plugin, listed in `plugins.enabled`, which registers the guards' hooks, the skills and the redaction patterns when Hermes starts;
- as the memory provider, selected by `memory.provider`.

The manifest declares `kind: standalone`. Without it, Hermes treats a directory that defines a memory provider as a provider only and skips the ordinary load.[16] Hermes matches the two loads of one plugin by name, so the directory under `plugins/` carries the manifest's name, as `hermes plugins install` makes it.[16] The layout was run, and is supported, only with Hermes's default plugin isolation, `in_process`, which every agent host keeps (OPERATIONS.md section 3). The other mode, `host`, runs user plugin code in a separate process.[16]

`register()` runs under both loads, and hooks are the only registration the two loads share.[16] So each load registers what it needs:

- the ordinary load registers the guards' hooks, the skills and the redaction patterns, so the guards do not wait for the memory provider to load, and no guard can be missing on its own;
- the provider load registers the memory provider, whose tools Hermes takes from the provider itself,[3] and the redaction patterns again, so the provider never writes without them even if the ordinary load failed. Hermes keeps one copy of each pattern.[14]

`register()` tells the two loads apart by the context Hermes passes it: the provider load passes a narrower context, with no `state` attribute (section 2).[14] The developer guide does not describe this (section 5.6). Hermes v0.23.1 lists a plugin's registered skills in the prompt with the others.[16]

Hermes imports the directory once for each load, under two module names, so anything held at module level exists twice.[16] The guards and the provider share nothing in memory, and each copy opens its own connection to the store. What they share goes through the memory store or the plugin's storage (section 2).

Hermes's developer guide says a memory provider can also load as an ordinary plugin, and that the ordinary load then owns its hooks.[15] It does not describe `kind: standalone` on such a directory, the two imports, or the two contexts `register()` receives. Those are read from the v0.23.1 source, were run against a throwaway plugin, and are re-tested on every Hermes upgrade (OPERATIONS.md section 5). The layouts that were compared, and the runs behind this one, are in #20.

## 2. The memory-provider contract

- **Prefetch blocks the turn.** Hermes waits for `prefetch()` for up to 8 seconds, then treats a timeout or an error as "no notes" and carries on. The provider therefore enforces its own deadline below 8 seconds. On failure it returns a "recall failed" note with its failure code instead of raising, because that is the only way the failure reaches the turn.
- **There is no input for a second query.** `prefetch()` receives only the user's message. The action query (section 3.5) therefore runs from a `pre_llm_call` plugin. Hermes runs `pre_llm_call` before prefetch, bounds it with its own callback timeout (`plugins.hook_callback_timeout`, 30 seconds by default), and skips prefetch entirely for a turn that `pre_llm_call` blocks.[4]
- **The project key comes from the session's working directory.** Both copies of the code (section 1) read it with Hermes's documented helper, `agent.runtime_cwd.resolve_agent_cwd()`, and turn it into a key the same way.[3][15] `initialize()` receives the directory as `cwd` only when the agent was built with one, and only as it was at that moment, so neither copy relies on it.[3][14] The key comes from the repository, so it is the same on every machine: the normalised URL of the repository's `origin` remote. An absolute path is never used, because it differs between machines. A workspace that is not a repository has no project, and neither has a repository whose `origin` is missing, a local path or a file URL: Hermes v0.23.1 has no per-repository setting that could name one (#112). Neither copy calls `os.getcwd()` itself.
- **Every entry point goes through one boundary wrapper.** Hermes contains errors from hooks and providers, often silently (section 5.5). Each function Hermes calls is wrapped by a single shared wrapper in the failure module. On an unexpected exception it records the failure, with its traceback in the log (section 6.1), then does what that entry point requires:

  | Entry point | Recorded as | Then |
  |---|---|---|
  | A guard on `pre_tool_call` or `transform_llm_output`, in `on` mode | `guard.crash` | Re-raises, so Hermes blocks the call or withholds the reply |
  | A guard in `log` mode, on any hook | `guard.crash` | Returns nothing, so the call or the reply goes ahead: `log` mode blocks nothing (section 5.4) |
  | A guard on `pre_llm_call` | `guard.crash` | Returns a block that names the failure |
  | The action query on `pre_llm_call` | `unhandled` | Returns a "recall failed" note, so the turn and the message query go ahead |
  | `prefetch()` | `unhandled` | Returns a "recall failed" note |
  | Any other provider method | `unhandled` | Re-raises |

  This wrapper, and the job wrapper (section 6.1), are the only code allowed to catch everything (CONTRIBUTING.md rule C2).
- **A provider tool that raises reaches the agent as a tool error.** Hermes catches an exception from a provider's tool and returns an error result that names the tool and carries the exception's message, so the agent never sees it as an empty result.[14] The memory search tool is a provider tool and goes through the boundary wrapper like any other provider method: an unexpected exception is recorded as `unhandled` and re-raised. A failure the tool expects, such as a missed deadline or an unreachable store, is returned as a failure result that carries its code in an `error` field (CONTRIBUTING.md rule C3). A search whose query is empty or not text searches nothing and is returned with an `error` field as well, holding a plain reason: it is the model's misuse, so it has no code and is not recorded, but Hermes still reports the call as an error (#187). Hermes then reports the call with the status `error`, so no guard counts it as a lookup (section 5.3).[14]
- **Threads.** Background work uses Hermes's `spawn_context_thread`, never a bare `threading.Thread`, which would write into the wrong profile's store. `register(ctx)` builds a new provider instance on each provider load.
- **Local state.** What a guard keeps on a machine between calls, such as its record of what a session has read, goes through Hermes's per-plugin storage: `ctx.state`, which is scoped to the profile and limited to 10 MiB for each plugin, or the plugin's data directory. Nothing is written into the plugin's own directory, which a reinstall replaces, or into `config.yaml`.[15] The guards get that storage from the ordinary load (section 1). The memory provider is loaded through a narrower context that forwards only registration calls, so it has no `ctx.state`.[14] It uses Hermes's `plugins.plugin_storage` helpers, which give a plugin a data directory and a SQLite database under the Hermes home.[16]
- **Built-in memory writes are mirrored.** `on_memory_write` reports each write the memory tool commits to `MEMORY.md` or `USER.md` at once. The provider stores them as deliberate facts. Hermes v0.23.1 reports a write approved at the inline prompt, but not one staged for approval (no one at a prompt, or a background review) and approved later with `/memory pending` (`agent/memory_manager.py`, `notify_memory_tool_write`; `tools/write_approval.py`, `evaluate_gate`), so a staged entry is not mirrored.
- **Session end.** The provider's own `on_session_end` method fires at session boundaries, never per turn, and the digest is written there; Hermes also calls it at each compression and when the gateway evicts an idle agent, where the session goes on (section 3.3, #156). The plugin hook with the same name is a different call: it fires at the end of every turn's run, so no session-end work is attached to it.[14][15]
- **Subagents get no automatic recall or writes.** Hermes suppresses prefetch, sync and session-end calls for subagents.[3]

## 3. Retrieval

The model keeps nothing between turns except the built-in memory files. Every message triggers a search, and the best results are added to the turn before the model reads it. A separate write path decides what is stored.

```mermaid
flowchart LR
  subgraph W["Write path: what gets remembered"]
    A["Stored on purpose"] --> R1["Redact"] --> F1["Pattern filter"]
    B["User messages and tool results"] --> R2["Redact"] --> X["Extraction model"] --> F2["Judge gate, then support check"]
    C["Session transcript, at session end"] --> R3["Redact"] --> D["Digest model"] --> F3["Judge gate"]
    F1 --> S["Store: embed, deduplicate"]
    F2 --> S
    F3 --> S
    S --> K[("Facts")]
  end
  subgraph R["Read path: every message"]
    Q["Message"] --> E["Embed the query"]
    E --> KS["Facts in this project: vector and full-text, fused"]
    KS --> RR["Rerank, then blend in recency"]
    E --> RS["References: vector and keyword, fused"]
    E --> SS["Sessions, on past-work questions: vector and full-text, fused"]
    RR --> M["Merge, slot rules, score floor"]
    RS --> M
    SS --> M
    M --> N["Recalled notes added to the turn"]
  end
  K --> KS
```

### 3.1 The stores

| Store | What it holds | How it is searched |
|---|---|---|
| Built-in memory | Standing rules and preferences | Not searched. It is in every prompt.[6] |
| Facts | Short facts and decisions, and one digest per long session | By meaning and by exact words, then reranked |
| Sessions | What was said in past conversations in the last 90 days, cut into chunks | By meaning and by exact words, on questions about past work |
| References | Long write-ups and procedures; none at first | By meaning and by exact words |

Facts go in the facts store. Long procedures and write-ups are references. What was said is in sessions. How to do a task is a skill.

The sessions store is built only after the eval set (section 3.8) shows it adds recall over Hermes's built-in `session_search`, which already searches every past session by full text, and is kept only while it does; #17's open questions are settled then.

There are no references at first, as there are no skills: the store is added when there is a set of documents to search, and #3 is decided then.

### 3.2 Embedding

- **One model for every store.** Facts, chunks and queries are all embedded by `Qwen3-Embedding-4B` at its native 2,560 dimensions, so their vectors are comparable.
- **One build on every machine.** The model build is pinned by its digest and recorded with every vector (`embedding_model`), written `qwen3-embedding:4b@sha256:<digest>` from Ollama's list of local models. A machine whose build differs refuses to run recall (`embed.model_mismatch`). A vector from a different build is never stored. The store's build is the one its facts record, so an empty store records none until its first fact. The provider checks when it starts, and its embedder checks again before embedding until a check has passed, so a failed check refuses every embedding and a later pass recovers.
- **Storage.** Vectors are stored as `halfvec(2560)` with an HNSW cosine index. At this size the half-precision type is required: an HNSW index supports `vector` only up to 2,000 dimensions and `halfvec` up to 4,000. Indexing `halfvec` needs pgvector 0.7.0 or later.[7]
- **Query instruction.** Queries are prefixed with the task instruction the model card recommends; documents are embedded without one.[8] It becomes the default only after the eval set shows the gain.
- **What is embedded.** A fact is embedded once, when it is stored. Reference and session text is cut into chunks of about 800 characters. Each chunk is prefixed with its origin: the file and heading for a reference, the user's message for a session reply.
- **Caching.** A query is embedded once per turn and reused by every store.
- **No substitute model.** If the embedding service is unavailable, search fails with `embed.unavailable` and new facts are refused.

### 3.3 The write path

1. **Three entrances.**
   - The agent stores a fact on purpose, with the provider's `store_fact` tool, saying what it rests on.
   - A write to built-in memory (`MEMORY.md`, `USER.md`) that Hermes reports (section 2) is mirrored as a fact stored on purpose, with the basis `user_stated` (section 2) and no project, because built-in memory is one file per profile, not per repository. An added entry is stored; a replaced entry supersedes the old entry's fact; a removed entry retires it, with the reason "removed from built-in memory". The pattern filter does not apply: a person approved the exact text.
   - An extraction model reads the user's messages and tool results (command output, file contents, documentation lookups) and proposes facts. It never reads the agent's own replies, because a wrong reply would otherwise be recalled later as a known fact. It runs after each turn of a primary session, in the provider's `sync_turn`, which Hermes calls on its background worker once the reply is shown, through the build's own auxiliary task, `custom_build_extraction` (#148). A call the provider refuses for the account is `extract.refused`; any other failure, or an answer that is not a list of facts, is `extract.error`.
   - At the provider's `on_session_end` (section 2), a digest model writes a digest of each long session (section 3.6), in a primary session only (#155). A session keeps one live digest: a later one replaces the earlier one, and a session that already has a digest gets a new one at each later end, even when its transcript is short again after an in-place compression; a digest written earlier never replaces one written later, ordered by time of writing because a resumed session counts its turns again from its compressed history (#156, decided B). Hermes v0.23.1 calls it at a CLI `/new`, on its background worker before the session id changes; inline at CLI exit and when the gateway stops; and when the gateway evicts an idle agent, whose session can resume, so a digest is written before the session's real end, and replaced at it (#156). Compression calls it too, inline, with the transcript before compression (`agent/conversation_compression.py`, `_commit_compaction`), and by default keeps the same session id (`compression.in_place`), so the turn being compressed waits for the digest, which the session's later ends then replace. When the gateway stops, Hermes waits about 30 seconds for this work and then abandons it, so a slower digest there is lost without a record (#156). The digest model reads the redacted transcript, without system prompts, keeping the newest messages when it is very long, and answers with a summary and the work left open, stored as the summary followed by an "Open items:" list. It goes through the build's auxiliary task `custom_build_digest`, which inherits the extraction task's route. Its basis is `assistant_inferred`, since the transcript holds the agent's replies (rule 6), and it is never compared with facts, so it supersedes only its own session's earlier digest. A call the provider refuses for the account is `digest.refused`; any other failure, or an answer that is not a digest, is `digest.error`.
2. **Redaction before any model sees the text.** The message, the tool output and the session transcript are redacted before they reach the extraction, digest or judge model, and before anything is stored (CONTRIBUTING.md rule L4). If redaction fails, nothing is sent or stored (`redact.failed`).
3. **A filter on each.**
   - A fact stored on purpose passes a pattern filter: it must meet the minimum length and must not match the narration patterns in the write-path configuration file, `custom_build/memory/write_path.toml`, which also holds section 3.6's supersede thresholds and the wording of the judge's questions.
   - Extracted facts and digests go to a judge model, which drops the ones not worth keeping.
   - An extracted fact then gets a separate check: does its source turn support it? A fact its source does not support is not stored. For an extracted fact both questions go to the judge in one request, worded in `write_path.toml`; the gate drops it at the judge gate's drop score, and the support check needs its own threshold (both in section 3.6, #152). A fact queued for the judge keeps its source turn, so the retry asks both questions too.
   - If the judge errors, the fact is queued for retry, never stored unchecked (`store.gate_error`). It waits, redacted, in the plugin's own SQLite database on the machine (section 2), and the provider retries the queue each time it starts in a primary session (not a subagent, cron run or flush), least-tried facts first, stopping at the first judge failure; a retry that resolves a fact is recorded as `store.gate_error` with the result `degraded`.
4. **One store function.** All three entrances then go through one function. It embeds the text and compares it with what is already stored in the same project. "Short" and "long" are defined in section 3.6.

   | Match with an existing fact | Result |
   |---|---|
   | Identical text | Not inserted |
   | Very close in meaning, short text | The new fact supersedes the old one |
   | Very close in meaning, long text, or somewhat close in meaning | The judge decides whether it is the same subject and whether the new fact supersedes the old |

5. **Origin protects deliberate facts.** A fact stored on purpose can supersede an extracted one. The reverse is never allowed.
6. **Every fact records its basis and source.** `basis` is `user_stated`, `tool_observed` or `assistant_inferred`; `source_turn` points to the turn it came from: the session's user-turn number, as Hermes counts it for `on_turn_start`, since a provider tool is not given a message position (#113). A digest takes the weakest basis of its inputs, so a summary never turns an inferred claim into a stated one. Recalled notes show the basis.
7. **Nothing is silently lost.** A superseded or retired fact stays in the table, marked, with a pointer to its replacement or the reason it was retired.

### 3.4 Retrieval

Retrieval is hybrid, with a rerank stage. A search by meaning and a search by words each produce a ranked list, fused by rank position (reciprocal-rank fusion). A row found both ways usually ranks above a row found one way, but not always: a row ranked low in both lists can score below a row ranked first in one.

| Store | By meaning | By words | Fusion | Rerank |
|---|---|---|---|---|
| Facts | Cosine distance over vectors | Postgres full-text, any of the query's words | Rank fusion, inside one SQL function | Yes |
| References | Cosine over chunks | BM25 over the same chunks, in application code | Rank fusion | No |
| Sessions | Cosine over chunks | Postgres full-text | Rank fusion | No |

For the facts store:

1. Embed the query.
2. Fetch a candidate pool larger than the number of results wanted, from live rows in the current project and live rows with no project, which mirrored built-in memory has (section 3.3, #142). A session with no project searches live rows in every project.
3. Fuse the two ranked lists. Steps 2 and 3 are one SQL function, `memory.search_facts` (`0005`), which runs as its caller, under the caller's grants and row policies (#158).
4. Rerank the pool with the judge model, under the judge deadline (section 3.6). One request carries the user's message as state and one probability question per candidate, each with the candidate's own text, worded in `write_path.toml` (#165); each result records `ranked_by: judge` and its score, and both `prefetch()` and the memory search tool rerank. If the judge errors or misses that deadline, the pool keeps its fused order and the result is recorded as degraded (`rerank.fallback`), never as a silent success; the notes are still delivered. No local reranker is kept as a fallback. A cross-encoder would fit the recall deadline for a pool of short facts on an agent host with no GPU (about 2 seconds for 30 facts, #15), but it would put a second model and its runtime on every agent host only for the turns when the judge is down. Whether a cross-encoder should replace the judge as the reranker is a question for the eval set.
5. Blend recency back in, on a reranked pool only. A rerank score ignores age, so the final score adds a recency term from `last_verified_at`, or `created_at` for a fact never checked.[9] Its weight is set by the eval set. A pool whose rerank failed keeps its fused order (step 4). Recency is not built yet: it arrives with the eval runner's numbers, as the action query waits for its own (#165, #26).
6. Return the top results. Each result records which searches found it and which stage ranked it.

**Filtering and the vector index.** pgvector applies `WHERE` conditions after the HNSW scan, so a filtered search can return fewer rows than asked for.[10] The search functions set `hnsw.iterative_scan = relaxed_order` (pgvector 0.8.0 and later). The write path's lookup of the one nearest fact (section 3.3) sets `strict_order`, because it reads only the top row and needs the truly nearest one. The facts index is partial, over live rows only, and queries repeat that predicate exactly so the planner uses it.

**Word queries.** The full-text side joins the query's words with OR and ranks with `ts_rank_cd`. `plainto_tsquery` and `websearch_to_tsquery` both join unquoted words with AND, so with either one a long question matches nothing.[11] Postgres full-text ranking is not BM25: it uses no statistics from the rest of the collection.[11] BM25 for references therefore runs in application code.

### 3.5 Delivery

1. **Every message is searched.** Only an empty message is skipped.
2. **Two searches run, one after the other** (section 2), once the action query is switched on; until then only the message query runs:
   - **The action query, first.** In `pre_llm_call`, one request, the routing request, goes to the judge model with the user's message and the end of the assistant's previous reply, which Hermes passes to the hook in the conversation history.[14] The judge answers two typed questions, each with a probability: which kind of work from a fixed action list the message asks for, and whether it asks for work at all. The facts store is searched with the picked action's description, and the results are added to the turn through the hook's own context.
     - The action query ships switched off, and is switched on only once the eval set has measured it (#26).
     - The judge always returns a pick, whatever the message. The action query therefore runs unless "asks for work at all" is a clear no (section 3.6). The top pick is used whatever its probability; whether a low one should count as no pick is a question for the eval set (#26).
     - A skipped action query is recorded in `events` under its own event name, `action.skipped`, so it cannot be mistaken for one that ran and found nothing, which is recorded as `empty`.
     - The action list and the wording of the two questions are one configuration file in this repository, `custom_build/memory/action_query.toml`, which also holds the switch and the action query's values from section 3.6. The list and the wording are the ones measured in the test for #15. Changing any of them is a pull request with eval-set numbers (CONTRIBUTING.md rule C8).
     - The notes carry a header of their own, so they can be told apart from the message query's. The user's message and the end of the previous reply are redacted before the routing request is sent (CONTRIBUTING.md rule L4).
     - The routing request runs under the judge deadline, and the action query as a whole under its own deadline, well inside Hermes's callback timeout (section 3.6). If the routing request errors or misses its deadline, the action query is skipped for that turn, the failure is recorded (`judge.error` or `judge.timeout`), and the turn carries a note with the code. If the search that follows misses the action query's deadline, that is `recall.timeout`.
     - A model that writes the action as free text is not used. In a benchmark on an agent host with no GPU, a local one took 4.5 to 8 seconds for each message. The judge answered in about 0.2 seconds, and its pick was among the answers the test's labels accepted on 42 of 44 messages (#15). Whether a search by the picked action finds the same facts as a search by a freely written one has not been measured; the eval set settles it before the action query is relied on (#26).
   - **The message query, second.** `prefetch()` searches the user's words across the stores, under its own deadline below Hermes's 8-second limit.
3. **Questions about past work widen the search.** They fetch more results and add past sessions. A question about past work is one that the recall gate's patterns match (section 5.3), so the search and the gate always agree on it. The patterns are in `custom_build/guards/recall_gate.toml`, written before the recall gate itself (#158); until the sessions store exists (3.1), widening means 8 results.
4. **"What is left to do?" adds the newest open items to the search.** A question about open work gets the newest digests' open items for the current project, newest first, because such a question has few topic words to match and its answer depends on recency. They are added before the normal search's notes, not instead of them, within the 8 results a question about past work gets; the search keeps its 5 (decided on #158, option A). A pattern that misfires on an ordinary message ("what's left on the disk?") then only adds a few notes, and patterns always misfire on some phrasing. The patterns are kept in the same configuration file as the recall gate's, checked before the past-work patterns. Each open item is a note of its own, with its digest's provenance; a project whose newest digests leave nothing open gets the search's notes alone. A session with no project reads the newest digests that have no project.
5. **Slot rules keep the mix fair.** The merge drops reranked facts under the score floor, then applies a cap on digests, reserved places for references and the cut to the number of results per message (section 3.6). The floor applies only where the score is a probability: the judge's rerank score for a fact. Results ranked by fusion alone, which are session chunks, references, and facts whose rerank failed, have no floor and are cut by rank only.
6. **Results are attached to the message** as recalled notes, labelled as notes and not as instructions. Each note shows its origin, its basis, the date it was stored and, when checked since, the date it was last verified. The provider's `prefetch()` does this (#158): it searches on its own thread, started through Hermes's `spawn_context_thread` so it keeps the session's directory and so its project, inside the boundary wrapper, and waits for it at most the recall deadline. The store cancels the search's statements at the same deadline, so a stalled store cannot hold a thread and a connection open turn after turn; the turn is told `recall.timeout`, and the event row is written off the turn's path, so recording it cannot take the call past Hermes's 8 seconds. Each note shows its provenance before its text, with a digest's open items indented under it. When the pool is mostly digests, the one-digest cap can leave fewer than 5 notes. Hermes v0.23.1 wraps every provider's context with its own label, "authoritative reference data"; the build's label, notes and not instructions, sits inside it, and the eval set should check how the model reads the two together.
7. **The agent can also search on demand** with the memory search tool.

A recall that fails or misses its deadline is reported in the turn with its failure code. It is never presented as "no notes found".

### 3.6 Defaults

These are configuration values. Change one through an issue, with eval-set numbers from before and after (CONTRIBUTING.md rule C8).

| Setting | Default |
|---|---|
| Chunk size | 800 characters; 100 shared between neighbouring session chunks |
| Rank-fusion constant | 20 for facts and references, 60 for sessions |
| Candidate pool for facts | Three times the results wanted, at least 15 |
| Short text, long text | Short is 300 characters or fewer; long is anything longer |
| Long session (gets a digest) | 20 messages or more |
| Minimum fact length | 10 characters |
| Supersede without the judge | Cosine similarity 0.92 or higher, short texts only |
| Ask the judge | Cosine similarity from 0.80 up to 0.92, or 0.92 and higher for long texts. The judge returns two probabilities, "same subject" and "the new fact supersedes the old"; both must be 0.75 or higher. |
| Judge gate | Drops at a drop score of 0.70 or higher: 1 minus the judge's probability that the fact is worth keeping (#152) |
| Support check (extracted facts) | Kept only when the judge's probability that its source turn supports it is 0.75 or higher. A starting value; the eval set confirms or changes it. |
| Results per message | 5; 8 on a question about past work; 3 for the action query |
| Digests per message | At most 1 among the search's notes; the rest are facts. A question about open work also carries open items from up to 3 digests, each item a note of its own (3.5 point 4). A starting value (decided on #158); the eval set confirms or changes it. |
| Digests read for "what is left to do?" | The project's newest 3. Their open items come first and fill the 8 results beside the search's 5. A starting value (#158); the eval set confirms or changes it. |
| Score floor | 0.30, on facts the judge reranked, where the score is the judge's probability that the fact helps answer the message: under 0.3 reads as no, as in the "Asks for work at all" row. No floor on results ranked by fusion alone. A starting value; the eval set confirms or changes it. |
| Recall deadline (`prefetch()`) | 6 seconds |
| Action query (section 3.5) | Off, until the eval set has measured it (#26) |
| Action-query deadline (`pre_llm_call`) | 6 seconds |
| Judge deadline (one rerank or routing request) | 2 seconds. A starting value: the slowest routing request in the test for #15 took 0.3 seconds, and a rerank request has not been timed. |
| Previous reply sent with the routing request | Its last 500 characters, as in the test for #15 |
| "Asks for work at all" (section 3.5) | A probability under 0.3 is a clear no, and the action query is skipped. Anything higher runs it. A starting value, the one used in the test for #15; the eval set confirms or changes it (#26). |
| Recency weight | Set by the eval set |
| Recalled-note budget | Set by the eval set |

Hermes caps the context that one `pre_llm_call` hook adds to a turn at 10,000 characters by default. Anything above the cap is written to a file and replaced in the turn with a preview and the file's path.[15] The action query's notes are added to the turn by that hook (section 3.5), so their budget stays below the cap.

Still open, each tracked in its own issue:

- Whether reference chunks move into the shared store, decided with eval-set numbers once there is a set of documents to search (#3).
- Where the memory provider reads each message's uid, and what happens to a session chunk when its uid appears on more than one row, its text changes or the message has none, to a branch's copied history, and to facts drawn from messages a rewind removed, settled when the sessions store is built (section 3.1) (#17).
- Whether a search by the picked action finds what a search by a freely written one would, and whether a low-probability pick should count (#26).

### 3.7 What leaves the host

Every deployment states this in its own documentation:

- Stored facts, digests, session chunks and events go to the memory database, which keeps session chunks and events for 90 days (section 4.3).
- Search candidates, candidate facts and support checks go to the judge model when it is hosted (search candidates go with the user's message, redacted, for the rerank, on every message whose search finds anything and on each memory search, #165; an extracted fact goes with its source turn: the user's message and the turn's tool results, redacted), and so does each new fact with the stored fact closest to it, when the judge decides whether one supersedes the other (section 3.3).
- Each user message, with the end of the assistant's previous reply, goes to the judge model for routing (section 3.5) when it is hosted and the action query is switched on.
- User messages and tool output go to the extraction model when it is hosted: after each turn of a primary session, the user's message and that turn's tool results, redacted, never the agent's replies. The first deployment's extraction model is hosted by Anthropic and reached through the Claude subscription (OPERATIONS.md section 2, #148). When that call fails, Hermes's auxiliary fallback can send the same request to the main agent model's provider, which already receives the turn; the build discards that answer and records the failure (`extract.refused` or `extract.error`).
- Session transcripts go to the digest model when it is hosted: at the end of each long primary session, redacted, without system prompts. The first deployment's digest model is the extraction model, reached the same way (OPERATIONS.md section 2, #155), with the same fallback behaviour; each digest then goes to the judge for its gate.
- The daily failure report goes to the operators' channel.

Secrets are redacted before anything is stored or sent.

### 3.8 The eval set

No ranking change ships without numbers from the eval set.

- It starts from LongMemEval-S, which covers information extraction, multi-session reasoning, temporal reasoning, knowledge updates and abstention.[12]
- It grows from real misses found in the `events` table: open-work questions answered badly, notes from the wrong project, recall that found nothing, and questions whose correct answer is "no note". A miss is promoted by copying it into the eval set; the `events` row itself is not kept.
- LongMemEval-S is not committed: its data file is about 280 MB. The eval runner downloads the cleaned release's S file at a pinned revision and checks it against a recorded hash before it runs. It is MIT-licensed.[17]
- The rest of the eval set lives in this repository as data files, reviewed through pull requests like code. Each question records its project and the expected fact or chunk IDs. A question copied from a real miss is reworded so that it holds no deployment values (CONTRIBUTING.md section 3). It expects facts, not session chunks, because chunks are deleted after 90 days (section 4.3), so real misses measure the facts store only; LongMemEval-S measures the sessions store.
- Reports give recall@5, MRR and an abstention score.
- The eval set runs on a maintainer's machine with a GPU, on the pinned embedding build checked by its digest, against its own throwaway Postgres. It never touches the shared store (OPERATIONS.md section 2).
- **The runner, `evals/longmemeval.py` (#167), replays the whole build** (decided: option C). For each question of a seeded subset spread across question types, it builds a throwaway database and Hermes home, drives the provider over the question's chat history as Hermes would (`evals/replay.py`: sessions in date order, each answered turn through `sync_turn`, so extraction, the judge's gate and the store, and `on_session_end`, so the digest), dates the stored rows by their sessions, asks the question through `prefetch()`, and scores the notes: recall@5 and MRR against the evidence sessions, matched by a note's exact stored text, and, as a first cut, an abstention question counts as answered when recall brings no notes. A recall that reports a failure counts as a miss, never as an abstention, and is counted on its own. It writes `report.json` and `report.md` with the commit, the embedding build and the judge model, also when a run stops or crashes part-way. It refuses a Postgres that already has the memory roles, because it drops the ones it creates and they are the whole server's.
- **A run is gated, because option C calls Haiku on the Claude subscription for every user turn and every long session's digest, and the judge for every proposed fact and every rerank.** Without `--yes` the runner prints only its estimate of the calls; it stops at the turn the first `extract.refused` or `digest.refused` (#148) is recorded, or an extraction or digest error that names a limit (a plan limit), keeping what it measured. The data file's revision and SHA-256 are recorded in `evals/longmemeval.toml` at the first download, which is a maintainer's to approve; until then the runner refuses to run. Extraction and digests need the Claude subscription provider set up in the Hermes home the runner copies (`--template`).

## 4. The memory store

The memory store is a Supabase project: Postgres with the `pgvector` extension. It holds the facts, the session chunks and the events, and runs the hybrid search inside SQL functions.

### 4.1 Design rules

1. **One meaning for each column.** What a row is, where it came from, which project and session it belongs to and which machine wrote it are columns, never tags or fields in a shared blob.
2. **The lifecycle is in the database.** Each fact records when it was last verified and what superseded it. Search reads only live rows, so a stale fact cannot look like a current one.
3. **Nothing is hard-deleted by the agent.** A fact is superseded, with a pointer to its replacement, or retired, with a reason. The row stays.
4. **Every vector records the build that made it.** A change of embedding model is a visible migration, not a silent mismatch.
5. **Nothing unused.** No column, index or function argument exists without code that reads it.
6. **Any machine can write a session chunk.** A chunk is identified by its source, session, the message's `message_uid` and its chunk number, never by a row id or a position. Rows carry Hermes's `session_id`. Compressing a transcript keeps it while `compression.in_place` is `true`, the default, which every agent host keeps; with `false`, Hermes would give the conversation a new `session_id`, and one conversation's rows would sit under two.[14][15] Hermes also changes the `session_id` on purpose, when a session is resumed, branched, reset or started anew. It tells the provider through `on_session_switch`, or through `initialize()` when a resumed session starts in a new process; rows already written keep the id they had.[3][14] A position in Hermes's message list can repeat after an in-place compaction or a rewind with `/undo` or `/rollback`,[14] while a message's `message_uid` is set when the message is first saved and kept by every later copy of it, through compaction, rewrites and a branch's copied history.[14] Still open (#17): where the memory provider reads each message's uid; what happens when one uid appears on more than one row or the text under it changes, to a message with no uid, to a branch's copied history, and to facts drawn from messages a rewind removed. A fact records only its turn's position, and Hermes's notice of a rewind names no removed message.[14]

### 4.2 Schema

`memory.events` and the `memory` schema are created by `migrations/0001_memory_events.sql`, and `memory.facts` by `migrations/0002_memory_facts.sql`, which also installs the `vector` extension where it is missing; those files are their source of truth, and the block below still shows them for reading in one place. The rest of this section is the plan. Each table's migration file becomes its source of truth when it merges, and this section is updated to match. A new migration creates only what earlier ones did not.

```sql
create extension if not exists vector;
create schema memory;   -- not exposed through the Supabase Data API

create table memory.facts (
  id               uuid primary key default gen_random_uuid(),
  body             text not null,
  body_hash        text not null,            -- hash of the normalised body, for the identical-text check
  kind             text not null check (kind in ('fact', 'digest')),
  origin           text not null check (origin in ('explicit', 'extracted', 'digest', 'imported')),
  basis            text not null check (basis in ('user_stated', 'tool_observed', 'assistant_inferred')),
  project          text,                     -- project key (section 2); null when the session had none
  source           text not null,            -- the machine that wrote the row
  session_id       text,
  source_turn      integer,                  -- the session's user-turn number the fact came from (#113)
  embedding        halfvec(2560) not null,
  embedding_model  text not null,            -- model name and build digest
  search_text      tsvector generated always as (to_tsvector('english', body)) stored,
  created_at       timestamptz not null default now(),
  last_verified_at timestamptz,
  superseded_by    uuid references memory.facts (id),
  retired_at       timestamptz,
  retired_reason   text,
  check ((retired_at is null) = (retired_reason is null)),
  check ((kind = 'digest') = (origin = 'digest'))
);

create unique index facts_live_body_hash on memory.facts (project, body_hash) nulls not distinct
  where superseded_by is null and retired_at is null;
create index facts_live_embedding_idx on memory.facts using hnsw (embedding halfvec_cosine_ops)
  where superseded_by is null and retired_at is null;
create index facts_search_text_idx on memory.facts using gin (search_text);
create index facts_session_idx on memory.facts (session_id);   -- the digest job checks for an existing digest

create table memory.transcript_chunks (
  id               uuid primary key default gen_random_uuid(),
  source           text not null,            -- the machine that wrote the session
  project          text,
  session_id       text not null,
  message_uid      text not null,           -- Hermes's id for the message, stable across compaction and rewinds
  chunk_no         smallint not null,
  body             text not null,
  embedding        halfvec(2560) not null,
  embedding_model  text not null,
  search_text      tsvector generated always as (to_tsvector('english', body)) stored,
  created_at       timestamptz not null default now(),
  unique (source, session_id, message_uid, chunk_no)
);

create index transcript_chunks_embedding_idx on memory.transcript_chunks using hnsw (embedding halfvec_cosine_ops);
create index transcript_chunks_search_text_idx on memory.transcript_chunks using gin (search_text);
create index transcript_chunks_created_at_idx on memory.transcript_chunks (created_at);   -- the retention job deletes by age

create table memory.events (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  source      text not null,                 -- the machine
  project     text,
  session_id  text,
  turn        integer,
  code        text not null,                 -- a code from the failure catalog, or an event name
  result      text not null check (result in ('ok', 'empty', 'failed', 'degraded')),
  reason      text,
  detail      jsonb not null default '{}'    -- IDs, scores, counts and hashes only; never content
);

create index events_at_idx on memory.events (at);
create index events_code_idx on memory.events (code, at);
```

| Column | Meaning |
|---|---|
| `kind` | A single fact, or a digest of a session |
| `origin` | Who wrote it: stored on purpose, extracted, written by the digest job, or imported |
| `basis` | What it rests on: the user said it, a tool showed it, or the agent inferred it |
| `project`, `source` | The project it belongs to, and the machine that wrote it |
| `last_verified_at` | When the fact was last checked against the live system |
| `superseded_by` | The fact that replaced this one. Empty on a live row. |
| `retired_at`, `retired_reason` | Set together when a fact is withdrawn without a replacement |
| `embedding_model` | The model and build that produced the vector |
| `events.result` | `ok`, `empty` (searched, found nothing), `failed`, or `degraded` (a fallback or a retry succeeded) |

Two SQL functions do the searching: `memory.search_facts` (`0005`, #158) and one for session chunks, which arrives with their table. Each runs the vector and full-text searches and fuses them. `memory.search_facts` reads live rows only; session chunks have no lifecycle, so their search reads every chunk. Each plans every call with its own values (PL/pgSQL `EXECUTE`): a plan reused with generic parameters never chose the vector index.

### 4.3 Access and migrations

- **The tables live in a schema the Supabase Data API does not expose**, so the project's API keys cannot reach them.
- **Each machine connects with its own role.** That role can read, insert into the tables code writes to, and update only the tables that take updates: `memory.events` takes none, and `memory.facts` takes updates of `superseded_by`, when the write path supersedes a fact, and of `retired_at` and `retired_reason`, when an entry removed from built-in memory retires one (section 3.3). It cannot delete rows or create, alter or drop anything. The privileges belong to two group roles that the migrations create and that cannot log in: `memory_agent`, which every machine's login role joins, and `memory_maintenance`, which no host's login role joins. Each table's migration grants them exactly what code uses: on `events`, select and insert for `memory_agent`, and select, delete and an insert limited by its row policy to retention's own `job.succeeded` row for `memory_maintenance`, for retention (`0004`, below); on `facts`, select, insert and update of `superseded_by` for `memory_agent` (`0002`), and update of `retired_at` and `retired_reason` (`0003`), and nothing for `memory_maintenance`, because retention keeps facts. The facts search, `memory.search_facts` (`0005`), may be run by `memory_agent` only; PUBLIC's default right to run it is revoked. On `schema_level` (`0006`, below), `memory_agent` has select only, and `memory_maintenance` nothing. A column that code starts to update later, such as `last_verified_at`, is granted by a forward migration with that code. Login roles and their passwords are created outside the repository (OPERATIONS.md section 3).
- **Row Level Security is on for every table, as a backstop.** With RLS on and no policy, Postgres denies every row to a role that does not own the table, so each machine's role gets an explicit select policy on every table, insert policies on the tables code writes to, update policies only on tables that take updates, and no delete policy.[13]
- **Schema changes are migration files in this repository**, reviewed through a pull request. Migrations only move forward; an applied migration is never edited. The store records the number of the newest migration applied in a one-row table, `memory.schema_level`, which a machine's login can read and cannot change: migration 0006 creates it, and every later migration ends by setting it to its own number. The plugin carries the number it was built against, and the startup check compares the two (section 5.5). A test fails a migration that does not set the level, and a plugin whose number is not the newest file's.
- **Data this repository did not create is not ours to change** (CONTRIBUTING.md rule C7). An export exists before any migration touches data.
- **Retention.** `events` rows and session chunks are deleted after 90 days by a database function that a migration adds and Supabase's `pg_cron` runs daily, so no host holds a login that can delete. The function, `memory.retain_events()` (`0004`), runs as `memory_maintenance`, which owns it (`security definer`, with its `search_path` fixed). The migration revokes the right to run it from PUBLIC, so a machine's login cannot call it; only the maintainer's role that applied the migration (Supabase's `postgres`, no host's login) may, and it schedules the `pg_cron` job. Neither that role nor `memory_maintenance` keeps the extra rights the hand-over needs (`set` on the role, `create` on the schema). Each run deletes `events` rows older than 90 days, except `guard.bypassed` rows, which the heartbeat would otherwise record again for a session that still exists (section 5.6), and records `job.succeeded` naming `retention`, with the rows it found as `input` and deleted as `output`; a missing one is reported as `job.missed` by the job-health check (OPERATIONS.md section 4).[18] Session chunks have no table yet; their retention arrives with it. Facts and digests are kept. The build's own recall then reaches older conversation only through them, and a session too short for a digest (section 3.6) only through its facts; Hermes's own `session_search` is unaffected. Misses worth keeping are copied into the eval set before then (section 3.8).

## 5. Guards

### 5.1 The principle

A rule in a prompt only asks. The model can skip a lookup that a prompt requests, and nothing visible happens when it does. A guard makes the skipped lookup fail where it can be seen. Every behaviour this project depends on is backed by a guard, or it is treated as a request the model may ignore.

### 5.2 The hook points

| Hook | When it runs | What a guard does there |
|---|---|---|
| `pre_llm_call` | Once per turn, before the model is first called | Classifies the message and adds a reminder to it. It can also end the turn. |
| `pre_tool_call` | Before each tool call | Checks the call. It can block it, by returning `{"action": "block", "message": ...}`, or send it to Hermes's approval gate, by returning `{"action": "approve", "message": ...}`.[15] |
| `post_tool_call` | After a tool call ends | Records what happened. Hermes also calls it for a call that was blocked or that raised, with a `status` that says so, and ignores its return value, so it can record but not enforce.[14][15] |
| `transform_tool_result` | After a tool returns | Replaces the result. To add a note, the guard returns the original result with the note appended. |
| `transform_llm_output` | On the final reply | Replaces the reply with a notice. Registered with `hold_stream=True`, so the reply is held until the guard has run. |

```mermaid
sequenceDiagram
    participant U as User
    participant G as Guards
    participant M as Model
    participant T as Tools
    U->>G: message
    G->>G: classify by pattern
    G->>M: message with a reminder, if a lookup is required
    loop until the model answers
        M->>G: tool call
        G->>G: allow, block, or ask for approval
        G->>T: allowed call
        T-->>G: status, counted when ok
        T-->>M: result
    end
    M->>G: final reply
    alt the required lookup ran
        G->>U: reply as written
    else it did not
        G->>U: notice that the reply was withheld
    end
```

The reminder alone is not enough, because the model can ignore it as it ignores a prompt rule. Withholding the reply is the step that changes behaviour. It works only because the output guard registers with `hold_stream=True`; without it, a streamed reply reaches the user first and is replaced afterwards.[4]

### 5.3 The guards

| Guard | Acts when | What it forces |
|---|---|---|
| Fact gate | The user asks a checkable question | At least one lookup tool in that turn. A reply that gives a vendor procedure or a diagnosis also needs a documentation lookup. |
| Recall gate | The user asks about past work | Both the memory search and the session search. One alone does not count. |
| Verify gate | A tool call would change a managed file or setting | The session must have read it first, or the call is blocked. A reply that says what a file contains is withheld if the file was never read. |
| Write gate | A tool call matches the list of sensitive actions | A human approval. With nobody to answer, the call is blocked. |

What counts as a checkable question, a question about past work, a managed file or setting, and a sensitive action is defined in each guard's configuration file in this repository, as patterns and lists. The recall gate's file, `recall_gate.toml`, came first, with memory's recall, which reads the same patterns (#158). The gate counts a match in either of its tables, past work or open work, as a question about past work. Its past-work patterns need "we" or "I" in the past: "did you set the env var?" is about this session, which needs no memory. Changing one is a pull request like any code change, with the guard's tests updated.

The verify gate's first list (#124): Hermes's `config.yaml` and `.env`, the plugin's folder, and the built-in memory files `MEMORY.md` and `USER.md`; and the settings in OPERATIONS.md section 3, items 3 and 4. A file is changed by `write_file`, `patch` or a terminal command that names it and writes, and read by `read_file` or a terminal command that names it without writing; a setting is changed by `hermes config set` or `unset`, and read by `hermes config get` or `show`. Hermes's memory tool is not counted as a change: `memory.write_approval` stages its writes. A session's reads are kept in the plugin's local state for the 100 most recently active sessions. Its event names the listed file, or the setting and what was done to it, and never the command (#115).

The fact gate's first lists (#133): a checkable question is one of eight named patterns on the user's message (a version, a place, a state, a value, a count, support, a date, or a failure in a question); a lookup is `web_search`, `web_extract`, `read_file`, `search_files`, `terminal`, `session_search`, `skill_view`, `x_search` or `delegate_task` (a subagent's own lookups carry its session and turn), and a documentation lookup is `web_search` or `web_extract`; a reply needs a documentation lookup too when it gives a numbered procedure or a diagnosis. The memory search tool, `memory_search`, is one of the lookups (#158).

The recall gate's searches (#173): `memory_search` and `session_search`, listed in its file beside the patterns. Each is counted as a tool call; the notes memory's recall delivers with a message (section 3.5) are not a tool call, and neither is a search a subagent ran after `delegate_task`, so neither counts. The gate reads the message as Hermes hands it to recall: a message given as parts is read as its text. In `log` mode the gate records each armed turn with its table and pattern name, and an armed turn's reply with the search it lacked.

The write gate's first list of sensitive actions (#80): writes into the skills folder by any tool; writes to Hermes's own `config.yaml` and `.env` and to the plugin's folder; and terminal commands that install packages, run `systemctl`, or reach other hosts. The gate reads a `write_file` or `patch` call by the paths the tool will write, and a terminal command by pattern (section 5.6), so "any tool" means those three. Hermes's skill tool, `skill_manage`, is held by `skills.write_approval` instead (the last item of the list below). The gate's event names what it matched and no more: the path a file tool would write, the listed path a command names, or the program and verb a command pattern matched, as in `pip … install`. The rest of a command is not stored, because an argument can be a credential that no redactor knows (#115).

The guards rest on Hermes mechanisms that the table does not name:

- **The fact gate, the recall gate and the verify gate count a tool from `post_tool_call`**, and only when its `status` is `ok`. A lookup reported with any other status, such as `error` or `blocked`, does not count. The verify gate keeps its record of what a session has read in the plugin's local state (section 2).
- **The write gate asks through Hermes's own approval gate**, by returning the `approve` directive from `pre_tool_call`. The request goes to a person; Hermes's model-based approval is not on this path. Hermes blocks the call when the person denies it, when the gate errors or times out, and when nobody can answer: in a scheduled run under the default `approvals.cron_mode: deny`, and in any other non-interactive context apart from the bypasses in section 5.6.[14] Each request carries a `rule_key` for the one action pattern it matched, so a person's "always" answer covers that pattern and not the whole tool; without a key, Hermes keys the answer on the tool.[4][14] The write gate records each request it sends in `events`. A person's answer arrives after the guard has returned, so it is recorded from Hermes's `post_approval_response` hook, and every "always" answer is listed in the daily report (OPERATIONS.md section 4). When the gate decides without asking, no approval hook fires: a call approved from an earlier answer shows as the request followed by `post_tool_call` with a status other than `blocked`, and a blocked call as `post_tool_call` with the status `blocked`. Before it asks, the write gate checks whether yolo or an approval bypass is on, using both of Hermes's checks: one for yolo, including a one-shot run, and one for an approval bypass, including `approvals.mode` switched to `off` while Hermes runs. If either reports a bypass, the gate blocks the call itself instead of asking (section 5.6).[14] It also blocks outright when any of the three unattended approval modes reads as `approve` (#80).

- **The skill tool's writes wait for a person.** Hermes's own skill-writing tool, `skill_manage`, stays on: Hermes switches tools off only by toolset, and the `skills` toolset that holds it also lets the agent read skills. Hermes's `skills.write_approval` setting is on (OPERATIONS.md section 3), so every change that tool makes, any of its six actions, in a turn or in Hermes's background review, is staged until a person approves it with `/skills approve`, as a write to `MEMORY.md` or `USER.md` waits under `memory.write_approval` (#42).[14] The setting holds that tool only: other tools that write files, such as the terminal, can still change a skill's files (#80).

Hermes's memory tool enforces the size limits of `MEMORY.md` and `USER.md` itself, so there is no memory-cap guard.[6]

### 5.4 How a guard is built

1. **Detect from the input, cheaply.** A pattern match on the message decides before the model answers. No model judgment is involved.
2. **Remind in the turn**, and keep the requirement in front of the model until it is met.
3. **Count the tool by name once it has run**, from `post_tool_call` with the status `ok`, including tools called inside a batch.
4. **Enforce at the output.** Block the call or withhold the reply.
5. **Record every decision.** Each arm, block, withhold and approval is logged and written to `events`.
6. **Ship in log mode first.** Each guard has three modes: `on`, `log` (detect and record only) and `off`, set in the guard's configuration file in this repository, so a mode changes only through a reviewed pull request. A guard moves from `log` to `on` after a week of real use; its pull request cites the guard's counts from `events`, with the triggers the maintainer judged false.

### 5.5 Failure behaviour

- **At startup.** Hermes does not stop for a plugin that fails to load: it is skipped with a startup warning, and the others keep loading.[1] A plugin cannot make Hermes refuse to start, because the loader catches an error raised while a plugin loads, and no plugin hook fires at startup.[16] The check therefore runs outside Hermes. The service that keeps the Hermes gateway running on an agent host runs the build's startup check first, and does not start Hermes when the check fails. The check loads the plugin in a process of its own, the way a host does: through Hermes's plugin discovery with the plugin enabled, and through its memory-provider loader. It is a `hermes` subcommand that the plugin's ordinary load registers, `hermes custom-build check`, so it is installed and updated with the plugin, and Hermes runs it in its own process, on its own interpreter and packages: the service calls it through the same `hermes` command that starts the gateway.[14] It runs through the job wrapper (section 6.1): a crash in it is recorded as for a scheduled job, and a failed check records the code of the condition that failed. It reads each Hermes setting through the function Hermes itself uses for it, such as Hermes's approval-mode readers, so it sees the value Hermes acts on: a plain read of the configuration file would miss, for example, an unquoted `off`, which Hermes's YAML loader reads as false.[14][16] It passes only when:

  - the provider loads, and every guard's hooks are registered (`guard.not_loaded`); a failed ordinary load leaves no subcommand to run (below);
  - the build's two settings are present (`config.missing`): the machine's name, `plugins.entries.custom_build.settings.machine_name` in `config.yaml`, and the store's connection string, the secret `CUSTOM_BUILD_DATABASE_URL` in the profile's `.env`. A setting whose value is not text with more than whitespace in it, such as an unquoted number or date, counts as missing (#71). With either missing there is nowhere to record, so the check logs the code and `events.write_failed`;
  - Hermes's own settings have the values the build depends on (`config.unsafe`): `memory.provider` is `custom_build`; `approvals.mode` is not `off`; `approvals.cron_mode`, `approvals.single_query_mode` and `approvals.unattended_mode` read as `deny`, because Hermes treats several other spellings as approve; `HERMES_YOLO_MODE` is not set; `compression.in_place` is `true`; `plugins.hook_callback_timeout` is 30 seconds or more (#69); the host has one Hermes profile (#63), `gateway.auto_multiplex_migration` reads as false and `gateway.multiplex_profiles`, or `GATEWAY_MULTIPLEX_PROFILES`, does not read as true (#85); and `memory.write_approval` and `skills.write_approval` read as on, and redaction is on by both `security.redact_secrets` and `HERMES_REDACT_SECRETS` (#81).[14]
  - the store is at the migration the plugin was built against, or a later one (`db.behind`): the level in `memory.schema_level` is not below the plugin's own number (section 4.3). A store with no such table is before migration 0006. A plugin installed ahead of its migrations therefore does not start the gateway; a later store passes, because each migration works with the plugin deployed before it (CONTRIBUTING.md rule C10);

  Plugin isolation is enforced by the check's absence: under `host`, Hermes registers no subcommand for the plugin, so `hermes custom-build check` does not exist, exits non-zero, and the service does not start Hermes. The same happens when the ordinary load fails, because Hermes then drops everything the plugin registered, or when the plugin is not enabled. Nothing can be recorded in `events` then, and the host is caught by its missing heartbeat (section 6.3).

  It is a check before the start, not proof about the running process (section 5.6).
- **A missing setting inside Hermes.** The plugin's `pre_llm_call` hook checks the build's settings on every turn. When one is missing or invalid, it blocks the turn with a notice that names the setting (`config.missing`), so the agent never runs a turn with the build half configured. Here and in the startup check, a setting is invalid when its value is not text with more than whitespace in it, or, for the connection string, when PostgreSQL's client library cannot parse it (#71, #83). Hermes keeps a blocked turn's message only as a placeholder, so the message must be sent again once the setting is fixed.[4] A scheduled job that finds a setting missing stops with the same code. The manifest declares no settings for Hermes to check: Hermes would only log a warning for a bad `config_schema` value, or disable a plugin whose `requires_env` variable is unset, and start anyway.[15]
- **Each guard finishes well inside Hermes's hook deadline.** Hermes bounds a hook by `plugins.hook_callback_timeout`, 30 seconds by default. A `pre_llm_call` callback that runs past it is skipped, so the turn goes ahead, and Hermes then suppresses that callback for 60 seconds. Each guard therefore keeps its whole run, network calls included, under 10 seconds, and the startup check fails when the setting is below 30 seconds (#69).[14] The write gate's hook writes its events from a worker thread, because a store that stops answering after the login is bounded by nothing the store enforces (#107). It waits on the store only to record a crash in the hook itself (#117).
- **During a turn**, Hermes isolates hook errors, and the direction depends on the hook:[4]

  | Hook | When the callback raises or times out |
  |---|---|
  | `pre_tool_call` | The tool call is blocked (fails closed) |
  | `pre_llm_call` | The error is logged and the turn goes ahead (fails open) |
  | `transform_llm_output` | The error is logged and the original reply is delivered (fails open). With `hold_stream=True`, the reply is withheld instead. |
  | `transform_tool_result` | The error is logged and the callback is skipped (fails open) |

  Hermes logs the first such error at WARNING and identical repeats at DEBUG, so a guard that keeps failing goes quiet in Hermes's own log.

  The plugin developer guide says in general that a hook callback that crashes is logged and skipped.[15] That does not describe `pre_tool_call`: in v0.23.1 a callback that raises or times out there produces a block, and for a raise the block names the callback.[14]
- **Every enforcing guard fails closed, and every failure is recorded.** Each guard's entry point goes through the boundary wrapper (section 2), which records `guard.crash`, with the traceback in the log, and then re-raises or blocks as the table in section 2 shows. Returning a block from `pre_llm_call` is what Nous recommends for policy hooks.[4] Recording happens in the wrapper on every occurrence, so it does not depend on Hermes logging a repeated error only once.
- **`transform_tool_result` is never used to enforce.** Hermes skips a callback that raises there, so a guard on it fails open. It is used only to annotate a tool result, and its failures are recorded by the wrapper like any other.

### 5.6 Known limits

- Detection is by pattern. A question phrased outside the patterns gets no reminder, no wider search and no open-items answer.
- A lookup check proves that a lookup happened, not that it was the right one.
- While a reply is held, tool progress and reasoning still show unless turned off, and commentary between tool calls is dropped.[4]
- Any exemption, such as for subagents or system notifications, is written down. An exempt path produces unguarded claims.
- A Hermes started by hand, outside the service, skips the startup check (section 5.5). Hermes logs a warning for a plugin that failed to load, and nothing in Hermes stops it; if the guards failed to load while the memory provider loaded, the provider records it (next item).
- The startup check runs in a process of its own before Hermes starts. It does not prove the load in the process that serves requests, where a plugin's load has its own deadline and plugins can be reloaded while Hermes runs.[16] If the ordinary load fails there while the provider loads, the provider runs with no guards. The provider reports it: when it starts, it reads the `custom_build` entry in Hermes's plugin list (`list_plugins()`), and if that entry is missing, or shows an error or no registered hooks, it records `guard.not_loaded` and every recall it returns carries a notice that the guards are off. It checks once, when it starts, so a reload that fails later, while Hermes runs, is not caught.[16] A missing entry is what a gateway started by hand with `HERMES_SAFE_MODE` in its own environment shows: it skips plugin discovery, so the ordinary load never happens, while the memory provider still loads (#29).[16] A CLI or TUI launched with that variable, or with `--safe-mode`, does not take this path: it creates a safe-mode session (next item), and a gateway the CLI starts for itself drops the variable.[14]
- Two kinds of gateway session run with no part of the build. A session created in safe mode runs its turns in a worker process where Hermes starts no memory provider and runs no plugin hook. A session created with the profile's settings ignored runs on Hermes's code defaults, which do not select the build. The build can neither block such a session nor act in it, so the heartbeat job records each one after the fact as `guard.bypassed`, up to 6 hours late, from the launch policy Hermes keeps in its state database (#23). Only local sessions carry these flags: from the CLI, the terminal UI, the desktop app or an editor client over ACP. A policy the heartbeat cannot read is recorded too, because nothing shows the build ran in that session. Limits: a session deleted, or handed off to a messaging channel, before the heartbeat's next run takes its policy with it and is not recorded; once retention deletes a session's `guard.bypassed` row after 90 days, a session whose policy is still kept is recorded again (#95); and two heartbeats running at the same moment on one host can each record the same session.[14]
- Some of the build rests on Hermes behaviour that the developer guide does not describe: `kind: standalone` on a memory provider's directory and the two imports it causes (section 1), `register()` telling the two loads apart by its context, the startup check running as a plugin subcommand and calling Hermes's plugin loader and approval-mode readers directly, the memory provider reading Hermes's plugin list to find whether the guards loaded, the write gate's checks for an approval bypass, the write gate reading which paths a `write_file` or `patch` call will write the way those tools resolve them, and the heartbeat reading each local session's launch policy from Hermes's state database. Each is re-tested on every Hermes upgrade (OPERATIONS.md section 5).
- Hermes's approval gate approves without asking in four cases, and the write gate does not hold in them:[14]
  - yolo is on, by any route: the `--yolo` flag, the `HERMES_YOLO_MODE` variable, the `/yolo` command in a CLI or gateway session, or a one-shot run started with `hermes -z`;
  - `approvals.mode` is `off`;
  - the person earlier answered "for this session" or "always" for the same rule, which is the whole tool unless the guard names a narrower rule;
  - an unattended context has its mode set to `approve` (`approvals.cron_mode`, `approvals.single_query_mode`, `approvals.unattended_mode`; each defaults to `deny`).

  The build answers each. The startup check fails on `approvals.mode: off`, on any of the three unattended modes that does not read as `deny`, and on `HERMES_YOLO_MODE` (section 5.5). During a turn, the write gate blocks outright when Hermes reports yolo or an approval bypass for the session. That also covers yolo turned on by the `--yolo` flag on the command that starts the gateway, or restored from an earlier `/yolo`, neither of which the startup check can see. An earlier answer covers only one action pattern, and every "always" answer is reported (section 5.3). One of the three unattended modes switched to `approve` while Hermes runs, which the startup check cannot see, is caught by the write gate, which blocks outright when any of them reads as `approve` (section 5.3, #80).[14]
- The write gate matches terminal commands by pattern, so a script that writes a file through its own code is not seen.
- Two other tools run commands the write gate does not see (#114).[14]
  - `execute_code` runs a Python script. A tool the script calls through Hermes, such as `terminal()`, goes through Hermes's dispatch, so the gate sees it. A command the script runs itself, with `subprocess` or `os.system`, passes through no hook. Hermes has its own check of the whole script, and it seldom reaches a person. In a messaging session on the gateway, under the default `approvals.mode: smart`, Hermes's approval model decides first and a person is asked only when it escalates or errors; an earlier "session" or "always" answer covers later scripts. There is no whole-script check in an interactive CLI, in a local desktop session, or when the terminal backend is a sandbox. In an unattended run, under the `deny` modes the build requires (section 5.5), Hermes blocks the script outright.
  - `process_manage` with the action `write` or `submit` sends text to the input of a process the terminal started in the background. A shell started that way runs whatever it is sent, and the gate sees only the command that started it.

  So a package install, a `systemctl` call or a command that reaches another host can run by either route without the gate recording it in `log` mode or, later, stopping it in `on` mode.

## 6. Failure handling

Every point of failure has a name, and a failure, an empty result and a fallback can always be told apart. The rules are F1–F6 in CONTRIBUTING.md; this section describes the mechanism.

### 6.1 The failure catalog

The catalog is one module in the package. It lists every failure code; beside it, `SEVERITY` gives each failure code's severity (high, medium or low) and `SHOWN_IN_TURN` names the codes the build shows in a turn, and the daily report prints both (#35). A test fails the build if a failure code has no severity. The catalog also lists the events that are not failures, such as `host.heartbeat`, because the recording helper takes only codes in the catalog (CONTRIBUTING.md rules L3 and F6). A test fails the build if any handler or fallback emits a code that is not in it, or emits none.

Two wrappers in the same module record what no handler caught. The boundary wrapper covers every entry point Hermes calls (section 2). The job wrapper covers every scheduled job that runs on a host: on an unexpected exception it records `unhandled` and `job.failed`, then exits non-zero, so Hermes cron also sends its own error alert (OPERATIONS.md section 4). The startup check (section 5.5) runs through the job wrapper too: a crash in it is recorded as `unhandled` and `job.failed`, and the check exits non-zero. These two wrappers are the only code allowed to catch everything. Both log the traceback; the `events` row carries only the exception's type and message as its reason, because a traceback can carry content (CONTRIBUTING.md rule L5).

Initial codes:

| Code | Raised when |
|---|---|
| `embed.unavailable` | The embedding service cannot be reached |
| `embed.model_mismatch` | This machine's embedding build differs from the one recorded in the store |
| `db.unreachable`, `db.auth`, `db.query` | The memory store cannot be reached, refuses the role, or rejects a query |
| `db.behind` | The startup check found the store behind the migrations the plugin needs (section 5.5) |
| `recall.timeout` | Recall misses its deadline |
| `judge.timeout`, `judge.error` | The judge model times out or errors |
| `rerank.fallback` | The judge could not rerank, and the pool kept its fused order (degraded) |
| `action.skipped` | Not a failure: the action query was skipped, because the judge read the message as asking for no work (section 3.5) |
| `store.gate_error` | The judge failed during a write; the fact is queued |
| `extract.error` | The extraction model failed or answered something other than a list of facts |
| `extract.refused` | The extraction model's provider refused the account: logged out, billing, or forbidden |
| `digest.error` | The digest model failed or answered something other than a digest |
| `digest.refused` | The digest model's provider refused the account: logged out, billing, or forbidden |
| `guard.bypassed` | The heartbeat found a local session started in safe mode or with the profile's settings ignored, which ran with no part of the build, or one whose launch policy it cannot read (section 5.6) |
| `guard.crash`, `guard.not_loaded` | A guard raised during a turn (recorded instead of `unhandled`), or the startup check found a guard's hooks or the provider not loaded, or the memory provider found, when it started, that the guards had not loaded |
| `write_gate.would_ask`, `write_gate.would_block` | Not failures: in `log` mode, the write gate saw a call on its list that `on` would send to a person for approval, or would block outright (section 5.3) |
| `verify_gate.would_block`, `verify_gate.would_withhold` | Not failures: in `log` mode, a call would change a listed file or setting the session never read, or a reply stated a listed file's contents the session never read (section 5.3) |
| `fact_gate.armed`, `fact_gate.would_withhold` | Not failures: in `log` mode, the user asked a checkable question, or its reply had no lookup, or gave a procedure or diagnosis with no documentation lookup (section 5.3) |
| `recall_gate.armed`, `recall_gate.would_withhold` | Not failures: in `log` mode, the user asked about past work or open work, or the reply came without both the memory search and the session search (section 5.3) |
| `config.unsafe` | The startup check found a Hermes setting with a value the build does not support (section 5.5) |
| `config.missing` | A setting the build needs is missing or invalid: the startup check fails, a scheduled job stops, and inside Hermes every turn is blocked (section 5.5) |
| `redact.failed` | The redactor raised; the text is not stored or sent |
| `memory.write_rejected` | Hermes refused a built-in memory write |
| `events.write_failed` | An `events` row could not be written; logged locally at ERROR |
| `job.failed`, `job.missed`, `job.no_output` | A scheduled job failed or the startup check crashed; a scheduled job did not run, or took input and produced nothing |
| `job.succeeded` | Not a failure: a scheduled job's run finished. Its `detail` names the job as `job` and, where the job has them, its counts as `input` and `output`; the job-health check reads those three keys (section 6.3) |
| `host.missing` | An agent host on the job-health check's list wrote no heartbeat in the last 24 hours |
| `host.heartbeat` | Not a failure: the heartbeat job on an agent host writes one every 6 hours (section 6.3) |
| `unhandled` | Anything not in the catalog, outside a guard. Recorded by the boundary wrapper or the job wrapper, with its traceback in the log. |

### 6.2 Where a failure appears

| Place | What appears |
|---|---|
| `errors.log` (Hermes) | Every occurrence, at WARNING or above |
| `events` table | Every occurrence, with its code, result and reason |
| The turn | A short notice, when the failure changed the answer |
| The daily failure report | Every failure of the day, grouped by code and machine |

A retry that succeeds is recorded as `degraded` with its retry count, so a flaky dependency shows up as a trend before it becomes an outage.

Retention is an exception to the first two rows. It runs inside the database, so a failed run rolls back: its error is in `pg_cron`'s run history, and `events` shows it as `job.missed` at the next job-health check (section 4.3).

### 6.3 Absence checks

Some failures raise nothing. A daily check flags any scheduled job other than the heartbeat and the check itself with no `job.succeeded` event in the last 24 hours (`job.missed`), and any job that processed input but produced no output for more than one run (`job.no_output`). The daily report is itself checked by its absence: a day with no report is treated as a failure (OPERATIONS.md section 4). A failed startup check is recorded in `events`, so it reaches the daily report when the host can reach the store. A host that cannot reach the store, or does not run at all, is caught by its absence: a heartbeat job on every agent host writes one `host.heartbeat` event every 6 hours, so one late or missed beat does not flag a live host (#43), and the job-health check, which has the list of agent hosts in its configuration, records `host.missing` for any host with no heartbeat in the last 24 hours.

## 7. Logging

Hermes owns logging for everything that runs inside it.[14]

- It writes `agent.log`, `errors.log` (WARNING and above) and, in gateway mode, `gateway.log` under `~/.hermes/logs/`.
- It rotates them: 5 MB and 3 backups by default, set by `logging.max_size_mb` and `logging.backup_count`.
- It passes every line through a redacting formatter.
- `hermes logs` filters by session, component and time.

What Hermes does not provide, and the build adds:

- **Supabase API key formats.** Hermes's redactor has no pattern for `sb_secret_` or `sb_publishable_` keys, so the build registers them through `register_redaction_patterns`.
- **The build's own redaction is forced.** Before anything is stored or sent, the build calls Hermes's redactor with `force=True`, which applies even on a host where `security.redact_secrets` is off (#81).[14]
- **Redaction outside logs.** Hermes redacts log lines, and the messages, queries, transcripts and tool arguments it hands a memory provider.[14] The build redacts every value itself before it is written to the store or sent off the host, whatever Hermes did before.
- **Structure.** Hermes's lines are plain text. Each event the build logs is one line whose message is a single JSON object, written by one helper in the failure module that both logs the line and writes the `events` row, so the two always use the same fields.
- **Durability.** Logs rotate and live on one machine. Decisions and failures also go to `events`, which every machine shares.

The rules are L1–L5 in CONTRIBUTING.md.

## Sources

- [1] [Plugins (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
- [2] [Memory providers (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)
- [3] [Memory provider plugins (Hermes Agent developer guide)](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin)
- [4] [Hooks (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
- [5] [Context files (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)
- [6] [Memory (Hermes Agent documentation)](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [7] [HNSW indexes (Supabase Docs)](https://supabase.com/docs/guides/ai/vector-indexes/hnsw-indexes)
- [8] [Qwen3-Embedding-4B model card (Hugging Face)](https://huggingface.co/Qwen/Qwen3-Embedding-4B)
- [9] [Park et al., Generative Agents: Interactive Simulacra of Human Behavior, arXiv 2304.03442](https://arxiv.org/abs/2304.03442)
- [10] [pgvector README: filtering and iterative index scans](https://github.com/pgvector/pgvector)
- [11] [Controlling text search (PostgreSQL documentation)](https://www.postgresql.org/docs/current/textsearch-controls.html)
- [12] [Wu et al., LongMemEval, arXiv 2410.10813](https://arxiv.org/abs/2410.10813)
- [13] [Row security policies (PostgreSQL documentation)](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [14] Hermes v0.23.1 source: `hermes_logging.py`, `hermes_state_messages.py`, `hermes_state_mutation_transcript.py`, `hermes_yaml.py`, `model_tools.py`, `agent/agent_init.py`, `agent/agent_init_memory_provider.py`, `agent/managed_worker.py`, `agent/memory_manager.py`, `agent/memory_provider.py`, `agent/message_metadata.py`, `agent/redact.py`, `agent/safe_worker_policy.py`, `agent/turn_context.py`, `gateway/session_finite.py`, `gateway/session_local.py`, `gateway/session_managed_worker.py`, `hermes_cli/cli_commands_mixin.py`, `hermes_cli/cli_commands_session_tools.py`, `hermes_cli/cli_session_mixin.py`, `hermes_cli/config_defaults.py`, `hermes_cli/gateway_chat.py`, `hermes_cli/gateway_runtime_start.py`, `hermes_cli/main.py`, `hermes_cli/plugins.py`, `hermes_cli/plugins_context.py`, `hermes_cli/plugins_dispatch.py`, `tools/approval.py`, `tools/code_execution_rpc.py`, `tools/code_execution_tool.py`, `tools/process_registry.py`, `tools/approval_context.py`, `tools/approval_yolo.py`, `plugins/memory/__init__.py`, `toolsets.py`, `tools/skill_manager_tool.py`, `acp_adapter/gateway_server.py`, `gateway/session_policy.py`, `hermes_state_local.py`, `hermes_state_mutation_retirement.py`
- [15] [Build a Hermes plugin (Hermes Agent developer guide)](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [16] Hermes v0.23.1 source, plugin loading: `agent/prompt_builder.py`, `hermes_cli/plugin_isolation.py`, `hermes_cli/plugins.py`, `hermes_cli/plugins_cmd_install.py`, `hermes_cli/plugins_content.py`, `hermes_cli/plugins_discovery.py`, `hermes_cli/plugins_ledger.py`, `hermes_cli/plugins_loader.py`, `hermes_cli/plugins_manifest.py`, `plugins/memory/__init__.py`, `plugins/plugin_storage.py`
- [17] [xiaowu0162/longmemeval-cleaned (Hugging Face dataset card)](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
- [18] [Cron quickstart: scheduling jobs with pg_cron (Supabase documentation)](https://supabase.com/docs/guides/cron/quickstart)
