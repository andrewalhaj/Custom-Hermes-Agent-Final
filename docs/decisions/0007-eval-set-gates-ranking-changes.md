# 0007. The eval set gates every ranking change

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decided by:** Andrew Alhaj

## Context

Several planned improvements change how notes are ranked or which stores exist:

- a recency term after the rerank
- an OR keyword query for long questions
- a smaller note budget
- Qwen's query instruction
- bge-reranker-v2-m3 as primary reranker instead of the hosted judge
- Hermes's built-in `session_search` instead of the sessions store

Each one could help or hurt. Rule C8 already says retrieval changes come with numbers, but there was no question set to produce them.

## Decision

- **The eval set exists before any ranking change ships.**
- **It starts from LongMemEval-S,** which covers updates, time questions and abstention.
- **It grows from real misses** recorded in the `events` table: "what's left?" failures, wrong-project recalls, recall that found nothing, and questions whose right answer is "no note".
- **Each question records** the project and the expected fact or chunk IDs.
- **Reports give** recall@5, MRR and an abstention score.
- **The two replacements are not settled design.** bge as primary reranker and `session_search` instead of the sessions store are decided by these numbers.

## Consequences

- **Ranking changes wait for the eval set.**
- **CONTRIBUTING.md section 4.6 marks new values "set by eval set"** instead of guessing numbers.
- **The `events` table must record enough to rebuild a miss:** the query hash, the session and turn, and the returned IDs with scores.

## Sources

- LongMemEval (arXiv 2410.10813); LoCoMo (arXiv 2402.17753).
- CONTRIBUTING.md rule C8.
