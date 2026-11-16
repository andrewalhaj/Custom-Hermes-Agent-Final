-- 0003: let a machine's login retire a fact (issue #142).
-- ARCHITECTURE.md sections 3.3 and 4.3: a column that code starts to update is granted with that code.
-- A fact removed from built-in memory is retired with a reason, never deleted; the row stays.

grant update (retired_at, retired_reason) on memory.facts to memory_agent;
