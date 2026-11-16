-- 0002: the facts table, its indexes and its grants (issue #105).
-- ARCHITECTURE.md sections 3.3, 4.2 and 4.3. Applied migrations are never edited; a change is a new file.

create extension if not exists vector;   -- halfvec and its HNSW index need pgvector 0.7.0 or later

create table memory.facts (
  id               uuid primary key default gen_random_uuid(),
  body             text not null,
  body_hash        text not null,            -- hash of the normalised body, for the identical-text check
  kind             text not null check (kind in ('fact', 'digest')),
  origin           text not null check (origin in ('explicit', 'extracted', 'digest', 'imported')),
  basis            text not null check (basis in ('user_stated', 'tool_observed', 'assistant_inferred')),
  project          text,                     -- project key (section 2); null when the session had none
  source           text not null,            -- the machine that wrote the row
  session_id       text,
  source_turn      integer,                  -- message position of the turn the fact came from
  embedding        halfvec(2560) not null,
  embedding_model  text not null,            -- model name and build digest
  search_text      tsvector generated always as (to_tsvector('english', body)) stored,
  created_at       timestamptz not null default now(),
  last_verified_at timestamptz,
  superseded_by    uuid references memory.facts (id),
  retired_at       timestamptz,
  retired_reason   text,
  check ((retired_at is null) = (retired_reason is null)),
  check ((kind = 'digest') = (origin = 'digest'))
);

create unique index facts_live_body_hash on memory.facts (project, body_hash) nulls not distinct
  where superseded_by is null and retired_at is null;
create index facts_live_embedding_idx on memory.facts using hnsw (embedding halfvec_cosine_ops)
  where superseded_by is null and retired_at is null;
create index facts_search_text_idx on memory.facts using gin (search_text);
create index facts_session_idx on memory.facts (session_id);   -- the digest job checks for an existing digest

-- Row-level security as a backstop: a role with no policy for an action gets no rows.
alter table memory.facts enable row level security;

-- Exactly what code uses: storing and searching facts, and marking one superseded (section 3.3).
-- No delete, and no update of any other column, so a stored fact's text, vector and dates stay as
-- written. Retention keeps facts, so memory_maintenance gets nothing here.
grant select, insert, update (superseded_by) on memory.facts to memory_agent;
create policy facts_agent_select on memory.facts for select to memory_agent using (true);
create policy facts_agent_insert on memory.facts for insert to memory_agent with check (true);
create policy facts_agent_update on memory.facts for update to memory_agent using (true) with check (true);
