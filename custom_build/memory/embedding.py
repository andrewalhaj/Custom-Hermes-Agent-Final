"""Embedding through the pinned build, checked against the store (ARCHITECTURE.md section 3.2).

Every vector comes from one build of one model, served by this machine's Ollama (OPERATIONS.md
section 2), and the build is stored with each vector in `embedding_model`. A machine whose build
differs from one already in the store embeds nothing (`embed.model_mismatch`), and an Ollama that
cannot embed is `embed.unavailable`: there is no substitute model.
"""

import json
import urllib.request

MODEL = "qwen3-embedding:4b"
DIMENSIONS = 2560
OLLAMA_URL = "http://127.0.0.1:11434"
# Ollama is on this machine, so its requests, which carry the text they embed, never go through a proxy
# that the environment or the system names (OPERATIONS.md section 2).
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EmbedUnavailable(Exception):
    """`embed.unavailable`: Ollama cannot be reached, answers with an HTTP error, or lacks the model."""


class ModelMismatch(Exception):
    """`embed.model_mismatch`: this machine's build differs from one in the store, or its vectors do."""


def _ollama(url, path, body=None, *, timeout):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with _DIRECT.open(request, timeout=timeout) as response:
            return json.load(response)
    except OSError as exc:   # urllib's URLError and HTTPError, and socket timeouts, are all OSError
        raise EmbedUnavailable(f"{path}: {exc}") from exc


def machine_build(url=OLLAMA_URL, *, timeout):
    """This machine's build of the model, as stored with each vector: `<model>@sha256:<digest>`."""
    for model in _ollama(url, "/api/tags", timeout=timeout)["models"]:
        if model["name"] == MODEL:
            return f"{MODEL}@sha256:{model['digest']}"
    raise EmbedUnavailable(f"Ollama does not have {MODEL}")


class Embedder:
    """Embeds through this machine's build once the store has been seen not to hold another.

    `build` stays None until a check passes, and `embed` checks again until then, so a failed check
    refuses every embedding and a later pass recovers. An empty store holds no build yet: the first
    fact stored records this one.
    """

    def __init__(self, url=OLLAMA_URL):
        self.url = url
        self.build = None

    def check(self, conn, *, timeout):
        build = machine_build(self.url, timeout=timeout)
        other = conn.execute(
            "select embedding_model from memory.facts where embedding_model <> %s limit 1", (build,)
        ).fetchone()
        if other:
            raise ModelMismatch(f"this machine has {build}; the store holds vectors from {other[0]}")
        self.build = build

    def embed(self, conn, texts, *, timeout):
        """One vector of DIMENSIONS numbers per text."""
        if self.build is None:
            self.check(conn, timeout=timeout)
        vectors = _ollama(self.url, "/api/embed", {"model": MODEL, "input": texts}, timeout=timeout)["embeddings"]
        if [len(vector) for vector in vectors] != [DIMENSIONS] * len(texts):
            raise ModelMismatch(f"Ollama returned {len(vectors)} vectors of {sorted({len(v) for v in vectors})} "
                                f"numbers for {len(texts)} texts, not {DIMENSIONS} numbers each")
        return vectors
