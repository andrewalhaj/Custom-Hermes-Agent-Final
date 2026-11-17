-- 0006: the store says which migration it is at (issue #185).
-- ARCHITECTURE.md 4.3 and 5.5: the startup check compares this number with the one the plugin was built
-- against, and does not start the gateway on a store that is behind. A machine's login cannot read
-- Supabase's own migration history, and a plain Postgres has none, so the store keeps its own number.
-- Every later migration ends with: update memory.schema_level set level = <its own number>;
-- (tests/test_schema_level.py fails a migration that does not).

create table memory.schema_level (
  only_row boolean primary key default true check (only_row),   -- one row, ever
  level    integer not null
);
insert into memory.schema_level (level) values (6);

-- Row-level security as a backstop, as on the other tables: a role with no policy for an action gets no rows.
alter table memory.schema_level enable row level security;
grant select on memory.schema_level to memory_agent;
create policy schema_level_agent_select on memory.schema_level for select to memory_agent using (true);
