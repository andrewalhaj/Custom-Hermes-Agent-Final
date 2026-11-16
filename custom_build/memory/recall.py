"""Delivery: the recalled notes prefetch() attaches to a turn (ARCHITECTURE.md 3.5 points 3-6, #158).

Notes are labelled as notes, not instructions, each with its origin, basis and dates. A failure is
reported with its code, never as "no notes found". A question about past work widens the search; one about
open work is answered from the newest digests' open items, by the recall gate's own patterns.
"""

import pathlib
import re
import tomllib

# The recall gate's file (5.3): the search and the gate read the same patterns, so they agree.
PATTERNS = pathlib.Path(__file__).resolve().parent.parent / "guards" / "recall_gate.toml"
OPEN_ITEMS = "Open items:"   # how a digest's open items are written (digest.py)


def question_kind(message):
    """"open_work", "past_work" or None for the user's message; open work is checked first."""
    patterns = tomllib.loads(PATTERNS.read_text(encoding="utf-8"))
    for kind in ("open_work", "past_work"):
        if any(re.search(pattern, message) for pattern in patterns[kind].values()):
            return kind
    return None


def open_items(digests):
    """Each digest's open items as notes of their own, with the digest's provenance, in the digests' order."""
    items = []
    for digest in digests:
        _, found, tail = digest["body"].partition(OPEN_ITEMS)
        for line in tail.splitlines() if found else []:
            if line.strip().startswith("- "):
                items.append({**digest, "body": line.strip()[2:].strip()})
    return items


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
        # Provenance first, so a digest's open items, indented under it, are not read as the tag's subject.
        body = result["body"].replace("\n", "\n    ")
        lines.append(f"- [{', '.join(provenance)}] {body}")
    return "\n".join(lines)


def failed(code):
    """What a turn is told when recall could not search: the code, and that this is not an empty result."""
    return f"Recall failed ({code}): memory could not be searched this turn, so its notes are unknown, not empty."
