# rip-swarm

Cross-harness agent swarm coordination via a **git-backed hive** (not a hosted queue). Each project keeps its hive on a dedicated **`swarm` branch** of its own repo, cloned single-branch into **`_swarm/`** (gitignored on code branches) and pushed to the project’s normal remote.

**Spec:** [docs/specs/2026-09-17-design-spec.md](docs/specs/2026-09-17-design-spec.md) (v0.2)  
**Plan:** [docs/plans/2026-09-17-rip-swarm.md](docs/plans/2026-09-17-rip-swarm.md)  
**Discussion:** [docs/specs/2026-09-17-hive-checkout.md](docs/specs/2026-09-17-hive-checkout.md) — `swarm` branch vs worktree vs nested clone.

Pillars: A install (`SKILL.md` / skills.sh-class) · B munder-shaped hive house, multi-push concurrency · C create-only claim files, first push wins (OpenMOSS-inspired leases; JSONL is audit) · D profiles (+ default) · E `/lookback` + `/status`.

Status: spec + plan only — skill not built yet.
