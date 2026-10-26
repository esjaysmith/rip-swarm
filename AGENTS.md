# AGENTS.md

Read [PROJECT.md](PROJECT.md) for project context before changing this repo. PROJECT.md notes that multi-harness `git push` is a known-weaker concurrency model and that exclusivity comes from create-only claim paths.

## Keep the repo in sync with GitHub (SJ, 2026-10-26)

GitHub (`origin`, `github.com/esjaysmith/rip-swarm`) is the source of truth. The default branch is `master`. This replaces any scheduled fleet sync of `~/src`.

- At the start of a session, `git pull --ff-only` on the branch you will work on. If it does not fast-forward, stop and ask SJ.
- Push after every commit (including new feature branches). Never end a session with unpushed commits or with notes/memory that only exist outside the repo.
- Push by default once unit and integration tests that this repo defines are green. If the repo has no test suite, push the docs/notes change after a quick sanity check of what you edited.
- Merge pull requests into `master` only after running the full unit and integration suite this repo defines and seeing it pass (never on a red or skipped suite). If the repo has no suite, say so in the PR and merge only when SJ (or the standing rule for that repo) allows it.
- Force-pushes and rewriting already-pushed history need SJ's explicit go. Never force-push `master`.
- Write lasting project decisions into the repo (`PROJECT.md`, `docs/`, or this file) and commit them — never leave them only in bot memory or a chat.
