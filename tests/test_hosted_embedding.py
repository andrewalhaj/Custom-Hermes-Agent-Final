"""Prove the hosted embedding route (#239, decided 2026-11-17: DeepInfra, called directly, no fallback).

The `deepinfra` fixture (conftest.py) fakes DeepInfra's OpenAI-compatible embeddings endpoint on a local
port, so nothing leaves the machine and no key is real. The hosted build is identified by a reference
vector, not a digest: a fixed sentence's vector recorded once, which each check embeds again and compares
(ARCHITECTURE.md 3.2).
"""

import json

import psycopg
import pytest

from custom_build.memory import embedding, tool

KEY = "test-key-not-a-real-one"
VECTOR = [0.01] * 2560   # the fake's default answer


@pytest.fixture
def reference(tmp_path):
    """A recorded reference: the fixed sentence, its hosted vector, and the threshold."""
    path = tmp_path / "embed_reference.json"
    path.write_text(json.dumps({"text": "The reference sentence.", "vector": VECTOR, "threshold": 0.999}),
                    encoding="utf-8")
    return path


def hosted(deepinfra, reference):
    return embedding.HostedEmbedder(KEY, url=deepinfra.url, reference=reference)


def _store_fact(dsn, build):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "insert into memory.facts (body, body_hash, kind, origin, basis, source, embedding, embedding_model)"
            " values ('A stored fact.', 'h', 'fact', 'explicit', 'user_stated', 'host-a', %s, %s)",
            ("[" + ",".join(["0.01"] * 2560) + "]", build))


def test_the_setting_picks_the_route_and_local_is_the_default():
    assert isinstance(embedding.for_setting(None, None), embedding.Embedder)
    assert isinstance(embedding.for_setting("local", None), embedding.Embedder)
    assert isinstance(embedding.for_setting("deepinfra", KEY), embedding.HostedEmbedder)


def test_the_local_route_uses_the_ollama_the_setting_names_and_this_machines_by_default():
    # #261: the model can run on another machine on the tailnet; unset, it is this machine's, as before.
    assert embedding.for_setting(None, None).url == embedding.OLLAMA_URL == "http://127.0.0.1:11434"
    assert embedding.for_setting("local", None, "http://100.64.0.2:11434").url == "http://100.64.0.2:11434"
    assert embedding.for_setting(None, None, "http://100.64.0.2:11434").url == "http://100.64.0.2:11434"


def test_a_store_embedded_on_another_machines_ollama_passes_on_a_host_that_names_it(ollama, db):
    # The digest recorded with each vector is the model's build wherever it runs (#261).
    _store_fact(db["owner"], ollama.build)
    embedder = embedding.for_setting("local", None, ollama.url)
    with psycopg.connect(db["agent"]) as conn:
        embedder.check(conn, timeout=5)
    assert embedder.build == ollama.build


def test_the_check_passes_when_the_reference_vector_comes_back_the_same(deepinfra, reference, db):
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn:
        embedder.check(conn, timeout=5)
    assert embedder.build == embedding.HOSTED_BUILD == "deepinfra:Qwen/Qwen3-Embedding-4B"
    assert deepinfra.requests[0][1]["input"] == ["The reference sentence."]


def test_a_reference_vector_that_drifted_is_a_model_mismatch(deepinfra, reference, db):
    # The service changed what it serves: the same sentence no longer gets the recorded vector (#239 item 3).
    deepinfra.other = [0.01] * 1280 + [0.02] * 1280   # cosine about 0.95 with the recorded one
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch, match="reference"):
        embedder.check(conn, timeout=5)
    assert embedder.build is None


def test_no_recorded_reference_is_a_model_mismatch(deepinfra, tmp_path, db):
    embedder = hosted(deepinfra, tmp_path / "missing.json")
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch, match="no reference"):
        embedder.check(conn, timeout=5)


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({"text": "The reference sentence.", "vector": VECTOR}),   # no threshold
    json.dumps({"text": "The reference sentence.", "vector": [0.01] * 1024, "threshold": 0.999}),
])
def test_a_reference_file_that_cannot_be_read_is_a_model_mismatch_not_a_crash(deepinfra, tmp_path, db, content):
    # #239 review: each was a ValueError or KeyError, recorded as unhandled.
    path = tmp_path / "embed_reference.json"
    path.write_text(content, encoding="utf-8")
    embedder = hosted(deepinfra, path)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch, match="reference"):
        embedder.check(conn, timeout=5)


