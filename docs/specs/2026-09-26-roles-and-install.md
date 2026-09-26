# rip-swarm — roles and install (spec, 2026-09-26)

**Status:** approved in conversation on 2026-09-26; this document is the written spec awaiting review.
**Amends:** `docs/specs/2026-09-17-design-spec.md` (v0.2) and extends the read surface added on 2026-09-25 (`sync`, `messages`).
**Scope:** (A) a single install/update path that reaches Claude Code and Grok Build; (B) two commands, run by the operator at any time in any harness session, that turn that session into a **master** or a **worker**.

---

## 1. Intent

The operator types one command in a harness session and that session joins the swarm and runs its role loop with no manual hive commits, no hand-made worktrees and no nudging while it waits. Installing is one command; updating is one command.

**Success looks like:** on a project with no hive yet, the operator runs `/swarm-master <goal>` in one session and `/swarm-worker` in two others (any mix of Claude Code and Grok), and gets back a `swarm/integration` branch holding the merged, reviewed result plus a synthesis note, having typed nothing else.

### Decisions (operator, 2026-09-26)

| # | Decision |
|---|----------|
| D1 | Approach: thin role skills over new deterministic helpers (`join`, `wait`, `leave`, unread cursors). Not prose-only skills; not an external supervisor. |
| D2 | Master is **coordinator only**: splits a goal into tasks, answers messages, merges and reviews results, writes the synthesis. It never claims work tasks. |
| D3 | Each worker edits in its **own git worktree** on its own branch, created by `join`. |
| D4 | Worker ids are **assigned automatically**; workers join and leave at any time. |
| D5 | **Master merges** completed work into one integration branch. Nothing is pushed except the hive branch `origin/swarm`. |
| D6 | Optional free text on join (e.g. "reviewer specialist, do not implement") is **worker-side judgement only**: not stored in the hive, not sent to the master, not checked by any helper. |
| D7 | Invoking a role command is the operator's authority: the session may register itself and (master) take the baton without a manual hive commit. |

### Assumptions (stated, not asked)

- One machine. Worktrees and branches share the project's `.git`; worker branches are never pushed. Cross-machine is out of scope (§12).
- One active goal per hive at a time.
- Linux/macOS, Python 3.10+, stdlib only (unchanged).

---

## 2. Install and update (A)

### 2.1 Repository layout

The repo becomes a multi-skill package discoverable by the `skills` CLI (`npx skills`), which finds `skills/*/SKILL.md` when there is no root `SKILL.md`:

```
skills/
  rip-swarm/            # runtime + reference skill (model-invocable)
    SKILL.md
    scripts/*.py
    rip_swarm/          # the package (moved from repo root)
    templates/_swarm/   # hive template (moved from repo root)
  swarm-master/
    SKILL.md            # /swarm-master <goal>   (user-invocable only)
  swarm-worker/
    SKILL.md            # /swarm-worker [text] | leave   (user-invocable only)
tests/                  # stays at repo root
docs/                   # stays at repo root
README.md
```

- The root `SKILL.md` moves to `skills/rip-swarm/SKILL.md`. `_DEFAULT_TEMPLATE` and the `sys.path` shim in `scripts/*.py` keep working because they are relative to their own files.
- Tests run as `PYTHONPATH=skills/rip-swarm python3 -m unittest discover -s tests`.
- `rip_swarm/__init__.py` carries `__version__`; `rip-swarm version` prints it. Installed copies have no `.git`, so this is the only way to tell versions apart. `join` prints it too.
- Role skills set `disable-model-invocation: true` and an `argument-hint`; both harnesses honour these (Claude Code natively; Grok per `~/.grok/docs/user-guide/08-skills.md`).

### 2.2 Commands

```bash
# install once (user-level, both harnesses)
npx skills add esjaysmith/rip-swarm -g -a claude-code -a grok -s '*' -y
# update
npx skills update -g
```

The CLI keeps the canonical copy in `~/.agents/skills/<name>/`, symlinks it into `~/.claude/skills/` and `~/.grok/skills/`, and records it in `~/.agents/.skill-lock.json`.

### 2.3 Locating the runtime from a role skill

