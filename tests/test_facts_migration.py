"""Prove migrations/0002_memory_facts.sql (issue #105, ARCHITECTURE.md sections 4.2 and 4.3).

The `db` fixture (conftest.py) gives each test an empty database with every migration applied.
"""

import uuid

import psycopg
import pytest

LIVE = "WHERE ((superseded_by IS NULL) AND (retired_at IS NULL))"


def _vector(dimensions=2560):
    return "[" + ",".join(["0.5"] * dimensions) + "]"


def _insert(conn, *, project="p1", body_hash=None, kind="fact", origin="explicit", embedding=None):
    return conn.execute(
        "insert into memory.facts (body, body_hash, kind, origin, basis, project, source, embedding, embedding_model)"
        " values ('The test host runs Postgres 17.', %s, %s, %s, 'user_stated', %s, 'test-host', %s,"
        " 'qwen3-embedding:4b@test') returning id",
        (body_hash or uuid.uuid4().hex, kind, origin, project, embedding or _vector()),
    ).fetchone()[0]


def test_installs_the_vector_extension(db):
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select count(*) from pg_extension where extname = 'vector'").fetchone() == (1,)


def test_creates_the_facts_table_as_planned(db):
    with psycopg.connect(db["owner"]) as conn:
        columns = conn.execute(
            "select attname, format_type(atttypid, atttypmod), attnotnull from pg_attribute"
            " where attrelid = 'memory.facts'::regclass and attnum > 0 and not attisdropped order by attnum"
        ).fetchall()
        defaults = conn.execute(
            "select column_name, column_default, generation_expression from information_schema.columns"
            " where table_schema = 'memory' and table_name = 'facts'"
            " and (column_default is not null or generation_expression is not null) order by ordinal_position"
        ).fetchall()
    assert columns == [
        ("id", "uuid", True),
        ("body", "text", True),
        ("body_hash", "text", True),
        ("kind", "text", True),
        ("origin", "text", True),
        ("basis", "text", True),
        ("project", "text", False),
        ("source", "text", True),
        ("session_id", "text", False),
        ("source_turn", "integer", False),
        ("embedding", "halfvec(2560)", True),
        ("embedding_model", "text", True),
        ("search_text", "tsvector", False),
        ("created_at", "timestamp with time zone", True),
        ("last_verified_at", "timestamp with time zone", False),
        ("superseded_by", "uuid", False),
        ("retired_at", "timestamp with time zone", False),
        ("retired_reason", "text", False),
    ]
    assert defaults == [
        ("id", "gen_random_uuid()", None),
        ("search_text", None, "to_tsvector('english'::regconfig, body)"),
        ("created_at", "now()", None),
    ]


def test_creates_the_five_indexes(db):
    with psycopg.connect(db["owner"]) as conn:
        indexes = conn.execute(
            "select indexdef from pg_indexes where schemaname = 'memory' and tablename = 'facts'"
            " and indexname <> 'facts_pkey' order by indexname"
        ).fetchall()
    assert [row[0] for row in indexes] == [
        f"CREATE UNIQUE INDEX facts_live_body_hash ON memory.facts USING btree (project, body_hash) NULLS NOT DISTINCT {LIVE}",
        f"CREATE INDEX facts_live_embedding_idx ON memory.facts USING hnsw (embedding halfvec_cosine_ops) {LIVE}",
        # 0008's one live digest per session (#156): the constraint's own index
        ("CREATE INDEX facts_one_live_digest_per_session ON memory.facts USING btree (session_id) WHERE"
         " ((kind = 'digest'::text) AND (superseded_by IS NULL) AND (retired_at IS NULL))"),
        "CREATE INDEX facts_search_text_idx ON memory.facts USING gin (search_text)",
        "CREATE INDEX facts_session_idx ON memory.facts USING btree (session_id)",
    ]


def test_creates_exactly_these_checks(db):
    # The behaviour tests below prove each check refuses; this proves none refuses more than planned.
    with psycopg.connect(db["owner"]) as conn:
        checks = conn.execute(
            "select pg_get_constraintdef(oid) from pg_constraint where conrelid = 'memory.facts'::regclass and contype = 'c'"
        ).fetchall()
    assert sorted(row[0] for row in checks) == [
        "CHECK (((kind = 'digest'::text) = (origin = 'digest'::text)))",
        "CHECK (((retired_at IS NULL) = (retired_reason IS NULL)))",
        "CHECK ((basis = ANY (ARRAY['user_stated'::text, 'tool_observed'::text, 'assistant_inferred'::text])))",
        "CHECK ((kind = ANY (ARRAY['fact'::text, 'digest'::text])))",
        "CHECK ((origin = ANY (ARRAY['explicit'::text, 'extracted'::text, 'digest'::text, 'imported'::text])))",
    ]


def test_memory_holds_exactly_these_tables(db):
    with psycopg.connect(db["owner"]) as conn:
        tables = conn.execute(
            "select table_name from information_schema.tables where table_schema = 'memory' order by table_name"
        ).fetchall()
    assert tables == [("events",), ("facts",), ("schema_level",)]