class Timed(embedding.HostedEmbedder):
    """The hosted embedder, recording the limit each request was given."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeouts = []

    def vectors(self, texts, *, timeout):
        self.timeouts.append(timeout)
        return super().vectors(texts, timeout=timeout)


def test_recall_gives_each_hosted_request_two_seconds(deepinfra, reference, db):
    # #239, decided: within recall's 6-second deadline, so a check and the query's embedding leave the rerank time.
    embedder = Timed(KEY, url=deepinfra.url, reference=reference)
    tool.memory_search({"query": "where is the deploy script"}, machine="host-a", session_id="s1", turn=1,
                       embedder=embedder, judge_at=None, connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    assert embedder.timeouts == [2, 2]   # the reference check, then the query


def test_a_store_on_the_turn_keeps_its_five_seconds_on_the_hosted_route(deepinfra, reference, db):
    embedder = Timed(KEY, url=deepinfra.url, reference=reference)
    tool.store_fact({"fact": "The parser tests run with pytest.", "basis": "user_stated"}, machine="host-a",
                    session_id="s1", turn=1, embedder=embedder, judge_at=None,
                    connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    assert embedder.timeouts == [tool.TIMEOUT, tool.TIMEOUT] == [5, 5]


def test_the_hosted_check_fails_while_the_store_holds_the_local_build(deepinfra, reference, db):
    # One build per store: the hosted route never adds to a store of local vectors (#239 item 1).
    _store_fact(db["agent"], "qwen3-embedding:4b@sha256:df5bd2e3c74c")
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch, match="store holds"):
        embedder.check(conn, timeout=5)
    assert embedder.build is None


def test_embed_sends_the_texts_with_the_key_and_returns_their_vectors_in_order(deepinfra, reference, db):
    deepinfra.vectors = {"first": [0.1] * 2560, "second": [0.2] * 2560}
    deepinfra.reverse = True
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn:
        vectors = embedder.embed(conn, ["first", "second"], timeout=5)
    assert [vector[0] for vector in vectors] == [0.1, 0.2]
    authorization, body = deepinfra.requests[-1]
    assert authorization == f"Bearer {KEY}"
    assert body == {"model": "Qwen/Qwen3-Embedding-4B", "input": ["first", "second"], "encoding_format": "float"}


@pytest.mark.parametrize("status", [401, 402, 403])
def test_a_refused_key_or_no_credit_is_embed_refused(deepinfra, reference, db, status):
    # It needs the maintainer, not a wait, so it is told apart from an outage.
    deepinfra.status = status
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.EmbedRefused):
        embedder.check(conn, timeout=5)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_limit_or_a_server_error_is_unavailable(deepinfra, reference, db, status):
    deepinfra.status = status
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.EmbedUnavailable) as raised:
        embedder.check(conn, timeout=5)
    assert not isinstance(raised.value, embedding.EmbedRefused)   # a wait may fix it; the key is not at fault


def test_a_service_that_cannot_be_reached_is_unavailable(ollama_down, reference, db):
    embedder = embedding.HostedEmbedder(KEY, url=ollama_down + "/v1/openai/embeddings", reference=reference)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.EmbedUnavailable):
        embedder.check(conn, timeout=5)


def test_a_vector_of_another_length_is_a_model_mismatch(deepinfra, reference, db):
    embedder = hosted(deepinfra, reference)
    with psycopg.connect(db["agent"]) as conn:
        embedder.check(conn, timeout=5)
        deepinfra.other = [0.01] * 1024
        with pytest.raises(embedding.ModelMismatch):
            embedder.embed(conn, ["a text"], timeout=5)


@pytest.mark.parametrize("vector", [[None] * 2560, ["x"] * 2560, [float("nan")] * 2560, [True] * 2560, None])
def test_a_vector_that_is_not_numbers_is_a_garbled_answer(deepinfra, reference, vector):
    # #239 review: a null or text crashed the cosine, and a NaN passed the threshold check (nan < t is False).
    deepinfra.other = vector
    with pytest.raises(embedding.EmbedUnavailable, match="numbers"):
        hosted(deepinfra, reference).reference_cosine(timeout=5)


def test_a_refused_key_in_a_search_is_recorded_and_returned_as_embed_refused(deepinfra, reference, db):
    deepinfra.status = 401
    outcome = tool.memory_search({"query": "where is the deploy script"}, machine="host-a", session_id="s1", turn=1,
                                 embedder=hosted(deepinfra, reference), judge_at=None,
                                 connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    assert outcome == {"error": "embed.refused"}
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select code from memory.events").fetchall() == [("embed.refused",)]


def test_a_secret_in_a_search_never_reaches_the_service(deepinfra, reference, db):
    # CONTRIBUTING.md rule L4: the text leaves the host now, so it must be redacted first (#239).
    token = "gh" + "p_" + "Abcdefghijklmnopqrstuvwxyz0123456789"   # assembled at run time, never a literal key
    tool.memory_search({"query": f"where is {token} kept"}, machine="host-a", session_id="s1", turn=1,
                       embedder=hosted(deepinfra, reference), judge_at=None,
                       connect=lambda: psycopg.connect(db["agent"], autocommit=True))
    sent = [text for _, body in deepinfra.requests for text in body["input"]]
    assert len(sent) == 2 and all(token not in text for text in sent)   # the reference sentence, then the query
