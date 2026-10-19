# PROJECT.md — rip-swarm

Durable project decisions and insights. Standing rule (Esjay, 2026-10-19): lasting decisions live in the repo
(this file or `docs/`), not only in bot memory. Kept thin; the specs and plans hold the detail.

## What it is

A git-backed hive for coordinating several coding-agent harnesses (Claude Code, Grok Build, …) on one project: an
orphan `swarm` branch on the project's own origin is the board, claims are create-only files (first push wins), and a
master merges results into `rip-swarm/integration`. Shipped as Agent Skills (`rip-swarm`, `/swarm-master`,
`/swarm-worker`); Python 3.10+ stdlib only.

## Status (2026-10-19)

- Branch: `master` (tracks `origin/master`), version 0.3.0. Last commits 2026-09-28 (merged `feat/min-reviews`,
  `fix/cascade-wake`). `feat/message-surface` is merged.
- Remote: `git@github.com:esjaysmith/rip-swarm.git`
- Install: `npx skills add esjaysmith/rip-swarm -g -a claude-code -a grok -s '*' -y`.

## Rulings and decisions

| Date | Ruling |
|---|---|
| 2026-09-17 | v0 locked pillars and implementation defaults (`docs/specs/2026-09-17-design-spec.md` §1, §13). No hosted queue. |
| 2026-09-26 | Role commands and install via skills (`docs/specs/2026-09-26-roles-and-install.md`). |
| 2026-09-27 | Execution proposals (cascade, heartbeat, result messages, minimum review rounds) implemented; implementation rulings folded into the spec as revision 10 (`docs/specs/2026-09-26-execution-proposals.md`). |

## Insights worth keeping

- Multi-harness `git push` is a known-weaker concurrency model; exclusivity comes from create-only claim paths.
- Known limits (message cursor, wait lock, never-pruned prev refs, …) are listed in `docs/plans/2026-09-25-first-trial.md`.

## Open loops

- First trial (on nr-adgency, runbook `docs/plans/2026-09-25-first-trial.md`): no results write-up is in the repo; outcome unknown.

## Pointers

- `README.md` — install, use, helpers.
- `skills/rip-swarm/SKILL.md` — full reference.
- `docs/specs/`, `docs/plans/` — design spec, roles/install, execution proposals, reviews, first-trial runbook.
