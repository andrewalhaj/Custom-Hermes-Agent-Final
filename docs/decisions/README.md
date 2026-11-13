# Decision records

One file per decision that shapes the build: the context, the options, what was decided and what follows from it. CONTRIBUTING.md states the rules. These files keep the reasons.

A record is never edited to change its decision. A new record replaces it and says which one it supersedes; the old one gets a line pointing to the new one.

| No. | Decision | Status |
|---|---|---|
| [0001](0001-laptop-host-shared-supabase.md) | Hermes runs on the laptop; memory is one shared Supabase project | Accepted |
| [0002](0002-start-memory-empty.md) | Memory starts from an empty database | Accepted |
| [0003](0003-extract-from-user-and-tools-only.md) | Facts are extracted only from the user's messages and tool results | Accepted |
| [0004](0004-failures-loud-and-distinguishable.md) | Every failure is loud and distinguishable | Accepted |
| [0005](0005-log-through-hermes-with-events-table.md) | Log through Hermes's logger, with an `events` table | Accepted |
| [0006](0006-drop-skills-store-and-memory-cap-guard.md) | Drop the skills store and the memory-cap guard | Accepted |
| [0007](0007-eval-set-gates-ranking-changes.md) | The eval set gates every ranking change | Accepted |
