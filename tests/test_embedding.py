"""Prove embedding through the pinned build and its check against the store (issue #111, ARCHITECTURE.md 3.2).

The `ollama` fixture (conftest.py) serves the two Ollama endpoints the build calls from a local HTTP
server, so the tests need no model. The store checks use the `db` fixture.
"""

import psycopg
import pytest

from custom_build.memory import embedding


def _store_fact(dsn, build):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "insert into memory.facts (body, body_hash, kind, origin, basis, source, embedding, embedding_model)"
            " values ('A stored fact.', 'h', 'fact', 'explicit', 'user_stated', 'host-a', %s, %s)",
            ("[" + ",".join(["0.01"] * 2560) + "]", build),
        )


def test_the_machine_build_names_the_model_and_its_digest(ollama):
    assert embedding.machine_build(ollama.url, timeout=5) == f"qwen3-embedding:4b@sha256:{ollama.digest}"


def test_a_machine_without_the_model_is_unavailable(ollama):
    ollama.models = [{"name": "qwen2.5:14b", "model": "qwen2.5:14b", "digest": "7cdf5a01"}]
    with pytest.raises(embedding.EmbedUnavailable):
        embedding.machine_build(ollama.url, timeout=5)


def test_an_ollama_that_cannot_be_reached_is_unavailable(ollama_down):
    with pytest.raises(embedding.EmbedUnavailable):
        embedding.machine_build(ollama_down, timeout=5)


def test_the_check_passes_on_an_empty_store(ollama, db):
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn:
        embedder.check(conn, timeout=5)
    assert embedder.build == ollama.build


def test_the_check_passes_when_the_store_holds_only_this_build(ollama, db):
    _store_fact(db["agent"], ollama.build)
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn:
        embedder.check(conn, timeout=5)
    assert embedder.build == ollama.build


def test_the_check_fails_when_the_store_holds_another_build(ollama, db):
    _store_fact(db["agent"], "qwen3-embedding:4b@sha256:another")
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch):
        embedder.check(conn, timeout=5)
    assert embedder.build is None


def test_a_check_that_failed_passes_once_the_other_build_is_gone(ollama, db):
    _store_fact(db["owner"], "qwen3-embedding:4b@sha256:another")
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn:
        with pytest.raises(embedding.ModelMismatch):
            embedder.embed(conn, ["one"], timeout=5)
        with psycopg.connect(db["owner"]) as owner:
            owner.execute("delete from memory.facts")   # a re-embedding migration would rewrite them instead
        assert [len(v) for v in embedder.embed(conn, ["one"], timeout=5)] == [2560]
    assert embedder.build == ollama.build


def test_embed_returns_one_vector_of_2560_numbers_per_text(ollama, db):
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn:
        vectors = embedder.embed(conn, ["one", "two"], timeout=5)
    assert [len(v) for v in vectors] == [2560, 2560]
    assert ollama.embedded == [["one", "two"]]


def test_embed_refuses_while_the_store_holds_another_build(ollama, db):
    _store_fact(db["agent"], "qwen3-embedding:4b@sha256:another")
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch):
        embedder.embed(conn, ["one"], timeout=5)
    assert ollama.embedded == []   # nothing was embedded, so nothing from this build can be stored


def test_an_http_error_while_embedding_is_unavailable(ollama, db):
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn:
        embedder.check(conn, timeout=5)
        ollama.status = 404
        with pytest.raises(embedding.EmbedUnavailable):
            embedder.embed(conn, ["one"], timeout=5)


def test_a_vector_of_another_length_is_a_model_mismatch(ollama, db):
    ollama.dimensions = 1024
    embedder = embedding.Embedder(ollama.url)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(embedding.ModelMismatch):
        embedder.embed(conn, ["one"], timeout=5)
