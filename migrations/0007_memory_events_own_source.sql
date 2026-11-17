-- 0007: a machine writes events only under its own name (issue #149).
-- ARCHITECTURE.md 4.3: until now any machine's login could insert any row, so one host could write another
-- host's heartbeat, or a job's success, and hide what the job-health check reports (6.3).
-- A machine's login is named as the machine (OPERATIONS.md section 3: `create role <machine-name>`), and the
-- plugin writes that name to `source`, so the check compares the two. Retention's row, whose source is
-- `database`, is written under memory_maintenance's own policy (0004), which this file does not touch.

-- Rule C10: this tightens a policy a deployed plugin uses. A host set up as documented already writes
-- `source` as its login's name, and the startup check now refuses one that does not (ARCHITECTURE.md 5.5).
alter policy events_agent_insert on memory.events with check (source = current_user);

update memory.schema_level set level = 7;
