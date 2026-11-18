"""Embedding through one build of one model, checked against the store (ARCHITECTURE.md section 3.2).

Every vector in a store comes from one build, stored with each vector in `embedding_model`. The build is
either an Ollama (`embedding: local`, the default: this machine's, or the one `ollama_url` names, #261) or DeepInfra's hosted copy of the same model
(`embedding: deepinfra`, #239). A route whose build differs from one already in the store embeds nothing
(`embed.model_mismatch`); a service that cannot embed is `embed.unavailable`, and one that refuses the key
is `embed.refused`: there is no substitute model, and the hosted route does not fall back to Ollama.
"""

import http.client
import json
import math
import pathlib
import urllib.error
import urllib.request

MODEL = "qwen3-embedding:4b"
DIMENSIONS = 2560
OLLAMA_URL = "http://127.0.0.1:11434"
# Ollama is on this machine or the tailnet (#261), so its requests, which carry the text they embed, never go through a proxy
# that the environment or the system names (OPERATIONS.md section 2).
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The hosted route (#239): DeepInfra's real-time API, never its batch API, which may keep the text longer.
# Like the judge's, its requests may go through a proxy the environment names.
HOSTED_URL = "https://api.deepinfra.com/v1/openai/embeddings"
HOSTED_MODEL = "Qwen/Qwen3-Embedding-4B"
HOSTED_BUILD = f"deepinfra:{HOSTED_MODEL}"
# The hosted build has no digest: this file records a fixed sentence, its hosted vector and the cosine the
# vector must keep, so a service that changes what it serves fails the check (#239 item 3).
REFERENCE = pathlib.Path(__file__).with_name("embed_reference.json")
REFUSED = (401, 402, 403)   # a key that is revoked, out of credit or not allowed: the maintainer's to fix


class EmbedUnavailable(Exception):
    """`embed.unavailable`: the service cannot be reached, answers with an HTTP error, lacks the model, cuts
    its answer off or garbles it, or returns a different number of vectors than texts."""


class EmbedRefused(EmbedUnavailable):
    """`embed.refused`: the hosted service refused the key (401, 402 or 403). A kind of EmbedUnavailable, so
    a caller that does not tell the two apart still stops; each recording caller catches it first."""


class ModelMismatch(Exception):
    """`embed.model_mismatch`: the route's build differs from one in the store, or its vectors do."""


def for_setting(route, key, ollama_url=None):
    """The embedder the `embedding` setting names: DeepInfra with `key`, else the Ollama at `ollama_url`, this
    machine's when unset (#261)."""
    return HostedEmbedder(key) if route == "deepinfra" else Embedder(ollama_url or OLLAMA_URL)


def _other_build(conn, build):
    """A build other than `build` that the store holds vectors from, or None."""
    row = conn.execute("select embedding_model from memory.facts where embedding_model <> %s limit 1",
                       (build,)).fetchone()
    return row[0] if row else None


def moved(cosine, threshold):
    """The ModelMismatch for a reference vector that moved under its threshold."""
    return ModelMismatch(f"the reference sentence's vector has moved: cosine {cosine:.4f} with the recorded one, "
                         f"under {threshold}")


def _cosine(a, b):
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norms if norms else 0.0


def _ollama(url, path, body=None, *, timeout):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with _DIRECT.open(request, timeout=timeout) as response:
            return json.load(response)
    # urllib's URLError and HTTPError and socket timeouts are OSError; an answer cut off or garbled while it
    # is read raises http.client's IncompleteRead or BadStatusLine, which are HTTPException.
    except (OSError, http.client.HTTPException) as exc:
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
        if other := _other_build(conn, build):
            raise ModelMismatch(f"this machine has {build}; the store holds vectors from {other}")
        self.build = build

    def embed(self, conn, texts, *, timeout):
        """One vector of DIMENSIONS numbers per text, from the checked build only.

        The build is read again after each request, so a model replaced while Hermes runs never has its
        vectors labelled with the old build (#120); the next call checks against the store again.
        """
        if self.build is None:
            self.check(conn, timeout=timeout)
        vectors = _ollama(self.url, "/api/embed", {"model": MODEL, "input": texts}, timeout=timeout)["embeddings"]
        if (build := machine_build(self.url, timeout=timeout)) != self.build:
            checked, self.build = self.build, None
            raise ModelMismatch(f"this machine's build changed from {checked} to {build}")
        if len(vectors) != len(texts):
            raise EmbedUnavailable(f"Ollama returned {len(vectors)} vectors for {len(texts)} texts")
        if any(len(vector) != DIMENSIONS for vector in vectors):
            raise ModelMismatch(f"Ollama returned vectors of {sorted({len(v) for v in vectors})} numbers, not {DIMENSIONS}")
        return vectors


class HostedEmbedder:
    """Embeds through DeepInfra's copy of the model (#239). `build` stays None until a check passes, as with
    Embedder. The check embeds the recorded reference sentence and compares its vector with the recorded one,
    then checks that the store holds no other build. It runs when the provider starts, so a service that
    changed what it serves is found at the next start."""

    def __init__(self, key, url=None, reference=None):
        self.key = key
        self.url = url or HOSTED_URL
        self.reference = pathlib.Path(reference or REFERENCE)
        self.build = None

    def vectors(self, texts, *, timeout):
        """One vector per text, straight from the service, with no check: `embed` is the build's way in."""
        body = json.dumps({"model": HOSTED_MODEL, "input": texts, "encoding_format": "float"}).encode()
        request = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)["data"]
            vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
        except urllib.error.HTTPError as exc:   # before OSError, which it is too
            raise (EmbedRefused if exc.code in REFUSED else EmbedUnavailable)(f"DeepInfra: {exc}") from exc
        # Unreachable, timed out, cut off, or an answer that is not the documented shape.
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
            raise EmbedUnavailable(f"DeepInfra: {exc!r}") from exc
        if len(vectors) != len(texts):
            raise EmbedUnavailable(f"DeepInfra returned {len(vectors)} vectors for {len(texts)} texts")
        # A garbled answer: json reads null, text, true and NaN as values a vector cannot hold (#239 review).
        if not all(isinstance(vector, list) and all(type(x) in (int, float) and math.isfinite(x) for x in vector)
                   for vector in vectors):
            raise EmbedUnavailable("DeepInfra returned a vector that is not a list of finite numbers")
        if any(len(vector) != DIMENSIONS for vector in vectors):
            raise ModelMismatch(f"DeepInfra returned vectors of {sorted({len(v) for v in vectors})} numbers, "
                                f"not {DIMENSIONS}")
        return vectors

    def reference_cosine(self, *, timeout):
        """(the cosine between the service's vector for the reference sentence and the recorded one, the threshold
        it must keep). ModelMismatch when no reference is recorded or its file cannot be read: it fails closed."""
        if not self.reference.exists():
            raise ModelMismatch(f"no reference vector recorded at {self.reference}")
        try:
            reference = json.loads(self.reference.read_text(encoding="utf-8"))
            text, recorded, threshold = str(reference["text"]), reference["vector"], float(reference["threshold"])
            if len(recorded) != DIMENSIONS:
                raise ValueError(f"a vector of {len(recorded)} numbers, not {DIMENSIONS}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelMismatch(f"the reference file {self.reference} cannot be read: {exc!r}") from exc
        [vector] = self.vectors([text], timeout=timeout)
        return _cosine(vector, recorded), threshold

    def check_reference(self, *, timeout):
        """ModelMismatch unless the service still gives the reference sentence the recorded vector."""
        cosine, threshold = self.reference_cosine(timeout=timeout)
        if cosine < threshold:
            raise moved(cosine, threshold)

    def check(self, conn, *, timeout):
        self.check_reference(timeout=timeout)
        if other := _other_build(conn, HOSTED_BUILD):
            raise ModelMismatch(f"the hosted route is {HOSTED_BUILD}; the store holds vectors from {other}")
        self.build = HOSTED_BUILD

    def embed(self, conn, texts, *, timeout):
        """One vector of DIMENSIONS numbers per text, once a check has passed."""
        if self.build is None:
            self.check(conn, timeout=timeout)
        return self.vectors(texts, timeout=timeout)
