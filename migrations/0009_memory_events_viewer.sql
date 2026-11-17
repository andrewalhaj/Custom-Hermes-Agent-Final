-- 0009: a group role that reads events and does nothing else, for the events page (issue #255).
-- scripts/events_viewer.py lists memory.events on the maintainer's own machine. Its login joins this role,
-- so the page cannot write, and cannot read facts. A maintainer creates the login outside this repository,
-- as a machine's is (OPERATIONS.md section 3).

create role memory_viewer nologin;

grant usage on schema memory to memory_viewer;
grant select on memory.events to memory_viewer;
create policy events_viewer_select on memory.events for select to memory_viewer using (true);

update memory.schema_level set level = 9;
