"""Move a store between the embedding routes, and record the reference the hosted one is checked by (#239, #261).

Decided on #239 on 2026-11-17: the store is re-embedded once and keeps one build. The maintainer runs this
from a checkout's root, with DeepInfra's key in DEEPINFRA_API_KEY (for `record`, `check` and `store`) and, for
`store`, `local` and `restore`, the store owner's connection string in CUSTOM_BUILD_OWNER_URL. Neither is ever an argument. See OPERATIONS.md section 3
item 1:

    python -m scripts.reembed record                    # once: writes custom_build/memory/embed_reference.json
    python -m scripts.reembed check                     # the cosine with the recorded vector, and its threshold
    python -m scripts.reembed store --backup FILE       # backs up the vectors, then re-embeds every fact
    python -m scripts.reembed local --ollama-url URL --backup FILE   # the same, through an Ollama, back from hosted (#261)
    python -m scripts.reembed restore FILE              # puts the backed-up vectors and builds back

`store` runs as the store's owner, because the agent's role may not change a vector. Before it, every Hermes
process on every host is stopped, so none goes on storing the old build's vectors. It checks the recorded
reference before writing anything. It writes every fact's id, vector and build to FILE, which must not exist
yet. It then re-embeds every fact, live, superseded or retired, from its body as the write path does:
redacted, then cut to store_chars (#238). It does all of this in one transaction, so a failure leaves the
store as it was. The script runs outside Hermes with the maintainer reading its output, so it records no
failure code.
"""

import argparse
import json
import os
import pathlib
import sys

import psycopg
from agent.redact import redact_sensitive_text, register_redaction_patterns

from custom_build import REDACTION_PATTERNS
from custom_build.memory import embedding, write_path

# A made-up sentence, so the reference holds nothing of anyone's memory.
SENTENCE = "The deploy script runs the startup check before the gateway restarts."
THRESHOLD = 0.999   # decided as the starting value on #239; the same text's vector from the other build is about 0.985
BATCH = 32          # texts per request
TIMEOUT = 30        # seconds per request: nothing waits on this but the maintainer
OWNER_URL = "CUSTOM_BUILD_OWNER_URL"
KEY = "DEEPINFRA_API_KEY"


class ReembedError(Exception):
    """The script cannot go on; nothing was written."""


def _hosted(key, url, path):
    return embedding.HostedEmbedder(key, url=url, reference=path)   # None: the module's, read at call time


def record(key, *, url=None, path=None):
    """Write the reference: the sentence, the service's vector for it, and the threshold. Never over an existing
    one, which would change what the hosted build is."""
    path = pathlib.Path(path or embedding.REFERENCE)
    if path.exists():
        raise ReembedError(f"{path} is already recorded; a new reference is a new build, so it is never replaced")
    [vector] = _hosted(key, url, path).vectors([SENTENCE], timeout=TIMEOUT)
    path.write_text(json.dumps({"text": SENTENCE, "vector": vector, "threshold": THRESHOLD}) + "\n", encoding="utf-8")


def check(key, *, url=None, path=None):
    """(the cosine between the service's vector for the recorded sentence and the recorded vector, the
    recorded threshold)."""
    return _hosted(key, url, path).reference_cosine(timeout=TIMEOUT)


def store(owner_url, key, *, backup, url=None, path=None, batch=BATCH):
    """Back up every fact's vector and build to `backup`, then re-embed every fact through the hosted route, in
    one transaction; returns how many facts, and the ids of those whose body the redactor changed before it was
    sent (their vector is of the redacted text)."""
    if pathlib.Path(backup).exists():
        raise ReembedError(f"{backup} already exists; a backup is never written over")
    hosted = _hosted(key, url, path)
    hosted.check_reference(timeout=TIMEOUT)   # before anything is written
    return _move(owner_url, backup, batch, lambda texts: hosted.vectors(texts, timeout=TIMEOUT),
                 embedding.HOSTED_BUILD)


def local(owner_url, *, ollama_url, backup, batch=BATCH):
    """As `store`, through the Ollama at `ollama_url` instead (#261): every fact gets that Ollama's build of the
    model, which is read before anything is written. Returns what `store` does, and the build."""
    if pathlib.Path(backup).exists():
        raise ReembedError(f"{backup} already exists; a backup is never written over")
    ollama_url = ollama_url.rstrip("/")
    build = embedding.machine_build(ollama_url, timeout=TIMEOUT)   # EmbedUnavailable when it lacks the model

    def vectors(texts):
        found = embedding._ollama(ollama_url, "/api/embed", {"model": embedding.MODEL, "input": texts},
                                  timeout=TIMEOUT)["embeddings"]
        # As the local route's embedder: a short answer is unavailable, a wrong size a different model.
        if len(found) != len(texts):
            raise embedding.EmbedUnavailable(f"Ollama returned {len(found)} vectors for {len(texts)} texts")
        if any(len(vector) != embedding.DIMENSIONS for vector in found):
            raise embedding.ModelMismatch(f"Ollama returned vectors of {sorted({len(v) for v in found})} numbers, "
                                          f"not {embedding.DIMENSIONS}")
        return found

    return {**_move(owner_url, backup, batch, vectors, build), "build": build}


