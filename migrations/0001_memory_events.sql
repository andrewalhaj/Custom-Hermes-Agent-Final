-- 0001: the memory schema, the events table and the two group roles (issue #33).
-- ARCHITECTURE.md sections 4.2 and 4.3. Applied migrations are never edited; a change is a new file.

create schema memory;   -- not exposed through the Supabase Data API

create table memory.events (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  source      text not null,                 -- the machine
  project     text,
  session_id  text,
  turn        integer,
  code        text not null,                 -- a code from the failure catalog, or an event name
  result      text not null check (result in ('ok', 'empty', 'failed', 'degraded')),
  reason      text,
  detail      jsonb not null default '{}'    -- IDs, scores, counts and hashes only; never content
);

create index events_at_idx on memory.events (at);
create index events_code_idx on memory.events (code, at);

-- Row-level security as a backstop: a role with no policy for an action gets no rows.
alter table memory.events enable row level security;

-- Group roles. Each machine's login role joins memory_agent; the retention job's login joins
-- memory_maintenance. Logins and their passwords are created outside this repository.
create role memory_agent nologin;
create role memory_maintenance nologin;

grant usage on schema memory to memory_agent, memory_maintenance;

grant select, insert on memory.events to memory_agent;
create policy events_agent_select on memory.events for select to memory_agent using (true);
create policy events_agent_insert on memory.events for insert to memory_agent with check (true);

grant select, delete on memory.events to memory_maintenance;
create policy events_maintenance_select on memory.events for select to memory_maintenance using (true);
create policy events_maintenance_delete on memory.events for delete to memory_maintenance using (true);
