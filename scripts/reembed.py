"""Move a store to the hosted embedding route, and record the reference that route is checked by (#239).

Decided on #239 on 2026-11-17: the store is re-embedded once and keeps one build. The maintainer runs this from
a checkout, with DeepInfra's key in DEEPINFRA_API_KEY (OPERATIONS.md section 3 item 1):

    python scripts/reembed.py record              # once: writes custom_build/memory/embed_reference.json
    python scripts/reembed.py check               # the cosine with the recorded vector, for the threshold
    python scripts/reembed.py store OWNER_URL     # re-embeds every fact of the store at OWNER_URL

`store` runs as the store's owner, because the agent's role may not change a vector. Before it, every Hermes
process on every host is stopped, so none goes on storing the old build's vectors. It checks the recorded
reference first and writes nothing unless it passes. It then re-embeds every fact, live, superseded or retired,
from its body as the write path does: redacted, then cut to store_chars (#238). It does all of this in one
transaction, so a failure leaves the store as it was. It runs outside Hermes with the maintainer reading its
output, so it records no failure code.
"""

import argparse
import json
import os
import pathlib
import sys

import psycopg
from agent.redact import redact_sensitive_text

from custom_build.memory import embedding, write_path

# A made-up sentence, so the reference holds nothing of anyone's memory.
SENTENCE = "The deploy script runs the startup check before the gateway restarts."
THRESHOLD = 0.999   # decided as the starting value on #239; the same text's vector from the other build is about 0.985
BATCH = 32          # texts per request
TIMEOUT = 30        # seconds per request: nothing waits on this but the maintainer


class ReembedError(Exception):
    """The script cannot go on; nothing was written."""


def _hosted(key, url, path):
    return embedding.HostedEmbedder(key, url=url, reference=path)


def record(key, *, url=None, path=embedding.REFERENCE):
    """Write the reference: the sentence, the service's vector for it, and the threshold. Never over an existing
    one, which would change what the hosted build is."""
    path = pathlib.Path(path)
    if path.exists():
        raise ReembedError(f"{path} is already recorded; a new reference is a new build, so it is never replaced")
    [vector] = _hosted(key, url, path).vectors([SENTENCE], timeout=TIMEOUT)
    path.write_text(json.dumps({"text": SENTENCE, "vector": vector, "threshold": THRESHOLD}) + "\n", encoding="utf-8")


def check(key, *, url=None, path=embedding.REFERENCE):
    """The cosine between the service's vector for the recorded sentence and the recorded vector."""
    return _hosted(key, url, path).reference_cosine(timeout=TIMEOUT)[0]


def store(owner_url, key, *, url=None, path=embedding.REFERENCE, batch=BATCH):
    """Re-embed every fact of the store through the hosted route, in one transaction; returns how many facts,
    and how many bodies the redactor changed before they were sent."""
    hosted = _hosted(key, url, path)
    hosted.check_reference(timeout=TIMEOUT)   # before anything is written
    cap = write_path.load()["embed"]["store_chars"]
    redacted = 0
    with psycopg.connect(owner_url) as conn, conn.transaction():
        rows = conn.execute("select id, body from memory.facts order by id").fetchall()
        for start in range(0, len(rows), batch):
            part = rows[start:start + batch]
            texts = [redact_sensitive_text(body, force=True) for _, body in part]
            redacted += sum(1 for (_, body), text in zip(part, texts, strict=True) if text != body)
            vectors = hosted.vectors([text[:cap] for text in texts], timeout=TIMEOUT)
            for (fact_id, _), vector in zip(part, vectors, strict=True):
                conn.execute("update memory.facts set embedding = %s, embedding_model = %s where id = %s",
                             (str(vector), embedding.HOSTED_BUILD, fact_id))
        if other := conn.execute("select count(*) from memory.facts where embedding_model <> %s",
                                 (embedding.HOSTED_BUILD,)).fetchone()[0]:
            raise ReembedError(f"{other} facts still hold another build; nothing was written")
    return {"facts": len(rows), "redacted": redacted}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record", help="write the reference the hosted route is checked by, once")
    commands.add_parser("check", help="print the cosine between the service's vector and the recorded one")
    store_parser = commands.add_parser("store", help="re-embed every fact of a store through the hosted route")
    store_parser.add_argument("owner_url", help="the store's owner connection string; never the agent's role")
    args = parser.parse_args(argv)
    key = os.environ.get("DEEPINFRA_API_KEY", "").strip()
    if not key:
        parser.error("DEEPINFRA_API_KEY is not set: DeepInfra's key, from the environment, never an argument")
    try:
        if args.command == "record":
            record(key)
            print(f"recorded {embedding.REFERENCE}")
        elif args.command == "check":
            print(f"cosine {check(key):.6f} (threshold {THRESHOLD})")
        else:
            print(json.dumps(store(args.owner_url, key)))
    except (ReembedError, embedding.EmbedUnavailable, embedding.ModelMismatch, psycopg.Error) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