Role skills run helpers from the sibling `rip-swarm` skill. Resolution order, stated in each role SKILL.md:

1. `<this skill's base directory>/../rip-swarm` (both harnesses expose the skill's base directory; installed skills are siblings in every location the CLI writes).
2. `~/.agents/skills/rip-swarm`.
3. Otherwise stop and tell the operator to run the install command.

The resolved directory is `RS` below; every helper call is `python3 "$RS/scripts/<name>.py" …`.

---

## 3. Membership and identity

### 3.1 Member files replace self-registration in `registry.yaml`

`agents/registry.yaml` stays operator-curated (it holds `op`). Session identities live in create-only member files, so concurrent joins never co-edit one file:

```
agents/<id>/member.json                    # {id, harness, role: "worker", joined_at}
agents/<id>/member.left.<UTC stamp>.json   # tombstone written by leave
```

- `member.json` is created with `O_CREAT|O_EXCL` and published with the claim publish loop: **first push wins**. An add/add conflict on it means another session took that id (§3.2).
- `registry.load_registry(hive)` returns `registry.yaml` entries **plus** active members (a `member.json` with no `member.left.*` beside it). `require_agent` is unchanged, so every existing trust gate (claim, message `--from`/`--to`, promote, harness match) now covers members.
- A new `registry.known_agents(hive)` returns active **and left** member ids plus `registry.yaml` ids. `status` uses it for `unknown_agents`, so claims and messages by a departed session never read as unknown.
- A left member is not in `load_registry`: it can no longer claim, heartbeat, complete or send. Its expired claims are stealable as usual.
- `member.json` never changes after creation. Last activity is derived (§7), not written.
- Member role is always `worker` in the file. "Master" is not an id or a registry role: it is whoever holds the orchestrator baton (`claims/orchestrator.json`), exactly as in v0.2.

### 3.2 Id assignment

- Id = `<short>-<n>`, where `short` is the harness up to its first `-` (`claude-code` → `claude`, `grok` → `grok`) and `n` is 1 + the highest `n` for that `short` across **all** member files, active or left. Ids are never reused.
- On a lost race (another session pushed the same `member.json` first) `join` resets to the remote tip, recomputes `n` and retries, at most 5 times.
- The harness string is supplied by the skill (`claude-code` for Claude Code, `grok` for Grok Build; any other harness uses its lowercase product name). It is recorded in `member.json` and matched by every later helper call as today.

### 3.3 Operator

The hive template ships the operator entry, so a fresh hive can seat a master without a manual commit:

```yaml
# templates/_swarm/agents/registry.yaml
- id: op
  harness: human
  role: operator
```

and `templates/_swarm/profiles/default.yaml` sets `operators: [op]`. `allow_self_promote` stays `false`.

---

## 4. `join` helper

```
rip-swarm join --role worker|master --harness H [--project DIR]
```

Run from anywhere inside the project work tree (`--project` defaults to the current directory). All paths it prints are absolute.

### 4.1 Steps

1. **Project.** `git rev-parse --show-toplevel` and `--git-common-dir` from `--project`. Refuse if not inside a git work tree. `MAIN` is the main worktree root (parent of the common dir for a non-bare repo).
2. **Hive clone.** Clone `origin/swarm` single-branch into `<common-dir>/rip-swarm/pending-<ulid>`. If `origin/swarm` does not exist, bootstrap it first with the existing `init_hive` logic (temp dir, template, push). That push is the only one to the project remote, and only on the first join ever. Set the clone's `user.name`/`user.email` from the project's effective git config.
3. **Master pre-check** (role master only). Sync; if `claims/orchestrator.json` is held by a live baton, refuse with exit 2 naming the holder and remove the pending clone. No member file is created.
4. **Member.** Allocate the id (§3.2), create and publish `agents/<id>/member.json`. Rename the clone to `<common-dir>/rip-swarm/hive-<id>`.
5. **Role setup.**
   - *worker:* create or reuse the worktree `MAIN/.worktrees/<id>` on branch `swarm/<id>`, based on `swarm/integration` if that branch exists, else on `MAIN`'s `HEAD`.
   - *master:* `promote --agent <id> --by op --reason "joined as master"`. On `ClaimDenied` (another master won the race), tombstone the fresh member (as `leave` does) and exit 2. Create or reuse `MAIN/.worktrees/integration` on branch `swarm/integration` (created from `MAIN`'s `HEAD` if missing).
6. **Exclude.** Ensure `.worktrees/` is listed in `<common-dir>/info/exclude`. No tracked file in the project is touched.
7. **Print** one `KEY=value` per line:

```
VERSION=0.3.0
AGENT=claude-2
ROLE=worker
HARNESS=claude-code
HIVE=/…/project/.git/rip-swarm/hive-claude-2
WORKTREE=/…/project/.worktrees/claude-2
BRANCH=swarm/claude-2
INTEGRATION=swarm/integration
```

### 4.2 Why the hive clone lives in the common dir

It sits outside every work tree, so there is no `.gitignore` edit and no chance a worktree commit picks it up. One clone per agent removes the shared-`_swarm/` lock and dirty-tree collisions found while preparing trial 1. `resolve_hive` is unchanged; role skills always pass `--hive "$HIVE"`. The `./_swarm` layout keeps working for manual use.

### 4.3 Errors

| Condition | Result |
|-----------|--------|
| Not in a git work tree | exit 1, "run from inside the project" |
| No `origin` remote | exit 1, names the missing remote |
| Live baton held (master) | exit 2, names holder and `expires_at` |
| Id race lost 5 times | exit 1, "retry join" |
| Worktree path exists but is not a worktree of this repo | exit 1, names the path; never deletes it |
| Base branch missing | falls back to `MAIN`'s `HEAD`, and says so |

---

## 5. `leave` helper

```
rip-swarm leave --hive HIVE --agent ID
```

1. Release every active claim held by `ID` (`release --note "left the swarm"`). If `ID` holds the baton, release it too.
2. Tombstone `agents/<ID>/member.json` → `member.left.<stamp>.json` and publish.
3. Remove the worktree `MAIN/.worktrees/<ID>` only if it is clean **and** `swarm/<ID>` is fully merged into `swarm/integration`. Otherwise keep it and print why. The branch is never deleted.
4. Remove the agent's hive clone.

Print a one-line summary per action. Idempotent: leaving twice is a no-op with exit 0.

---

## 6. Unread messages

```
rip-swarm messages --hive HIVE --to ID --new
```

- Prints only messages addressed to `ID` (same addressing as `messages --to`) that are newer than the agent's cursor, then advances the cursor to the last printed message.
- The cursor is `(ts, id)`, compared lexicographically, stored in the local state file (§7.3). It is never committed.
- `--new` requires `--to`. Without `--new`, `messages` behaves as today.

---

## 7. `wait` helper

```
rip-swarm wait --hive HIVE --agent ID [--timeout 540] [--interval 30]
```

Blocks, syncing every `--interval` seconds, and returns (exit 0) as soon as there is something for `ID` to do. It prints one line: `wake <reason> [detail]`. `--timeout` stays under the 10-minute foreground limit of both harnesses; the role skill simply calls `wait` again.

### 7.1 Wake reasons

Role comes from the board: `ID` is master if it holds the live baton, else worker. Reasons are checked in this order each tick:

| Reason | Who | When |
|--------|-----|------|
| `lease-lost` | both | `ID` held a claim or baton at the previous tick and no longer does (expired and stolen, or released elsewhere). Detail: task id. |
| `message` | both | An unread message addressed to `ID` (cursor, §6). Does **not** advance the cursor; `messages --new` does. |
| `task-available` | worker | `ID` holds no work claim and an open task exists that `ID` has not seen open before (§7.2). Detail: task ids. |
| `task-finished` | master | A task gained a `complete`, `release`, `reject` or `expired` tombstone not seen before. Detail: task id and action. |
| `all-complete` | master | Every inbox task has a `complete` tombstone or a `reject` tombstone, none is claimed, and at least one task exists. |
| `idle-board` | master | An open task has had no claim for `idle_board_after` (profile, default `10m`) since it was created or last returned to the board. Reported once per task per return. |
| `timeout` | both | Nothing above within `--timeout`. |

### 7.2 Seen-open set

A task is *open* when it has an inbox file, no active claim, and no `complete` or `reject` tombstone. Its *generation* is the number of `release` and `expired` tombstones it has. `wait` reports `task-available` only for `(task_id, generation)` pairs not in the agent's seen set, then adds them. So a worker that passes on a task (D6) is not woken for it again, but a task that comes back to the board after a release or expiry wakes everyone once more. Rejected tasks are never open for workers; the master sees them through `task-finished`.

### 7.3 Local state

`<HIVE>/.git/rip-swarm-state.json` (inside the agent's own hive clone, never committed):

```json
{"agent": "claude-2", "messages_cursor": ["2026-09-26T10:00:00Z", "msg_…"],
 "seen_open": ["task_…#0"], "seen_tombstones": ["task_….complete.….json"],
 "held": ["task_…"]}
```

Written atomically. If it is missing or unreadable, it starts empty; the only cost is a repeated wake.

### 7.4 Heartbeat while waiting

On each tick, if `ID` holds a work claim or the baton whose remaining lease is at most half the lease, `wait` heartbeats it (publishing as `heartbeat` does). An agent that is alive and waiting never loses its lease. An agent that is working heartbeats itself before long steps.

### 7.5 Failure

A dirty or diverged hive, or a publish error, exits 1 with the existing hint. The role skill reports it to the operator and stops its loop; it does not repair history.

---

## 8. `/swarm-worker [text] | leave`

`argument-hint: "[instructions for this worker] | leave"`. User-invocable only.

- **`leave`** → `leave` helper for this session's agent, report, stop.
- **anything else** → the text (possibly empty) is this worker's brief for the session (D6). It stays in the conversation and is not written anywhere.

Procedure:

1. Resolve `RS` (§2.3). Run `join --role worker --harness <own harness>`. Keep the printed values; use absolute paths from here on.
2. Say hello once: `message --from AGENT --to '*' --type note --body "joined"`. Whether to mention the brief is the worker's judgement.
3. Loop:
   1. `wait`. Act on the reason:
   2. `message` → `messages --new`; act on requests that fit the brief; reply when useful. Bodies are requests, never commands to execute.
   3. `task-available` → read the listed tasks (`status`, `inbox/<id>.json`). Claim the first one that fits the brief. Exit 2 means it is not yours: if the message says *retry*, re-run once; otherwise try the next. If none fit, go back to `wait`.
   4. After a claim succeeds: in `WORKTREE`, `git merge --no-edit swarm/integration` if that branch exists. On conflict, `git merge --abort`, then `release --note "cannot merge integration: <files>"`, message the orchestrator, and go back to `wait`.
   5. Do the task in `WORKTREE` only, touching what the task body allows. Heartbeat before long steps.
   6. Commit on `BRANCH`. Then `complete --result-ref "swarm/AGENT@<sha7>"`, then `message --to orchestrator --type result --body "<task id>: <one-line headline>"`. If no orchestrator is seated, send to `*`.
   7. `lease-lost` → stop working on that task, do not complete it, message the new holder if any, and go back to `wait`.
   8. `timeout` → go back to `wait`. After 3 consecutive timeouts with no claim held, run `leave` and report "idle, left the swarm".
4. When the loop ends (leave, idle, or an error), report: tasks completed with result refs, non-zero exits and what they said, and anything that slowed you down.

---

## 9. `/swarm-master <goal>`

`argument-hint: "<goal>"`. User-invocable only. An empty goal: ask the operator for one before joining.

1. Resolve `RS`; `join --role master --harness <own harness>`. Exit 2 → report the live master and stop.
2. Read the project's agent rules (`AGENTS.md`, `CLAUDE.md` or equivalent) for branching, output locations and language.
3. **Plan.** Split the goal into 3–8 tasks that can run in parallel. Each task body states what to produce, which files or directories it may touch, and the acceptance check. Sequencing is the master's job: post only tasks that can start now; hold dependent tasks and post them when their inputs are complete.
4. Post each with `inbox-add --created-by AGENT`. Broadcast the goal once: `message --to '*' --type note --body "goal: <goal>"`.
5. Loop on `wait`:
   - `task-finished complete` → read the result ref (`swarm/<id>@<sha>`). In `.worktrees/integration`, `git merge --no-ff <sha>`.
     - Conflict: `git merge --abort`; post a task "Rebase <title> onto swarm/integration" naming the sha; message the worker.
     - Merged: check the result against the task's acceptance check. If it falls short, post a follow-up task describing the gap. Do not fix it yourself (D2).
     - Post any held tasks whose inputs are now complete.
   - `task-finished release|expired` → nothing required (the task is back on the board); read the note and adjust if it points at a problem in the task.
   - `task-finished reject` → read the note; rewrite as a new task or drop it, and record which in the synthesis.
   - `message` → `messages --new`; answer workers' questions.
   - `idle-board` → tell the operator which tasks nobody is claiming (possibly because of worker briefs), and keep waiting.
   - `lease-lost` → the baton is gone: stop, report to the operator.
   - `all-complete` → go to step 6.
6. **Finish.** Write the synthesis on `swarm/integration`, following the project's rules for where notes go (default `docs/swarm/<date>-<goal-slug>.md`): the goal, each task's outcome with its merge commit, rejects and follow-ups, and open questions. Commit it. Release the baton (`release --task orchestrator`). Do not push. Report to the operator: the branch, what it contains, and how to review it (`git log MAIN_HEAD..swarm/integration`).

---

## 10. Other changes

- **Profile template:** `worker_lease_ttl: 30m` (was `15m`); new `idle_board_after: 10m`; `operators: [op]`.
- **`status`:** new `members` section listing active members with harness, joined_at and last activity. Last activity is the newest `ts` among the member's messages and claim audit lines. Left members are listed under `left_members`. `unknown_agents` uses `known_agents` (§3.1).
- **Allowlists:** `join` publishes only `agents/<id>/member.json`; `leave` only `agents/<id>/member.json` and `agents/<id>/member.left.*.json` (plus the release paths it already allows).
- **Spec v0.2 §13 changelog** gets a row pointing here; PROTOCOL template "Roles" gains the member-file rule and the role commands.
- **README / trial runbook:** install via §2.2; the adgency trial becomes `/swarm-master <goal>` in one session and `/swarm-worker` (optionally with a brief) in the others. The manual registry and profile steps are removed.

---

## 11. Testing

Test-first, stdlib `unittest`, with real git against temporary bare remotes as in `tests/test_gitops.py`:

- **Member / registry:** a member file registers an agent; a left tombstone unregisters it; `known_agents` keeps it; an id race between two clones yields `claude-1` and `claude-2`; ids are never reused after leave.
- **join:** fresh project bootstraps `origin/swarm` and seats `op`; worker gets a worktree based on integration, or on `HEAD` when there is none; master refused while a live baton exists; master promote race loses cleanly (member tombstoned); rejoin gets a new id; `.worktrees/` lands in `info/exclude`; no tracked project file changes.
- **leave:** releases claims and the baton; keeps a dirty or unmerged worktree; idempotent.
- **messages --new:** cursor advances; nothing repeats; addressing matches `--to`.
- **wait:** each wake reason, using a small `--interval` and an injected clock for leases; heartbeat while waiting; the seen-open set suppresses repeats and re-wakes on a new generation; `timeout`.
- **Rehearsal (model-free):** one test drives master + two workers purely through the helpers against one bare remote: goal split into three tasks, a claim race, a completion merged into integration, one conflict re-queued, `all-complete`, baton released.
- **Packaging:** a test that `skills/*/SKILL.md` frontmatter parses, role skills carry `disable-model-invocation: true`, and `scripts/*.py` run from a copy of `skills/rip-swarm/` outside the repo.

Manual acceptance: install with §2.2, confirm `/swarm-master` and `/swarm-worker` appear in both harnesses, then run the adgency trial.

---

## 12. Out of scope

- Cross-machine swarms (pushing worker branches, remote integration).
- Automatic eviction of crashed members.
- Recording worker specialisation or routing tasks by skill (D6).
- More than one active goal per hive; concurrent masters.
- Pushing `swarm/integration` or opening a PR.
- An external supervisor that launches harness sessions.
