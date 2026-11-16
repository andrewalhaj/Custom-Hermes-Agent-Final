-- 0005: the facts search, one function (issue #158, 4a-i).
-- ARCHITECTURE.md 3.4, steps 2-3: live rows of the session's project and of no project (mirrored built-in
-- memory has none, #142), or of every project when the session has none; by meaning and by words, each a
-- ranked list of at most `pool` rows; fused by reciprocal rank with the constant `k` (3.6).
-- Security invoker, so a machine's login searches under its own grants and row policies.

-- pgvector's library defines hnsw.iterative_scan; until it is loaded in this session, a login that is not a
-- superuser (Supabase's postgres) may not set the parameter on the function below. Using its type loads it.
select '[0]'::public.halfvec;

create function memory.search_facts(
  query_embedding public.halfvec(2560),
  query_text text,
  query_project text,
  pool integer,
  k integer
) returns table (
  id uuid,
  body text,
  kind text,
  origin text,
  basis text,
  project text,
  created_at timestamptz,
  last_verified_at timestamptz,
  by_meaning integer,
  by_words integer,
  score double precision
)
  language plpgsql
  stable
  -- pgvector lives in public on Supabase (#108); pg_catalog first, so nothing in public shadows a built-in.
  set search_path = pg_catalog, public, pg_temp
  -- Filters apply after the HNSW scan; keep scanning until enough rows pass them (3.4, pgvector 0.8.0).
  set hnsw.iterative_scan = relaxed_order
as $function$
begin
  -- EXECUTE plans each call with its own values. A plan reused with generic parameters never chose the
  -- vector index, and sorted every live row by distance instead (#158 review).
  return query execute $query$
    with nearest as materialized (
      select f.id, f.embedding <=> $1 as distance
      from memory.facts f
      where f.superseded_by is null and f.retired_at is null   -- the index's predicate, repeated exactly
        and ($3::text is null or f.project is null or f.project = $3)
      order by f.embedding <=> $1
      limit $4
    ),
    -- relaxed_order can return rows slightly out of order, so rank them by distance again (pgvector's advice).
    by_meaning as (
      select nearest.id, row_number() over (order by nearest.distance)::integer as rank from nearest
    ),
    -- Any of the query's words: plainto_tsquery joins them with AND, so its operators become OR (3.4).
    words as (
      select nullif(replace(plainto_tsquery('english'::regconfig, $2)::text, ' & ', ' | '), '')::tsquery as q
    ),
    by_words as (
      select f.id, row_number() over (order by ts_rank_cd(f.search_text, words.q) desc)::integer as rank
      from memory.facts f, words
      where f.superseded_by is null and f.retired_at is null
        and ($3::text is null or f.project is null or f.project = $3)
        and f.search_text @@ words.q
      order by ts_rank_cd(f.search_text, words.q) desc
      limit $4
    ),
    fused as (
      select coalesce(m.id, w.id) as id, m.rank as by_meaning, w.rank as by_words,
             coalesce(1.0 / ($5 + m.rank), 0) + coalesce(1.0 / ($5 + w.rank), 0) as score
      from by_meaning m full join by_words w on m.id = w.id
    )
    select f.id, f.body, f.kind, f.origin, f.basis, f.project, f.created_at, f.last_verified_at,
           fused.by_meaning, fused.by_words, fused.score::double precision
    from fused join memory.facts f on f.id = fused.id
    order by fused.score desc, fused.by_meaning nulls last
    limit $4
  $query$ using query_embedding, query_text, query_project, pool, k;
end
$function$;

-- Postgres grants execute to PUBLIC on a new function; only a machine's login may search.
revoke execute on function memory.search_facts(public.halfvec, text, text, integer, integer) from public;
grant execute on function memory.search_facts(public.halfvec, text, text, integer, integer) to memory_agent;
