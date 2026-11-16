-- 0004: retention of events, run inside the database (issue #95).
-- ARCHITECTURE.md 4.3 and OPERATIONS.md section 4: a function owned by memory_maintenance, run daily by
-- Supabase's pg_cron, so no host holds a login that can delete. 0001's comment that "the retention job's
-- login joins memory_maintenance" is out of date: no login joins it; this function runs as it.
-- Session chunks have no table yet; their retention arrives with it. Facts and digests are kept.

-- To record each run (#92): the policy admits retention's own success row and nothing else.
grant insert on memory.events to memory_maintenance;
create policy events_maintenance_insert on memory.events for insert to memory_maintenance
  with check (code = 'job.succeeded' and detail->>'job' = 'retention');

create function memory.retain_events() returns void
  language plpgsql
  security definer
  set search_path = pg_catalog, pg_temp   -- the PostgreSQL documentation's advice for security definer
as $$
declare
  old integer;
  removed integer;
begin
  -- guard.bypassed rows stay: the heartbeat records a session it finds no row for again (#95).
  select count(*) into old from memory.events
    where at < now() - interval '90 days' and code <> 'guard.bypassed';
  delete from memory.events where at < now() - interval '90 days' and code <> 'guard.bypassed';
  get diagnostics removed = row_count;
  -- The job-health check reads job, input and output from job.succeeded (ARCHITECTURE.md 6.1).
  insert into memory.events (source, code, result, detail)
    values ('database', 'job.succeeded', 'ok',
            jsonb_build_object('job', 'retention', 'input', old, 'output', removed));
end
$$;

-- Postgres grants execute to PUBLIC on a new function; no machine's login may run this one.
revoke execute on function memory.retain_events() from public;
-- Supabase's postgres is no superuser: to hand the function to a role it created it must be able to SET
-- ROLE to it, and that role needs create on the schema, for the change only (Supabase knowledge base,
-- "Can't revoke a role membership granted by supabase_admin"). Neither is kept. As the new owner, the
-- role grants execute to the login applying this migration (Supabase's postgres), which schedules the
-- pg_cron job that runs it.
grant create on schema memory to memory_maintenance;
grant memory_maintenance to current_user with set true, inherit false;
alter function memory.retain_events() owner to memory_maintenance;
set role memory_maintenance;
grant execute on function memory.retain_events() to session_user;
reset role;
revoke memory_maintenance from current_user;
revoke create on schema memory from memory_maintenance;
