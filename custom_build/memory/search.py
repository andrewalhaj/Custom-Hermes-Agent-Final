"""The facts search (ARCHITECTURE.md 3.4 steps 1-3 and 6, #158): embed the query, then one SQL function,
memory.search_facts, finds and fuses. Until 4b reranks, every result is ranked by fusion alone."""

from . import write_path


def search(conn, embedder, query, project, *, wanted, timeout):
    """The top `wanted` results, each with its provenance and which searches found it.

    Raises embedding's EmbedUnavailable and ModelMismatch, and psycopg's errors.
    """
    return candidates(conn, embedder, query, project, wanted=wanted, timeout=timeout)[:wanted]


def candidates(conn, embedder, query, project, *, wanted, timeout):
    """The whole fused candidate pool for `wanted` results (3.6), best first, for delivery's slot rules."""
    limits = write_path.load()["recall"]
    [vector] = embedder.embed(conn, [query], timeout=timeout)
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
