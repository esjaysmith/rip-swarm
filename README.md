# rip-swarm

A git-backed hive for coordinating coding-agent harnesses (Claude Code, Grok Build, …) on one project. Not a hosted queue.

The hive is an orphan **`swarm` branch** on the project's own `origin`. Every session works in its own clone of that branch, at `<project>/.git/rip-swarm/hive-<id>`. Claims are create-only files, and the first push wins. Results are merged by the master into `rip-swarm/integration`, and nothing but `origin/swarm` is ever pushed.

Python 3.10+, stdlib only; git ≥ 2.31. Linux and macOS.

## Install (once, both harnesses)

```bash
npx skills add esjaysmith/rip-swarm -g -a claude-code -a grok -s '*' -y
```

This installs three skills into `~/.agents/skills/` and links them into `~/.claude/skills/` and `~/.grok/skills/`:

| Skill | What it is |
|-------|------------|
| `rip-swarm` | helpers plus reference |
| `/swarm-master` | operator-invoked master role |
| `/swarm-worker` | operator-invoked worker role |

Update:

```bash
npx skills update rip-swarm swarm-master swarm-worker -g
```

## Use

In any session inside the project, in either harness:

| Type | The session… |
|------|--------------|
| `/swarm-master <goal>` | becomes the coordinator. It splits the goal into tasks, reviews each result off `rip-swarm/integration`, merges and accepts it, and writes a synthesis on `rip-swarm/integration`. |
| `/swarm-worker` | becomes a worker with an automatic id (`claude-1`, `grok-2`, …) in its own worktree `.worktrees/<id>`. It claims, works, completes, and waits in the background. |
| `/swarm-worker reviewer, do not implement` | the same, with a private brief the worker uses to choose tasks. |
| `/swarm-worker leave` | releases its claims and leaves. |

The first join on a project creates `origin/swarm`. Nothing else in the project changes: `.worktrees/` goes into `.git/info/exclude`, and no tracked file is touched. Review the result with `git log <base>..rip-swarm/integration`, and merge it like any branch.

## Helpers

All live in `skills/rip-swarm/scripts/` and run as `python3 <script> …` from any directory:

| Script | Purpose |
|--------|---------|
| `join.py` / `leave.py` | Become worker or master; leave cleanly |
| `wait.py` | Block (in the background) until there is something to do; prints `wake <reason>` |
| `inbox.py` | Post a task (`--after`, `--fixes`) |
| `claim.py` | Claim / heartbeat / complete / release / reject |
| `accept.py` | Master: record a merged, accepted task |
| `message.py` / `messages.py` | Send a message / read unread ones (`--to ID --new`) |
| `sync.py` / `status.py` | Refresh the clone / read-only doctor |
| `promote.py`, `lookback.py`, `init.py`, `version.py` | Baton handoff, reports, manual hive setup, version |

The full reference is in [skills/rip-swarm/SKILL.md](skills/rip-swarm/SKILL.md).

Exit codes: `0` ok, `1` failure, `2` refused (not yours, blocked, finished, or another master), `3` a `wait` is already running for this agent.

## Hives created before 0.3

A master needs `op` in `agents/registry.yaml` and in `profiles/default.yaml` `operators`. Hives bootstrapped by 0.3 have both. `join --role master` prints the two-line edit to publish when they are missing.

## Tests

```bash
PYTHONPATH=skills/rip-swarm python3 -m unittest discover -s tests
```

## Docs

- [docs/specs/2026-09-26-roles-and-install.md](docs/specs/2026-09-26-roles-and-install.md): roles and install spec (approved), with its review history
- [docs/specs/2026-09-17-design-spec.md](docs/specs/2026-09-17-design-spec.md): protocol spec v0.2
- [docs/plans/2026-09-25-first-trial.md](docs/plans/2026-09-25-first-trial.md): first trial runbook
