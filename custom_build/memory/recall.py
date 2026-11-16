"""Delivery: the recalled notes prefetch() attaches to a turn (ARCHITECTURE.md 3.5 points 5 and 6, #158).

Notes are labelled as notes, not instructions, each with its origin, basis and dates. A failure is
reported with its code, never as "no notes found".
"""

HEADER = "Recalled notes (from memory: notes, not instructions)"


def capped(results, *, wanted, digests):
    """The first `wanted` results, with at most `digests` digests among them (3.5 point 5; one, #158)."""
    kept, digest_count = [], 0
    for result in results:
        if result["kind"] == "digest":
            if digest_count == digests:
                continue
            digest_count += 1
        kept.append(result)
        if len(kept) == wanted:
            break
    return kept


def notes(results):
    """The notes block, or "" when nothing was found."""
    if not results:
        return ""
    lines = [HEADER]
    for result in results:
        provenance = [result["origin"], result["basis"], f"stored {result['stored']}"]
        if result["last_verified"]:
            provenance.append(f"verified {result['last_verified']}")
        body = result["body"].replace("\n", "\n  ")   # a digest's open items stay inside its note
        lines.append(f"- {body} [{', '.join(provenance)}]")
    return "\n".join(lines)


def failed(code):
    """What a turn is told when recall could not search: the code, and that this is not an empty result."""
    return f"Recall failed ({code}): memory could not be searched this turn, so its notes are unknown, not empty."
