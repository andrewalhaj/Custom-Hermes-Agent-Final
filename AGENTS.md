# Working in the custom Hermes build repository

For anyone, person or AI agent, who changes this repository. This file points at the repository's
documents and restates their rules. If it and `CONTRIBUTING.md` differ, `CONTRIBUTING.md` wins.

The repository is a custom build of Hermes Agent (v0.23.1): one plugin, `custom_build`, with the memory
provider, four guards and the failure module, on an unmodified Hermes. Several machines share one Postgres
store. The repository is public: no secret, hostname, project reference or chat ID goes in it.

## Read before you change anything
1. `CONTRIBUTING.md`, in full, first. It holds the rules and the workflow.
2. `ARCHITECTURE.md`, the sections for the part you change: 2 provider contract, 3 retrieval and the eval
   set, 4 the store, 5 guards, 6 failure handling and the code catalog, 7 logging.
3. `OPERATIONS.md`, before touching deployment, machine setup, scheduled jobs, migrations or
   `scripts/deploy.py`: sections 3 to 8.

`README.md` is only an index. The documents describe the design and can lag the code: when they disagree
with the code, the code is the fact; say so and open an issue. Facts about Hermes are for v0.23.1; re-check
before relying on them with another version.

## Start of a task
- `git config core.hooksPath .githooks` once per clone (gitleaks pre-commit; never `--no-verify`).
- `git fetch --prune`, `git switch main`, `git merge --ff-only origin/main`, then branch `<type>/<short-name>`.
- Stage by path. Never `git add -A`, `git add .` or `git add -f`. The `.gitignore` is an allow-list: a new
  path it does not list needs a line in the same pull request, with a case in `tests/test_gitignore.py`.

## Workflow rules (CONTRIBUTING.md 4.1)
- W1 Issue first: every pull request answers one issue; the issue is the whole scope.
- W2 One branch per issue, fresh from an up-to-date `main`.
- W3 Failing test first; watch it fail for the right reason. Commit the red test on its own, "(red)" in the subject.
- W4 One concern per commit, conventional prefix: feat, fix, refactor, test, chore, docs, ci.
- W5 Small pull requests, tests included.
- W6 Review the whole diff before opening; record who, how many rounds, the verdict.
- W7 PR body: `Closes #n`, Commits, Review, Validation, Found not fixed (template in 4.5).
- W8 Never merge, never enable auto-merge, never push to `main`.
- W9 Never reset hard, force-push or delete a branch without a maintainer's yes.
- W10 Repository, account and database settings are a maintainer's: propose with what, risk, rollback.
- W11 Verify before claiming: every statement about remote state comes from a fresh read.
- W12 Scope is the issue. Anything else goes under "Found, not fixed", each with its own issue.
- W13 Never commit secrets.
- W14 End clean: no uncommitted edits, stashes or untracked files.

## Code rules (CONTRIBUTING.md 5.1, 6, 7)
- C1 Catch the error you expect, by name; the `try` covers only the line that raises it.
- C2 No `except Exception`, `except BaseException` or bare `except:`; never suppress BLE001, S110 or E722.
  Only the two wrappers in the failure module catch everything.
- C3 A failure never looks like an empty result. A missing setting is a startup error.
- C4 Let it crash; add handling only when the issue asks, and still record the failure.
- C5 "Never raises", "fail open", "best-effort" must say which failures, which code, how a caller tells.
- C6 A guard or check counts only after it has blocked its own failing case.
- C7 Data this repo did not create is not ours to alter, rename or drop.
- C8 Retrieval changes report eval-set numbers before and after.
- C9 Code from elsewhere meets these rules in the same pull request.
- C10 A migration must work with the plugin deployed before it.
- F1 to F6: every failure has a code from the catalog; failed, empty and degraded differ; each is logged at
  WARNING or above and written to `events` through the one helper; a failure that changed the answer shows in
  the turn; the catalog is enforced by a test.
- L1 to L5: `logging.getLogger(__name__)` only; events through the one helper; redact before every write;
  no content in logs.

## About this file
Hermes loads the first of `.hermes.md`, `HERMES.md`, `AGENTS.override.md`, `AGENTS.md`, `CLAUDE.md` and
`.cursorrules` as project context when a session runs inside a repository, and `SOUL.md` only from its own
home. So this file holds development rules and nothing about the running agent's identity or behaviour; that
is `SOUL.md`, installed in the Hermes home, never committed here. Do not add `.hermes.md`, `HERMES.md` or
`AGENTS.override.md` (they would load instead of this file), or `CLAUDE.md` or `.cursorrules` (a second copy
of the rules to keep in step). Keep this file short: it is read every turn.

## Briefs, and when you are blocked
- End every brief you write for another agent with this line, verbatim: "Error handling rule: catch the
  specific exception you expect, by name. Never return a value that also means "nothing found" or "skipped"
  for a failure; log every caught failure with its failure code, and let anything unexpected crash."
- If a guard or permission blocks you, do not look for another route to the same result. Stop, say what was
  blocked and that nothing changed, give a maintainer one command with what it does, the risk and the
  rollback, then read the state back and report it (CONTRIBUTING.md 8.3).
- A person is accountable for every pull request, whoever wrote the code.
