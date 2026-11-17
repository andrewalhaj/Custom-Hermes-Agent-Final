"""Prove the hosted route's switch-over script (#239, decided 2026-11-17: re-embed the store once, one build per store).

`scripts/reembed.py record` writes the reference the hosted route is checked by; `check` compares the
service's vector for it with the recorded one; `store` re-embeds every fact of a store through the hosted
route. The `deepinfra` fixture fakes DeepInfra on a local port and the `db` fixture gives a throwaway store.
"""

import json

import psycopg
import pytest

from custom_build.memory import embedding, write_path
from scripts import reembed

KEY = "test-key-not-a-real-one"
LOCAL = "qwen3-embedding:4b@sha256:df5bd2e3c74c"


@pytest.fixture
def reference(tmp_path, deepinfra):
    """A reference recorded by the script, against the fake."""
    path = tmp_path / "embed_reference.json"
    reembed.record(KEY, url=deepinfra.url, path=path)
    deepinfra.requests.clear()
    return path


def _add(owner, body, *, retired=False):
    owner.execute(
        "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding, embedding_model,"
        " retired_at, retired_reason) values (%s, %s, 'fact', 'explicit', 'user_stated', 'p1', 'host-a', %s, %s,"
        " %s, %s)",
        (body, body[:40], "[" + ",".join(["0.5"] * 2560) + "]", LOCAL,
         "2026-11-01" if retired else None, "wrong" if retired else None))


def test_record_writes_the_reference_sentence_its_hosted_vector_and_the_threshold(tmp_path, deepinfra):
    path = tmp_path / "embed_reference.json"
    reembed.record(KEY, url=deepinfra.url, path=path)
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert recorded == {"text": reembed.SENTENCE, "vector": deepinfra.other, "threshold": 0.999}
    assert [body["input"] for _, body in deepinfra.requests] == [[reembed.SENTENCE]]


def test_record_never_replaces_a_recorded_reference(deepinfra, reference):
    # A new reference would change what the hosted build is: that is a new store, not a re-recording.
    before = reference.read_text(encoding="utf-8")
    with pytest.raises(reembed.ReembedError, match="already"):
        reembed.record(KEY, url=deepinfra.url, path=reference)
    assert reference.read_text(encoding="utf-8") == before and deepinfra.requests == []


def test_check_gives_the_cosine_with_the_recorded_vector(deepinfra, reference):
    assert reembed.check(KEY, url=deepinfra.url, path=reference) == pytest.approx(1.0)
    deepinfra.other = [0.01] * 1280 + [0.02] * 1280
    assert reembed.check(KEY, url=deepinfra.url, path=reference) == pytest.approx(0.9487, abs=1e-3)


def test_store_re_embeds_every_fact_live_or_retired_with_the_hosted_build(deepinfra, reference, db):
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        _add(owner, "The parser tests run with pytest.")
        _add(owner, "The deploy script lives in scripts/.", retired=True)   # check() reads every row
    assert reembed.store(db["owner"], KEY, url=deepinfra.url, path=reference) == {"facts": 2, "redacted": 0}
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select embedding_model, embedding::text from memory.facts").fetchall()
    assert {model for model, _ in rows} == {embedding.HOSTED_BUILD}
    assert all(vector.startswith("[0.01") for _, vector in rows)   # the hosted 0.01s, not the local 0.5s (halfvec)
    # The store now passes the hosted route's own check.
    with psycopg.connect(db["agent"]) as conn:
        hosted = embedding.HostedEmbedder(KEY, url=deepinfra.url, reference=reference)
        hosted.check(conn, timeout=5)
    assert hosted.build == embedding.HOSTED_BUILD


def test_store_writes_nothing_when_the_reference_does_not_check(deepinfra, reference, db):
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        _add(owner, "The parser tests run with pytest.")
    deepinfra.other = [0.01] * 1280 + [0.02] * 1280   # the service serves something else now
    with pytest.raises(embedding.ModelMismatch, match="moved"):
        reembed.store(db["owner"], KEY, url=deepinfra.url, path=reference)
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select embedding_model from memory.facts").fetchall() == [(LOCAL,)]
    assert [body["input"] for _, body in deepinfra.requests] == [[reembed.SENTENCE]]   # the check, nothing more


def test_a_failure_part_way_leaves_every_fact_as_it_was(deepinfra, reference, db):
    # One transaction: a store is never left holding both builds.
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        for n in range(5):
            _add(owner, f"Fact number {n}.")
    deepinfra.fail_on = "Fact number 3."
    with pytest.raises(embedding.EmbedUnavailable):
        reembed.store(db["owner"], KEY, url=deepinfra.url, path=reference, batch=2)
    with psycopg.connect(db["owner"]) as conn:
        assert {row[0] for row in conn.execute("select embedding_model from memory.facts")} == {LOCAL}


def test_store_sends_each_body_redacted_and_capped(deepinfra, reference, db):
    token = "gh" + "p_" + "Abcdefghijklmnopqrstuvwxyz0123456789"   # assembled at run time, never a literal key
    long_body = "The deploy notes. " + "x" * 6000
    with psycopg.connect(db["owner"], autocommit=True) as owner:
        _add(owner, f"The token {token} was stored before a redaction pattern covered it.")
        _add(owner, long_body)
    outcome = reembed.store(db["owner"], KEY, url=deepinfra.url, path=reference)
    sent = [text for _, body in deepinfra.requests[1:] for text in body["input"]]
    assert all(token not in text for text in sent) and outcome["redacted"] == 1
    assert long_body[:write_path.load()["embed"]["store_chars"]] in sent
    with psycopg.connect(db["owner"]) as conn:   # the stored text is left as it is
        assert conn.execute("select count(*) from memory.facts where body = %s", (long_body,)).fetchone() == (1,)


def test_the_command_needs_deepinfras_key(capsys, monkeypatch):
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        reembed.main(["check"])
    assert "DEEPINFRA_API_KEY" in capsys.readouterr().err