def test_grants_exactly_what_code_uses(db):
    # Read from the catalog's own lists, which also show MAINTAIN, grant options and PUBLIC, unlike
    # information_schema.
    with psycopg.connect(db["owner"]) as conn:
        tables = conn.execute(
            "select a.grantee::regrole::text, a.privilege_type, a.is_grantable from pg_class c, aclexplode(c.relacl) a"
            " where c.oid = 'memory.facts'::regclass and a.grantee <> c.relowner order by 1, 2"
        ).fetchall()
        columns = conn.execute(
            "select attname, a.grantee::regrole::text, a.privilege_type, a.is_grantable"
            " from pg_attribute, aclexplode(attacl) a"
            " where attrelid = 'memory.facts'::regclass and attacl is not null order by 1, 2, 3"
        ).fetchall()
    assert tables == [("memory_agent", "INSERT", False), ("memory_agent", "SELECT", False)]
    assert columns == [("retired_at", "memory_agent", "UPDATE", False), ("retired_reason", "memory_agent", "UPDATE", False),
                       ("superseded_by", "memory_agent", "UPDATE", False)]   # retiring, from 0003 (#142)


def test_row_level_security_is_on(db):
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select relrowsecurity from pg_class where oid = 'memory.facts'::regclass").fetchone() == (True,)


def test_the_facts_policies_are_exactly_these(db):
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute(
            "select policyname, cmd, roles::text[] from pg_policies where schemaname = 'memory' and tablename = 'facts'"
            " order by policyname"
        ).fetchall()
    assert rows == [
        ("facts_agent_insert", "INSERT", ["memory_agent"]),
        ("facts_agent_select", "SELECT", ["memory_agent"]),
        ("facts_agent_update", "UPDATE", ["memory_agent"]),
    ]


def test_agent_can_store_and_find_a_fact_by_its_words(db):
    with psycopg.connect(db["agent"]) as conn:
        fact_id = _insert(conn)
        found = conn.execute(
            "select id from memory.facts where search_text @@ to_tsquery('english', 'postgres')"
        ).fetchall()
    assert found == [(fact_id,)]


def test_agent_can_retire_a_fact_with_its_reason(db):
    # 0003 (#142): a fact removed from built-in memory is retired, never deleted.
    with psycopg.connect(db["agent"]) as conn:
        fact = _insert(conn)
        assert conn.execute("update memory.facts set retired_at = now(), retired_reason = 'removed from built-in memory'"
                            " where id = %s", (fact,)).rowcount == 1


def test_agent_can_mark_a_fact_superseded(db):
    with psycopg.connect(db["agent"]) as conn:
        old, new = _insert(conn), _insert(conn)
        assert conn.execute("update memory.facts set superseded_by = %s where id = %s", (new, old)).rowcount == 1


def test_superseded_by_must_name_a_stored_fact(db):
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("update memory.facts set superseded_by = %s where id = %s", (uuid.uuid4(), _insert(conn)))


@pytest.mark.parametrize("statement", [
    "delete from memory.facts",
    "update memory.facts set body = 'rewritten'",
    "update memory.facts set last_verified_at = now()",
    "truncate memory.facts",
    "alter table memory.facts add column extra text",
    "drop table memory.facts",
])
def test_agent_is_refused(db, statement):
    with psycopg.connect(db["owner"]) as owner:
        _insert(owner)
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


@pytest.mark.parametrize("statement", [
    "select * from memory.facts",
    "insert into memory.facts (body) values ('x')",
    "update memory.facts set superseded_by = null",
    "delete from memory.facts",
])
def test_maintenance_is_refused(db, statement):
    with psycopg.connect(db["maint"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


@pytest.mark.parametrize("project", ["p1", None])
def test_a_second_live_fact_with_the_same_hash_is_refused(db, project):
    with psycopg.connect(db["agent"]) as conn:
        _insert(conn, project=project, body_hash="same")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, project=project, body_hash="same")


def test_a_superseded_fact_frees_its_hash(db):
    with psycopg.connect(db["agent"]) as conn:
        old = _insert(conn, body_hash="same")
        conn.execute("update memory.facts set superseded_by = %s where id = %s", (_insert(conn), old))
        _insert(conn, body_hash="same")


def test_the_same_hash_in_another_project_is_accepted(db):
    with psycopg.connect(db["agent"]) as conn:
        _insert(conn, project="p1", body_hash="same")
        _insert(conn, project="p2", body_hash="same")


def test_a_vector_with_the_wrong_number_of_dimensions_is_refused(db):
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.DataException):
        _insert(conn, embedding=_vector(3))


@pytest.mark.parametrize("kind, origin", [
    ("note", "explicit"),        # not a kind
    ("fact", "remembered"),      # not an origin
    ("digest", "explicit"),      # a digest comes only from the digest job
    ("fact", "digest"),          # and the digest job writes only digests
])
def test_kind_and_origin_take_only_the_planned_pairs(db, kind, origin):
    with psycopg.connect(db["agent"]) as conn, pytest.raises(psycopg.errors.CheckViolation):
        _insert(conn, kind=kind, origin=origin)


def test_basis_takes_only_the_three_planned_values(db):
    with psycopg.connect(db["agent"]) as conn:
        fact_id = _insert(conn)
    with psycopg.connect(db["owner"]) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update memory.facts set basis = 'guessed' where id = %s", (fact_id,))


@pytest.mark.parametrize("assignment", ["retired_at = now()", "retired_reason = 'wrong'"])
def test_retired_at_and_its_reason_are_set_together(db, assignment):
    with psycopg.connect(db["agent"]) as conn:
        fact_id = _insert(conn)
    with psycopg.connect(db["owner"]) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(f"update memory.facts set {assignment} where id = %s", (fact_id,))