def _move(owner_url, backup, batch, vectors, build):
    """Back up every fact's vector and build to `backup`, then give every fact, live or not, a vector of its body
    (redacted, then cut to store_chars, as the write path does) from `vectors`, labelled `build`, in one
    transaction that checks at the end that no fact holds another build. A failure rolls everything back and
    removes the backup, so a retry may run."""
    backup = pathlib.Path(backup)
    register_redaction_patterns(REDACTION_PATTERNS)   # the plugin's own, as its load registers them
    cap = write_path.load()["embed"]["store_chars"]
    redacted = []
    try:
        with psycopg.connect(owner_url) as conn, conn.transaction():
            rows = conn.execute("select id, body, embedding::text, embedding_model from memory.facts order by id"
                                ).fetchall()
            backup.write_text(json.dumps([{"id": str(fact_id), "embedding": vector, "embedding_model": model}
                                          for fact_id, _, vector, model in rows]), encoding="utf-8")
            for start in range(0, len(rows), batch):
                part = rows[start:start + batch]
                texts = [redact_sensitive_text(body, force=True) for _, body, _, _ in part]
                redacted += [str(fact_id) for (fact_id, body, _, _), text in zip(part, texts, strict=True)
                             if text != body]
                for (fact_id, _, _, _), vector in zip(part, vectors([text[:cap] for text in texts]), strict=True):
                    conn.execute("update memory.facts set embedding = %s, embedding_model = %s where id = %s",
                                 (str(vector), build, fact_id))
            if other := conn.execute("select count(*) from memory.facts where embedding_model <> %s",
                                     (build,)).fetchone()[0]:
                raise ReembedError(f"{other} facts still hold another build; nothing was written")
    except BaseException:   # rolled back: the store is as the backup says, so the backup goes, and a retry may run
        backup.unlink(missing_ok=True)
        raise
    return {"facts": len(rows), "redacted": redacted}


def restore(owner_url, backup):
    """Put every backed-up fact's vector and build back, in one transaction; returns how many."""
    saved = json.loads(pathlib.Path(backup).read_text(encoding="utf-8"))
    with psycopg.connect(owner_url) as conn, conn.transaction():
        for fact in saved:
            conn.execute("update memory.facts set embedding = %s, embedding_model = %s where id = %s",
                         (fact["embedding"], fact["embedding_model"], fact["id"]))
    return len(saved)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record", help="write the reference the hosted route is checked by, once")
    commands.add_parser("check", help="print the cosine between the service's vector and the recorded one")
    store_parser = commands.add_parser("store", help="back up, then re-embed every fact through the hosted route")
    store_parser.add_argument("--backup", required=True, help="a file to write the old vectors to; must not exist")
    local_parser = commands.add_parser("local", help="back up, then re-embed every fact through a named Ollama")
    local_parser.add_argument("--ollama-url", required=True, help="the Ollama to embed with, as http://host:port")
    local_parser.add_argument("--backup", required=True, help="a file to write the old vectors to; must not exist")
    restore_parser = commands.add_parser("restore", help="put a backup's vectors and builds back")
    restore_parser.add_argument("backup")
    args = parser.parse_args(argv)
    key = os.environ.get(KEY, "").strip()
    if not key and args.command not in ("restore", "local"):
        parser.error(f"{KEY} is not set: DeepInfra's key, from the environment, never an argument")
    owner_url = os.environ.get(OWNER_URL, "").strip()
    if not owner_url and args.command in ("store", "local", "restore"):
        parser.error(f"{OWNER_URL} is not set: the store owner's connection string, from the environment")
    try:
        if args.command == "record":
            record(key)
            print(f"recorded {embedding.REFERENCE}")
        elif args.command == "check":
            cosine, threshold = check(key)
            print(f"cosine {cosine:.6f} (threshold {threshold})")
        elif args.command == "store":
            print(json.dumps(store(owner_url, key, backup=args.backup)))
        elif args.command == "local":
            print(json.dumps(local(owner_url, ollama_url=args.ollama_url, backup=args.backup)))
        else:
            print(f"restored {restore(owner_url, args.backup)} facts")
    except psycopg.Error as exc:   # its message can hold the connection string, password and all
        print(f"{args.command} failed: {type(exc).__name__} from the store", file=sys.stderr)
        return 1
    except (ReembedError, embedding.EmbedUnavailable, embedding.ModelMismatch, OSError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
