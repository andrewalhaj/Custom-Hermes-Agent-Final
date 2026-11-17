-- 0008: one live digest per session (issue #156, item 4; ARCHITECTURE.md 3.3 and 4.3).
-- Two ends of one session at once (an eviction and the gateway stopping) could each read "no live digest" and
-- each store one. The store now keeps at most one live digest per session, checked at commit. The plugin
-- inserts a new digest and supersedes the old one in one transaction, so a check at each statement would refuse
-- the plugin deployed before this file (CONTRIBUTING.md rule C10); checked at commit, it never sees two there.

-- A store that already holds the race's result: every live digest of a session but its newest is superseded by
-- the newest, so the constraint can be added.
with newest as (
  select distinct on (session_id) id, session_id
  from memory.facts
  where kind = 'digest' and session_id is not null and superseded_by is null and retired_at is null
  order by session_id, created_at desc, id desc
)
update memory.facts as f set superseded_by = newest.id
  from newest
  where f.session_id = newest.session_id and f.kind = 'digest' and f.id <> newest.id
    and f.superseded_by is null and f.retired_at is null;

alter table memory.facts add constraint facts_one_live_digest_per_session
  exclude using btree (session_id with =)
  where (kind = 'digest' and superseded_by is null and retired_at is null)
  deferrable initially deferred;

update memory.schema_level set level = 8;
