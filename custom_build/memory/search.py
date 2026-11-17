"""The facts search (ARCHITECTURE.md 3.4 steps 1-4 and 6, #158, #165): embed the query, then one SQL function,
memory.search_facts, finds and fuses; the judge then reranks, or the fused order stands."""

from . import judge, write_path


def search(conn, embedder, query, project, *, wanted, timeout):
    """The top `wanted` results, each with its provenance and which searches found it.

    Raises embedding's EmbedUnavailable and ModelMismatch, and psycopg's errors.
    """
    return candidates(conn, embedder, query, project, wanted=wanted, timeout=timeout)[:wanted]


def reranked(results, message, judge_at, *, timeout):
    """The results reordered by the judge's probability that each helps answer the message, and cut at the
    score floor (3.4 step 4, 3.6). A stable sort, so equal scores keep the fused order. `timeout` bounds the
    judge's answer. Raises the judge's JudgeError and JudgeTimeout, for the caller to fall back to the fused
    order."""
    if not results:
        return results
    scores = judge.relevance_answers(message, [result["body"] for result in results], url=judge_at.url,
                                     key=judge_at.key, model=judge_at.model, timeout=timeout)
    scored = sorted(({**result, "ranked_by": "judge", "score": score} for result, score in zip(results, scores)),
                    key=lambda result: result["score"], reverse=True)
    floor = write_path.load()["rerank"]["floor"]
    return [result for result in scored if result["score"] >= floor]


def newest_digests(conn, project, count):
    """The project's newest `count` live digests, newest first (3.5 point 4): not a similarity search."""
    rows = conn.execute(
        "select body, kind, origin, basis, created_at, last_verified_at from memory.facts"
        " where kind = 'digest' and superseded_by is null and retired_at is null and project is not distinct from %s"
        " order by created_at desc, id desc limit %s", (project, count)).fetchall()   # id: ties keep an order
    return [{"body": body, "kind": kind, "origin": origin, "basis": basis, "stored": created.date().isoformat(),
             "last_verified": verified.date().isoformat() if verified else None, "found_by": ["newest"],
             "ranked_by": "newest"}
            for body, kind, origin, basis, created, verified in rows]


def candidates(conn, embedder, query, project, *, wanted, timeout):
    """The whole fused candidate pool for `wanted` results (3.6), best first, for delivery's slot rules."""
    limits = write_path.load()["recall"]
    # Only the query's first characters are embedded, so it finishes inside recall's limit on a CPU-only host;
    # the word search below still reads the whole message (#238).
    [vector] = embedder.embed(conn, [query[:write_path.load()["embed"]["recall_chars"]]], timeout=timeout)
    pool = max(limits["pool_factor"] * wanted, limits["pool_min"])
    rows = conn.execute(
        "select body, kind, origin, basis, created_at, last_verified_at, by_meaning, by_words"
        " from memory.search_facts(%s::halfvec, %s, %s, %s, %s)",
        (str(vector), query, project, pool, limits["fusion_k"])).fetchall()
    return [{"body": body, "kind": kind, "origin": origin, "basis": basis, "stored": created.date().isoformat(),
             "last_verified": verified.date().isoformat() if verified else None,
             "found_by": [name for name, rank in (("meaning", meaning), ("words", words)) if rank is not None],
             "ranked_by": "fusion"}
            for body, kind, origin, basis, created, verified, meaning, words in rows]
