# rip-swarm — roles and install (spec, 2026-09-26)

**Status:** revision 3, folding the second review (§15). Dispositions: §14 for §13, and §16 for §15. Awaiting operator review. Decisions D1–D7 stand.
**Amends:** `docs/specs/2026-09-17-design-spec.md` (v0.2) and extends the read surface added on 2026-09-25 (`sync`, `messages`).
**Scope:** (A) a single install/update path that reaches Claude Code and Grok Build; (B) two commands, run by the operator at any time in any harness session, that turn that session into a **master** or a **worker**.

---

## 1. Intent

The operator types one command in a harness session. That session joins the swarm and runs its role loop, with no manual hive commits, no hand-made worktrees and no nudging while it waits. Installing is one command, and so is updating.

**Success looks like:** on a project with no hive yet, the operator runs `/swarm-master <goal>` in one session and `/swarm-worker` in two others (any mix of Claude Code and Grok). They get back a `rip-swarm/integration` branch holding the merged, reviewed result plus a synthesis note, having typed nothing else.

### Decisions (operator, 2026-09-26)

| # | Decision |
|---|----------|
| D1 | Approach: thin role skills over new deterministic helpers (`join`, `wait`, `leave`, unread cursors). Not prose-only skills, and not an external supervisor. |
| D2 | Master is **coordinator only**: it splits a goal into tasks, answers messages, merges and reviews results, and writes the synthesis. It never claims work tasks. |
| D3 | Each worker edits in its **own git worktree** on its own branch, created by `join`. |
| D4 | Worker ids are **assigned automatically**. Workers join and leave at any time. |
| D5 | **Master merges** completed work into one integration branch. Nothing is pushed except the hive branch `origin/swarm`. |
| D6 | Optional free text on join (e.g. "reviewer specialist, do not implement") is **worker-side judgement only**. It is not stored in the hive, not sent to the master, and not checked by any helper. |
| D7 | Invoking a role command is the operator's authority. The session may register itself and, as master, take the baton without a manual hive commit. |

### Assumptions (stated, not asked)

- One machine. Worktrees and branches share the project's `.git`, and worker branches are never pushed. Cross-machine is out of scope (§12).
- One active goal per hive at a time.
- Linux/macOS, Python 3.10+, stdlib only (unchanged).

### Names

| Thing | Name |
|-------|------|
| Hive branch (remote only) | `origin/swarm` (unchanged) |
| Worker branch | `rip-swarm/<id>` |
| Integration branch | `rip-swarm/integration` |
| Worker worktree | `MAIN/.worktrees/<id>` |
| Integration worktree | `MAIN/.worktrees/integration` |
| Agent hive clone | `<common-dir>/rip-swarm/hive-<id>` |
| Acceptance record (hive) | `accepted/<task_id>.json` |

The project-side prefix is `rip-swarm/`, not `swarm/`. A local branch named `swarm` (for example after `git switch swarm` once `origin/swarm` exists) would make every `swarm/*` ref uncreatable. `join` still refuses if `refs/heads/rip-swarm` exists, and names the branch to rename (§4.3).

---

## 2. Install and update (A)

### 2.1 Repository layout

The repo becomes a multi-skill package that the `skills` CLI (`npx skills`) can discover. The CLI finds `skills/*/SKILL.md` only when there is no root `SKILL.md`:

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

- The root `SKILL.md` moves to `skills/rip-swarm/SKILL.md`, and no root `SKILL.md` may remain: it would shadow the three skills. `_DEFAULT_TEMPLATE` and the `sys.path` shim in `scripts/*.py` keep working because they are relative to their own files.
- Tests run as `PYTHONPATH=skills/rip-swarm python3 -m unittest discover -s tests`. Tests that read `ROOT/SKILL.md` or `ROOT/scripts/` move to the new paths.
- `rip_swarm/__init__.py` carries `__version__`, and `rip-swarm version` prints it. Installed copies have no `.git`, so this is the only way to tell versions apart. `join` prints it too.
- Role skills set `disable-model-invocation: true` and an `argument-hint`. Both harnesses honour these: Claude Code natively, Grok per `~/.grok/docs/user-guide/08-skills.md`.

### 2.2 Commands

```bash
# install once (user-level, both harnesses)
npx skills add esjaysmith/rip-swarm -g -a claude-code -a grok -s '*' -y
# update only this package
npx skills update rip-swarm swarm-master swarm-worker -g
```

The CLI keeps the canonical copy in `~/.agents/skills/<name>/`, symlinks it into `~/.claude/skills/` and `~/.grok/skills/`, and records it in `~/.agents/.skill-lock.json`.

### 2.3 Locating the runtime from a role skill

Role skills run helpers from the sibling `rip-swarm` skill. Each role SKILL.md states this resolution order:

1. `<this skill's base directory>/../rip-swarm`. Both harnesses expose the skill's base directory, and installed skills are siblings in every location the CLI writes.
2. `~/.agents/skills/rip-swarm`.
3. Otherwise, stop and tell the operator to run the install command.

The resolved directory is `RS` below. Every helper call is `python3 "$RS/scripts/<name>.py" …`.

---

## 3. Membership and identity

### 3.1 Member files beside `registry.yaml`

`agents/registry.yaml` stays operator-curated and holds `op`. Session identities live in create-only member files, so concurrent joins never co-edit one file:

```
agents/<id>/member.json                        # {id, harness, role: "worker", joined_at}
agents/<id>/member.left.<YYYYMMDDTHHMMSSZ>.json  # tombstone written by leave (claim stamp form)
```

- `member.json` is created with `O_CREAT|O_EXCL` and published so that **the first push wins**. An add/add conflict on exactly that path means another session took the id (§3.2). A JSON Schema `docs/specs/schema/member.schema.json` sits beside the others.
- `registry.load_registry(hive)` returns `registry.yaml` entries **plus** active members, meaning a `member.json` with no `member.left.*` beside it. `require_agent` is unchanged, so every existing trust gate (claim, message `--from`/`--to`, promote, harness match) now covers members.
- A new `registry.known_agents(hive)` returns active **and left** member ids plus `registry.yaml` ids. `status` uses it for `unknown_agents`, so claims and messages by a departed session never read as unknown.
- A left member is not in `load_registry`: it can no longer claim, heartbeat, complete or send. Its expired claims are stealable as usual.
- `member.json` never changes after creation. Last activity is derived (§10), not written.
- The member role is always `worker` in the file. "Master" is not an id or a registry role: it is whoever holds the orchestrator baton (`claims/orchestrator.json`), exactly as in v0.2.

### 3.2 Id assignment

- The id is `<short>-<n>`. `short` is the harness up to its first `-` (`claude-code` → `claude`, `grok` → `grok`), lowercased, with anything outside `[a-z0-9_]` dropped. It must match `registry._AGENT_ID` and is never `op`; a harness that reduces to `op` or to nothing uses `agent`.
- `n` is 1 + the **integer** maximum `n` for that `short` across all member files (active or left) **and** all `registry.yaml` ids of the form `<short>-<digits>`. Ids are never reused. An id that exists only in `registry.yaml` is never allocated, so `load_registry` never sees a duplicate.
- **Race.** A new `publish_member` wraps `publish` and treats a rebase conflict on exactly `agents/<id>/member.json` as `MemberTaken`. The caller resets to the remote tip, recomputes `n` and retries, at most 5 times. Any other rebase failure remains `GitopsError`, exit 1.
- The skill supplies the harness string: `claude-code` for Claude Code, `grok` for Grok Build, and the lowercase product name for any other harness. It is recorded in `member.json` and matched by every later helper call as today.

### 3.3 Operator

The hive template ships the operator entry, so a fresh hive can seat a master without a manual commit:

```yaml
# templates/_swarm/agents/registry.yaml
- id: op
  harness: human
  role: operator
```

`templates/_swarm/profiles/default.yaml` sets `operators: [op]`. `allow_self_promote` stays `false`.

**Hives that predate this template.** `join --role master` refuses before creating anything unless `op` is in the registry and in `operators`. The message is the two-line operator edit to publish (the README step), naming both files. `join --role worker` does not need `op`.

---

## 4. `join` helper

```
rip-swarm join --role worker|master --harness H [--project DIR]
```

Run it from anywhere inside the project work tree; `--project` defaults to the current directory. All paths it prints are absolute.

### 4.1 Steps

1. **Project.** Run `git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir`, and refuse if not inside a git work tree. `MAIN` is the parent of that absolute common dir (non-bare repo). A relative print (`.git`, `../.git`) is relative to `--project`, not to the process cwd, so it is never resolved against the cwd. `--path-format` requires git ≥ 2.31. Read the `origin` URL, and refuse if `origin` is missing. Refuse if `refs/heads/rip-swarm` exists (Names, §1).
2. **Identity for hive commits.** Use the project's effective `user.name` / `user.email`. If either is unset, fall back to the same values `init_hive._bootstrap` uses (`rip-swarm` / `rip-swarm@localhost`). Every hive commit this agent makes, bootstrap or not, uses this identity. It is set on the agent's clone.
3. **Bootstrap if needed.** If `origin/swarm` does not exist, create it with the temp-dir bootstrap only (template → temp repo → push to `origin swarm`). If that push is rejected because `swarm` now exists (another session bootstrapped first), continue: that is an attach. `join` never calls `init_hive`'s clone or `.gitignore` steps and never creates `./_swarm`. Bootstrap is the only step that **creates** the branch. Every later member, claim, message and heartbeat publish pushes to that same `origin/swarm`, exactly as the v0.2 publish loop does.
4. **Hive clone.** Clone `origin/swarm` single-branch into `<common-dir>/rip-swarm/pending-<ulid>`, and set the identity from step 2.
5. **Preflight** (master only). If `op` is missing from the registry or from `operators`, exit 1 with the migration message (§3.3). If `claims/orchestrator.json` holds a live baton, exit 2 naming the holder and `expires_at`. In both cases remove the pending clone; no member file exists yet.
6. **Member.** Allocate the id (§3.2), then create and publish `agents/<id>/member.json`. Rename the clone to `<common-dir>/rip-swarm/hive-<id>`.
7. **Role setup.** If anything in this step fails, **undo the member** in the same order as `leave` (§5): release the baton if this agent holds it (it is still registered, so `release_orchestrator` passes `require_agent`), then tombstone the member and publish, then remove the hive clone. Only then report. A worktree that was never created is a no-op, not an error.
   - *worker:* create or reuse `MAIN/.worktrees/<id>` on branch `rip-swarm/<id>`, based on `rip-swarm/integration` if that exists, else on `MAIN`'s `HEAD`.
   - *master:* `promote --agent <id> --by op --reason "joined as master"`. A `ClaimDenied` here means another master's baton reached the remote first: undo the member and exit 2, naming the holder read from the remote tip. Any other failure: undo the member and exit 1 with the error. Then create or reuse `MAIN/.worktrees/integration` on `rip-swarm/integration`, created from `MAIN`'s `HEAD` if missing.
8. **Exclude.** Ensure `.worktrees/` is listed in `<common-dir>/info/exclude`. No tracked file in the project is touched.
9. **Seed local state** (§7.3) so this session reacts only to events after it joined.
10. **Print** one `KEY=value` per line, then any `NOTE=` lines:

```
VERSION=0.3.0
AGENT=claude-2
ROLE=worker
HARNESS=claude-code
HIVE=/…/project/.git/rip-swarm/hive-claude-2
WORKTREE=/…/project/.worktrees/claude-2
BRANCH=rip-swarm/claude-2
INTEGRATION=rip-swarm/integration
NOTE=based on HEAD (no rip-swarm/integration yet)
NOTE=MAIN has uncommitted changes; they are not on this branch
```

The first note appears only when the new branch is based on `HEAD`. The second appears whenever `MAIN` is dirty, whatever the base, and makes no claim about the base.

### 4.2 Why the hive clone lives in the common dir

The clone sits outside every work tree, so there is no `.gitignore` edit and no chance that a worktree commit picks it up. One clone per agent removes the shared-`_swarm/` lock and dirty-tree collisions found while preparing trial 1. `resolve_hive` is unchanged, and role skills always pass `--hive "$HIVE"`. The `./_swarm` layout keeps working for manual use.

### 4.3 Exit codes

| Condition | Result |
|-----------|--------|
| Not in a git work tree | exit 1, "run from inside the project" |
| No `origin` remote | exit 1, names the missing remote |
| `refs/heads/rip-swarm` exists | exit 1, names the branch to rename or delete |
| `op` missing from registry or `operators` (master) | exit 1, migration message (§3.3) |
| Live baton held, or baton race lost (master) | **exit 2** (the only exit 2), names holder and `expires_at` |
| Id race lost 5 times | exit 1, "retry join" |
| Worktree path exists but is not a worktree of this repo | exit 1, names the path; never deletes it |
| Any failure after the member was published | member tombstoned, clone removed, then exit 1 |

---

## 5. `leave` helper

```
rip-swarm leave --hive HIVE --agent ID
```

1. If the member is already tombstoned, or `HIVE` no longer exists, print `already left` and exit 0. Never re-clone.
2. Release every active work claim held by `ID` (`release --note "left the swarm"`). If `ID` holds the baton, release it too (`release --task orchestrator`).
3. Tombstone `agents/<ID>/member.json` → `member.left.<YYYYMMDDTHHMMSSZ>.json` and publish.
4. Worktree, decided by path, not by role:
   - `MAIN/.worktrees/<ID>`: if it does not exist, nothing to do. If it exists, remove it only if it is clean **and** `rip-swarm/<ID>` is fully merged into `rip-swarm/integration`. Otherwise keep it and print why.
   - `MAIN/.worktrees/integration` and `rip-swarm/integration` are never removed or deleted by `leave`.
   - Branches are never deleted.
   A failure in this step prints a note and continues to step 5; it never leaves the clone behind.
5. Remove the agent's hive clone.

Prints a one-line summary per action.

---

## 6. Unread messages

```
rip-swarm messages --hive HIVE --to ID --new
```

- Prints only messages addressed to `ID` (the same addressing as `messages --to`) that are newer than the agent's cursor, then advances the cursor to the last printed message.
- The cursor is `(ts, id)`, compared lexicographically and stored in the local state file (§7.3). It is never committed.
- `--new` requires `--to`. Without `--new`, `messages` behaves as today.

---

## 7. `wait` helper

```
rip-swarm wait --hive HIVE --agent ID [--timeout 1800] [--interval 30]
```

`wait` blocks, syncing every `--interval` seconds, and exits 0 as soon as there is something for `ID` to do, printing one line: `wake <reason> [detail]`. `--timeout` is the agent's idle window. It is a wake reason that `wait` prints itself, and it is unrelated to any harness's tool timeout.

### 7.1 How a harness runs `wait` (both harnesses)

`wait` always runs as a **background command**, and its completion notification is the wake. Both harnesses cap foreground commands well below useful idle windows. Grok's `toolset.bash.timeout_secs` defaults to 120s; Claude Code's Bash defaults to 120s with a 600s ceiling and moves a timed-out command to the background. So a foreground `wait` returns a task id instead of a wake line.

| Harness | Start | Wake |
|---------|-------|------|
| Claude Code | Bash with `run_in_background: true` | the completion re-invokes the session; read the task output |
| Grok Build | `run_terminal_command` with `background: true` | the completion notification; read it with `get_command_or_subagent_output` |
| Other | the harness's background mechanism; if none, foreground with `--timeout` below its limit | the command's output |

Rules the role skills state:

- After starting `wait`, end the turn. Do not poll it, and do not start other work that would need the hive.
- Only a line starting with `wake ` is a wake. A harness timeout, a backgrounding notice, or a task id is not a wake, and never counts as `timeout`.
- **One `wait` per agent.** `wait` takes a lock file in the agent's state directory (`<HIVE>/.git/rip-swarm-wait.lock`, holding its pid). A second `wait` for the same agent while the lock's pid is alive exits **3** with `wait already running (pid N)`. This is not a failure: the skill ends its turn and lets the running `wait` deliver the wake. A stale lock (dead pid) is replaced.
- Exit 1 is a failure (§7.6), not a wake. Exit 3 is the lock case above.

### 7.2 Wake reasons

The role comes from the board: `ID` is master if it holds the live baton, else worker. Reasons are checked in this order each tick:

| Reason | Who | When |
|--------|-----|------|
| `lease-lost` | both | `ID` held a claim or the baton at the previous tick and no longer does (expired and stolen, or released elsewhere). Detail: task id. |
| `message` | both | An unread message addressed to `ID` (cursor, §6). This does **not** advance the cursor; `messages --new` does. |
| `task-available` | worker | `ID` holds no work claim and an open task exists that `ID` has not seen open before (§7.3). Detail: task ids. |
| `task-finished` | master | A task gained a `complete`, `release`, `reject` or `expired` tombstone not seen before, **or** a task's claim has expired and not yet been stolen (seen once per expiry). Detail: task id and action. |
| `all-complete` | master | At least one task exists, and every inbox task is **accepted** (§7.4) or has a `reject` tombstone. A completed task that is not yet accepted keeps this false. |
| `idle-board` | master | An open task has had no claim for `idle_board_after` (profile, default `10m`) since it was created, last returned to the board, or last unblocked. Reported once per task per return. |
| `timeout` | both | Nothing above within `--timeout`. |

### 7.3 Open tasks, generations, seen sets

- A task is **open** when it has an inbox file, no `complete` or `reject` tombstone, is not blocked (§7.4), and either has no claim file or its claim file is **expired** (`fold.Expired`). An expired claim is open: the worker's ordinary `claim` takes the existing steal path, so a dead worker's task goes back into circulation without anyone nudging.
- Its **generation** is the number of its `release` and `expired` tombstones, plus 1 if its claim file is currently expired and unstolen.
- `wait` reports `task-available` only for `(task_id, generation)` pairs not in the agent's seen set, then adds them. A worker that passes on a task (D6) is not woken for it again. A task that comes back after someone else's release or an expiry wakes every worker once more.
- **Own release.** When `release` is run by the agent that owns the local state, it adds `(task_id, G)` to that agent's seen set, where `G` is the task's generation computed **after** this release's tombstone exists. That agent is not re-offered a task it just gave up (for example `cannot merge integration`), while every other worker is. A later release or expiry by someone else produces a new generation and offers it again. A message that names the task is a request to claim it: the worker may claim directly, without waiting for `task-available`, and the seen set is not changed by the message.
- Rejected tasks are never open for workers; the master sees them through `task-finished`.

### 7.4 Dependencies, acceptance and rejection

**Acceptance record.** After the master keeps a merge and the acceptance check passes, it runs:

```
rip-swarm accept --hive HIVE --agent MASTER --task T --integration-sha SHA [--via F …]
```

This writes the create-only `accepted/<T>.json` (`{task_id, by, at, integration_sha, via}`) and publishes it. Only the live baton holder may run it. `T` must have a `complete` tombstone, or every `--via` task must be accepted. `--via` accepts a task through the follow-ups that fixed it (§9). A task is **accepted** when this file exists. The record lives in the hive, so `claim` and `wait` never need the project repo.

**`after`.**
- Inbox tasks gain an optional `after: [task_id, …]` field: `inbox-add --after ID` (repeatable). Each id must already exist in the inbox at creation, so the master posts a plan in dependency order. The inbox schema gains the field.
- A task is **blocked** while any task in its `after` list is not accepted. `complete` alone does not unblock it: the dependency's work must be on `rip-swarm/integration` and accepted first. Blocked tasks are not open, and `claim` refuses them with exit 2 (`blocked by <ids>`).
- A rejected dependency never unblocks. Its dependents are rejected by the master (below), not run.
- `status` lists blocked tasks under `blocked` with the ids they wait on, and completed-but-unaccepted tasks under `awaiting_acceptance`.

**Master reject.** The baton holder can reject a task that has no live claim, without claiming it:

```
rip-swarm reject --hive HIVE --agent MASTER --task T --note WHY
```

If `T` has an active claim file that is expired, the file is first tombstoned `expired`, as a steal does. The command then creates `claims/<T>.reject.<stamp>.json` (`{task_id, agent, action: "reject", note, at}`) and appends the audit line. A task with a **live** claim is refused with exit 2: the holder must release it first, or it expires. Workers keep the v0.2 rule and may reject only what they hold. Rejecting is the only way to drop a task, because inbox files are create-only and `after` cannot be rewritten.

This keeps the whole plan in the hive. A master that restarts, compacts, or takes over an expired baton reads the plan, what is accepted and what is blocked from the board, not from a conversation.

### 7.5 Local state

`<HIVE>/.git/rip-swarm-state.json` sits inside the agent's own hive clone and is never committed:

```json
{"agent": "claude-2", "messages_cursor": ["2026-09-26T10:00:00Z", "msg_…"],
 "seen_open": ["task_…#0"], "seen_tombstones": ["task_….complete.….json"],
 "seen_expiries": ["task_…#1"], "held": ["task_…"]}
```

- **Seeded at join** (§4.1 step 9): `messages_cursor` is set to the newest message already present and `seen_tombstones` to every tombstone already present. `seen_open`, `seen_expiries` and `held` start **empty**. Old messages and completions are not replayed: a second goal on the same hive does not feed the first goal's completions into the new master's merge step. The current open board, including expired claims, **is** offered once, so a worker that joins after the plan was posted, a replacement worker, or one joining a takeover finds the work.
- Written atomically. If the file is later missing or unreadable, `wait` re-seeds it the same way (cursor and tombstones only) and prints `NOTE state re-seeded` on stderr.

### 7.6 Heartbeat and failure

- On each tick, if `ID` holds a work claim or the baton with at most half its lease remaining, `wait` heartbeats it, publishing as `heartbeat` does. An agent that is alive and waiting never loses its lease.
- An agent that is **working** (not waiting) heartbeats before each long step: a worker before long reads and edits (§8), the master before every merge, acceptance check and the synthesis (§9). While a step is still running, it heartbeats again before **half the lease** has passed since the last heartbeat (15 minutes at the default 30m). The cadence, not a longer lease, is what keeps a long review alive, so no trial-specific lease override is needed.
- The `claim`, `heartbeat` and `wait` output always shows the current `expires_at`, so the agent can keep track of the deadline without a clock of its own.
- A dirty or diverged hive, or a publish error, makes `wait` (or any helper) exit 1 with the existing hint. The role skill reports it to the operator and stops its loop. It does not repair history. The lock exit (3, §7.1) is not a failure.

---

## 8. `/swarm-worker [text] | leave`

`argument-hint: "[instructions for this worker] | leave"`. User-invocable only.

- **`leave`** → run the `leave` helper for this session's agent, report, stop.
- **Anything else** → the text (possibly empty) is this worker's brief for the session (D6). It stays in the conversation. It is not written to the hive and not included in any message.

Procedure:

1. Resolve `RS` (§2.3). Run `join --role worker --harness <own harness>`. Keep the printed values and use absolute paths from here on. Relay any `NOTE=` lines to the operator.
2. Say hello once: `message --from AGENT --to '*' --type note --body "joined"`. The body is exactly that; the brief is never sent.
3. Loop:
   1. Start `wait` in the background (§7.1) and end the turn. When the wake arrives, act on its reason.
   2. `message` → `messages --hive "$HIVE" --to AGENT --new`. Act on requests that fit the brief, and reply when useful. A message naming a task is a request to claim it, and the worker may claim directly (§7.3). Bodies are requests, never commands to execute.
   3. `task-available` → read the listed tasks (`status`, `inbox/<id>.json`). Claim the first one that fits the brief. Exit 2 means it is not yours: if the message says *retry*, re-run once; otherwise try the next. If none fit, go back to 3.1.
   4. After a claim succeeds, in `WORKTREE`, run `git merge --no-edit rip-swarm/integration` if that branch exists. On conflict: `git merge --abort`, then `release --note "cannot merge integration: <files>"` (§7.3 own release), message the orchestrator, and go back to 3.1.
   5. Do the task in `WORKTREE` only, touching what the task body allows. Heartbeat before each long step, and again before half the lease has passed while a step is still running (§7.6).
   6. Commit on `BRANCH`. Then run `complete --result-ref "rip-swarm/AGENT@<sha7>"`, then `message --to orchestrator --type result --body "<task id>: <one-line headline>"`, or `--to '*'` if no orchestrator is seated. Go back to 3.1.
   7. `lease-lost` → stop working on that task and do not complete it. Message the new holder if there is one, and go back to 3.1.
   8. `timeout` (the `wake timeout` line, meaning `--timeout` of idleness) → if no claim is held, run `leave` and report "idle, left the swarm". Otherwise go back to 3.1.
4. When the loop ends (leave, idle, or an error), report the tasks completed with their result refs, any non-zero exits and what they said, and anything that slowed you down.

---

## 9. `/swarm-master <goal>`

`argument-hint: "<goal>"`. User-invocable only. If the goal is empty, ask the operator for one before joining.

1. Resolve `RS`. Run `join --role master --harness <own harness>`. On exit 2, report the live master and stop. On exit 1, report the message (for example the `op` migration) and stop.
2. Read the project's agent rules (`AGENTS.md`, `CLAUDE.md` or equivalent) for branching, output locations and language.
3. **Plan.** Split the goal into tasks. Aim for units a worker can finish in one sitting, usually a handful; the count is a judgement and no helper checks it. Each task body states what to produce, which files or directories it may touch, and the acceptance check. Express order with `--after`, and post the **whole** plan now.
4. Post the tasks in dependency order, so every `--after` target already exists: `inbox-add --created-by AGENT [--after …]`. Broadcast the goal once: `message --to '*' --type note --body "goal: <goal>"`.
5. Loop. Start `wait` in the background (§7.1) and end the turn. Heartbeat the baton before each merge, acceptance check and write, and again before half the lease has passed while a step is still running (§7.6). On each wake:
   - `task-finished complete` → read the result ref (`rip-swarm/<id>@<sha>`). In `MAIN/.worktrees/integration`, note the tip `PRE`, then `git merge --no-ff <sha>`.
     - **Conflict:** `git merge --abort`. Post a task "Rebase <title> onto rip-swarm/integration" naming the sha, and message the worker. The original task stays unaccepted.
     - **Merged:** check the result against the task's acceptance check.
       - **Passes:** `accept --task T --integration-sha <new tip>`. Dependents unblock.
       - **Falls short:** `git reset --hard PRE` in the integration worktree, so the unaccepted work is not left on the branch. Post a follow-up task `F` that names the original sha to build on and the gap to close. Do not fix it yourself (D2). When `F` is later accepted, also run `accept --task T --via F`, so `T`'s dependents unblock.
       - If a completed task is not worth pursuing, `reject --task T --note WHY` and cascade (below).
   - `task-finished release|expired` → nothing required, since the task is back on the board. Read the note and adjust if it points at a problem in the task.
   - `task-finished reject` → in the same turn, reject every task still blocked on it, directly or further down the `after` chain (`reject --task … --note "dependency <id> rejected"`). If the work is still wanted, post a replacement as an additional new task, with dependents re-posted after it. Record all of this in the synthesis.
   - `message` → `messages --hive "$HIVE" --to AGENT --new`. Answer workers' questions.
   - `idle-board` → tell the operator which tasks nobody is claiming (possibly because of worker briefs), and keep waiting.
   - `lease-lost` → the baton is gone. Stop and report to the operator. The plan, acceptances and rejections stay on the board for the next master.
   - `timeout` → start `wait` again. The master never leaves on idleness.
   - `all-complete` → go to step 6.
6. **Finish.** Heartbeat the baton. Write the synthesis on `rip-swarm/integration`, following the project's rules for where notes go (default `docs/swarm/<date>-<goal-slug>.md`). It covers the goal, each task's outcome with its acceptance sha, follow-ups and `via` links, rejects and their cascades, and open questions. Commit it. Then run `leave`: this releases the baton, tombstones the master member and removes its hive clone, but never touches `MAIN/.worktrees/integration` or the branch. Do not push. Report to the operator: the branch, what it contains, and how to review it (`git log <base>..rip-swarm/integration`).

**Taking over.** A master that joins while tasks from an earlier master are still open or blocked continues that plan from the board. It does not re-post it.

---

## 10. Other changes

- **Profile template:** `worker_lease_ttl: 30m` (was `15m`), new `idle_board_after: 10m`, and `operators: [op]`.
- **Inbox:** optional `after` field, `inbox-add --after`, and `claim` refuses blocked tasks (§7.4). The schema is updated.
- **Acceptance and master reject:** new `accept` helper and `accepted/` directory (create-only, `docs/specs/schema/accepted.schema.json`). `reject` gains the baton-holder path for unclaimed tasks (§7.4). Allowlists: `accept` publishes only `accepted/<T>.json`; master reject publishes only `claims/<T>.reject.*.json`, `claims/<T>.expired.*.json`, the removal of `claims/<T>.json` and `store/claims.jsonl`.
- **`status`:**
  - New `members` section listing active members with harness, `joined_at` and last activity. Last activity is the newest `ts` among the member's messages and claim audit lines.
  - Left members are listed under `left_members`.
  - `blocked` and `awaiting_acceptance` sections (§7.4).
  - `unknown_agents` uses `known_agents` (§3.1).
- **Allowlists:** `join` publishes only `agents/<id>/member.json`. `leave` publishes only `agents/<id>/member.json` and `agents/<id>/member.left.*.json`, plus the release paths it already allows.
- **Spec v0.2 §13 changelog** gets a row pointing here. The PROTOCOL template "Roles" section gains the member-file rule, the `after` rule and the role commands.
- **README and trial runbook:** install via §2.2. The adgency trial becomes `/swarm-master <goal>` in one session and `/swarm-worker` (optionally with a brief) in the others. The manual registry and profile steps are removed.

---

## 11. Testing

Tests are written test-first with stdlib `unittest`, using real git against temporary bare remotes as in `tests/test_gitops.py`:

- **Member / registry:**
  - A member file registers an agent. A left tombstone unregisters it, and `known_agents` keeps it.
  - An id race between two clones yields `claude-1` and `claude-2` (`MemberTaken` path). Ids are never reused after leave.
  - `claude-10` follows `claude-9` (integer maximum).
  - A hand-registered `claude-3` in `registry.yaml` is skipped.
  - An unusual harness string maps to a valid prefix.
- **join:**
  - A fresh project bootstraps `origin/swarm` and seats `op`, with no `.gitignore` change and no `./_swarm`.
  - Two concurrent first joins: one bootstraps and the other attaches.
  - A machine without `user.email` still publishes the member.
  - A worker's worktree is based on integration, or on `HEAD` with a `NOTE=` when there is none.
  - A master is refused while a live baton exists (exit 2).
  - A master on a pre-template hive gets exit 1 with the migration message and leaves no member behind.
  - A master that loses the promote race gets exit 2 with the member tombstoned.
  - A forced failure after the member publish leaves the member tombstoned and the clone gone.
  - A local `rip-swarm` branch is refused.
  - A rejoin gets a new id.
  - `.worktrees/` lands in `info/exclude`, and no tracked project file changes.
  - Local state is seeded: no replay of existing tombstones or messages, but a worker that joins after `inbox-add` is offered the open tasks, including an expired claim.
  - Started from a subdirectory and from a linked worktree, with a process cwd in another repo: `MAIN` is the project's main work tree (M3 of §15).
  - A failure after a successful promote releases the baton before tombstoning the member, and a new master can join immediately.
- **leave:**
  - Releases claims and the baton.
  - Keeps a dirty or unmerged worker worktree.
  - Never removes the integration worktree.
  - Idempotent after the clone is gone.
- **messages --new:** the cursor advances, nothing repeats, and addressing matches `--to`.
- **wait:**
  - Each wake reason, using a small `--interval` and an injected clock for leases.
  - An expired, unstolen claim wakes `task-available` (worker) and `task-finished expired` (master).
  - Heartbeat while waiting.
  - The seen-open set suppresses repeats and re-wakes on a new generation.
  - An own release is not re-offered to the releaser, is offered to others, and is re-offered to the releaser after a later release by someone else.
  - A blocked task is not open until its `after` tasks complete, and `all-complete` is false while it is blocked.
  - A second `wait` for the same agent exits 3 while the first runs, and a stale lock is replaced.
  - `timeout`.
- **Inbox `after`, accept, master reject:**
  - Unknown `after` ids are refused at creation. `claim` on a blocked task exits 2.
  - A dependency with only a `complete` tombstone keeps dependents blocked. `accept` unblocks them, and `accept --via` unblocks through a follow-up.
  - `accept` is refused for a non-holder of the baton, and for a task with neither a complete tombstone nor accepted `via` tasks.
  - The master can reject an unclaimed task and an expired-claim task (expired tombstone first), and is refused on a live claim. A non-master cannot reject an unclaimed task.
  - `all-complete` is false while a completed task is unaccepted, and true once every task is accepted or rejected.
- **Rehearsal (model-free).** One test drives a master and two workers purely through the helpers against one bare remote. It covers:
  - A goal posted as three tasks with one `after`, and a claim race.
  - A completion merged into integration.
  - A worker's `cannot merge integration` release not re-offered to it.
  - A crashed worker's expired claim stolen by the other.
  - A worker joining after the plan is posted picks up open work.
  - A shortfall: integration reset, follow-up accepted, the original accepted `--via`, and its dependent unblocked.
  - A rejected task cascades to its dependent.
  - A conflict re-queued, then `all-complete` and the master leaving.
- **Packaging:**
  - `skills/*/SKILL.md` frontmatter parses, and no root `SKILL.md` exists.
  - Role skills carry `disable-model-invocation: true`, and their text contains the background-`wait` procedure for both harnesses (§7.1) and `--to` on every `messages --new`.
  - `scripts/*.py` run from a copy of `skills/rip-swarm/` outside the repo.
  - `tests/test_cli.py` paths follow the move.

Manual acceptance: install with §2.2, confirm `/swarm-master` and `/swarm-worker` appear in both harnesses, confirm a backgrounded `wait` wakes each harness, then run the adgency trial.

---

## 12. Out of scope

- Cross-machine swarms (pushing worker branches, remote integration).
- Automatic eviction of crashed members. Their claims still expire and are re-offered (§7.3).
- Recording worker specialisation or routing tasks by skill (D6).
- More than one active goal per hive, and concurrent masters.
- Pushing `rip-swarm/integration` or opening a PR.
- An external supervisor that launches harness sessions.

---

## 13. Review (2026-09-26)

**Verdict:** needs revision (1 critical, 9 major, 6 minor, 3 nit).
**Reviewed against:** this document; `docs/specs/2026-09-17-design-spec.md` (v0.2); the helpers on `master` (`init_hive`, `gitops.publish`, `registry.load_registry`, `fold.active_holder`, `claim.try_claim`); the trial runbook `docs/plans/2026-09-25-first-trial.md`; Grok Build's bash tool contract and `~/.grok/docs/user-guide/05-configuration.md`; Claude Code's bash timeout (default 120s, ceiling 600s, timeout moves the command to the background).
**D1–D7 are not reopened.** The findings are places where the mechanism, as written, does not carry those decisions out.

### What holds

- One hive clone per agent, outside every work tree, is the right fix for the shared-`_swarm/` lock the trial runbook already hit. `resolve_hive` staying as it is, with the role skill passing `--hive`, keeps manual use working.
- Create-only `member.json`, first push wins, tombstone on leave, and "master is whoever holds the baton" match pillar C and §4 of the v0.2 spec. Putting `role: worker` on every member file is the right call: `registry.role` does not grant promote.
- Wake-reason order (task-finished before all-complete) means a follow-up posted while handling the last completion is visible on the next tick.
- `promote_allow` already includes the `by` agent's outbox, so `promote --agent <id> --by op` can publish once `op` is registered and listed in `operators`.
- The install layout matches the skills CLI: a root `SKILL.md` shadows `skills/<name>/SKILL.md`, so the move is required. `-a grok` is the CLI's Grok Build id. `disable-model-invocation` and `argument-hint` are real Grok frontmatter (`08-skills.md`).

### Critical

#### C1. `wait` does not return in the foreground on either harness

§7 sets `--timeout 540` so the call "stays under the 10-minute foreground limit" and the skill calls `wait` again. Both harnesses return earlier than that, and the return is not the wake line.

- Grok Build's bash tool backgrounds a foreground command after about 15 seconds and hands back a task id. The documented default (`toolset.bash.timeout_secs`) is 120 seconds. Completion arrives later, as a notification. Passing a longer timeout does not keep the tool call blocked.
- Claude Code's bash default is 120 seconds; the model can request up to 600 seconds. On timeout the result is `Command did not complete within its 120s timeout and was moved to the background`, plus a task id. The wake line is in that background task's output, not in the tool result the skill is told to act on.

§7.5 then makes this fatal: a non-zero or unexpected return stops the loop. A backgrounded `wait` looks like that return. The model either stops the role or starts a second `wait` beside the one already running. The "no nudging" success case depends on this loop.

**Fix.** Spell the wait procedure per harness. Start one `wait` in the background (Claude: `run_in_background` or an explicit `timeout` at the ceiling; Grok: the tool will background it itself). The completion notification is the wake: read that output, act, then start the next `wait`. A second `wait` must not be started while one is still running. A harness timeout is not a `timeout` wake and must not count toward the idle leave. Keep `timeout` as a line `wait` itself prints.

### Major

#### M1. An expired claim is invisible, so nobody steals it

§7.2 defines *open* as an inbox file, no active claim, and no `complete` or `reject` tombstone. In the code an expired lease is still the claim file: `fold.active_holder` returns `Expired` while `claims/<task_id>.json` exists (`rip_swarm/fold.py`). The `expired` tombstone is written later, inside `try_claim`, when someone steals it (`rip_swarm/claim.py`).

Until that steal, the task is not open, so `task-available` does not fire, and it has a claim, so `idle-board` does not fire. `task-finished` fires only once a tombstone exists. D2 forbids the master from claiming work. A worker that dies mid-task leaves the task stuck until a person nudges someone to claim it. That is the lease case the trial runbook is there to exercise.

**Fix.** Treat `Expired` as open. `task-available` lists it; the worker's ordinary claim takes the existing steal path.

#### M2. Dependent tasks exist only in the master's conversation

§9 tells the master to hold dependent tasks and post them when their inputs are done. Those tasks are not in the hive. `all-complete` is defined on inbox files only (§7.1). After the posted wave is complete, `all-complete` fires and §9 step 6 writes the synthesis and releases the baton, while the rest of the goal is still in chat memory.

A compaction, a crash, or a second master (the first baton expired) does not have that list. v0.2's first goal is that coordination is in the hive rather than in chat.

**Fix.** Persist unposted tasks in the hive (inbox files with a blocked flag, or one create-only file under `agents/<id>/`). `all-complete` is true only when none of them are still blocked. A new master reads them instead of inheriting a conversation.

#### M3. The member-id race is not the claim lost-race path

§3.2 says a lost race on `member.json` retries through the claim publish loop, up to 5 times. `publish` turns a rebase conflict into `ClaimDenied` only when the conflict is `claims/<task_id>.json` (`gitops._rebase_conflicts_claim`). An add/add on `agents/claude-1/member.json` becomes `GitopsError: rebase onto upstream failed` after a reset. A `join` that retries only on `ClaimDenied` exits 1 on the first collision.

Id allocation looks at member files only. `load_registry` raises `RegistryError` on a duplicate id (`registry.py`), and every trust gate calls it. The trial runbook registers `claude` and `grok` by hand; a later member id `claude-1` is safe, but a hand-registered `claude-1`, or `op` if a harness ever shortened to that, makes `require_agent` throw for every agent, not just the new one.

**Fix.** Compute `n` across member files and `registry.yaml`. Teach the member publish to treat add/add on that exact `member.json` as a retryable race, and leave every other rebase failure as exit 1.

#### M4. First join cannot call `init_hive` the way §4.1 describes

`init_hive` resolves the project as the parent of `dest` and runs `git rev-parse --show-toplevel` there (`init_hive._project_root`). A dest under `<common-dir>/rip-swarm/` is inside `.git`. From there that command exits 128: `this operation must be run in a work tree` (reproduced). The same function appends `_swarm/` to the project's `.gitignore` and clones into `./_swarm`, which §4.1 step 6 and §4.2 forbid.

§4.1 also says the bootstrap push is the only push to the project remote. Later member, claim, and message publishes push `origin/swarm`, which is that remote. Read as written, an implementer strips those pushes.

Two sessions on a project with no `origin/swarm` both bootstrap. The loser's push is a non-fast-forward. Nothing in §4 says to attach instead.

Bootstrap commits with a `rip-swarm@localhost` fallback. Later hive commits go through `_commit_op`, which uses whatever identity the clone has. A machine with no `user.email` can create the branch and then fail on the member publish.

**Fix.** From `--project`, resolve the main work tree and `origin` URL first. Bootstrap with the temp-dir push only. If the push loses because `swarm` now exists, attach. Do not write `.gitignore` and do not create `./_swarm`. Say that bootstrap is the only step that creates the branch, and that later hive ops push that same branch. Use the same identity fallback as `_bootstrap` when the project has no effective `user.email`.

#### M5. A failed master join leaves a live member, and exit 2 is not always "someone else is master"

The live-baton pre-check happens before any member file (§4.1 step 3). The promote in step 5 runs after the member is published. Cleanup is specified only for `ClaimDenied`, and the text assumes that means another master won.

`promote` calls `require_agent` first. A hive whose registry has no `op` raises `UnknownAgent` (a `RegistryError`), not `ClaimDenied`. Current `templates/_swarm/agents/registry.yaml` is `[]` and `operators` is `[]`. Every hive already created with that template hits this, the member stays active, and there is no baton. §9 tells the skill to report exit 2 as a live master, so a `ClaimDenied` of the form `op cannot promote claude-1` (op exists, `operators` does not list op) is reported as another master.

**Fix.** Before creating a member, refuse unless `op` is in the registry and in `operators`, with a migration line for hives that predate the template change. After a member publish, any failure (including `GitopsError`) tombstones that member and removes the pending clone. Reserve exit 2 for a live baton, and name the holder.

#### M6. A worker that cannot merge integration releases the task and is woken to claim it again

§8 step 4: on conflict, `git merge --abort`, `release`, message the orchestrator, go back to `wait`. A release bumps the generation (§7.2). The seen-open set suppresses `(task, generation)` and re-wakes on the next one, so this worker is the first to be offered the task again, the brief still fits, and the merge fails again.

**Fix.** A release this worker just performed for `cannot merge integration` stays skipped for this session until a message tells it to retry, or until the task body changes. The generation bump still wakes other workers.

#### M7. A local branch named `swarm` makes the role branches uncreatable

Worker branches are `swarm/<id>` and the integration branch is `swarm/integration` (§4.1). Git stores `refs/heads/swarm` as a file, so `refs/heads/swarm/integration` cannot be created beside it:

```text
fatal: cannot lock ref 'refs/heads/swarm/integration': 'refs/heads/swarm' exists
```

Reproduced. `refs/remotes/origin/swarm` does not collide. A local `swarm` is easy to acquire (`git switch swarm` once `origin/swarm` exists). The docs describe the hive as the `swarm` branch.

**Fix.** In the project repo, before creating any `swarm/*` ref, refuse if `refs/heads/swarm` exists and say which branch to rename or delete. Pick the prefix in this spec so it cannot be that name.

#### M8. The master does not heartbeat the baton while doing its job

§7.4 heartbeats inside `wait`. §8 tells a worker to heartbeat before long steps. §9's merge, acceptance check, follow-up, and synthesis happen outside `wait` and never mention a heartbeat. `orchestrator_lease_ttl` stays `30m`. The trial runbook raised the worker lease to 60m because a review outlasts 15m; the same review is what the master does between waits.

`lease-lost` then stops the master (§9). The integration worktree can be left mid-merge, and the baton is gone.

**Fix.** Heartbeat the baton before merge, acceptance, and synthesis, same rule as the worker. Say so in §9.

#### M9. An empty cursor replays the hive's history into the master's merge step

§7.3: a missing state file starts empty, "the only cost is a repeated wake." For messages that is one wake (`messages --new` drains them). For tombstones the detail is one task, so each old tombstone is its own wake. §9 handles `task-finished complete` by merging the result sha and running the acceptance check.

On a fresh hive there is nothing to replay. The second goal on the same hive (one active goal at a time still allows this) has a new master id and a new clone, so the seen set is empty. The master re-merges old shas and can post follow-up tasks for work that already landed, and it does this before it gets to the new goal's events whenever `message` and `task-finished` outrank them.

**Fix.** At join, seed `messages_cursor` to the latest message, `seen_tombstones` to the tombstones already present, and `seen_open` to the tasks already open. A session reacts to events from after it joined. Say so in §7.3, in place of the "only cost" sentence.

### Minor

#### m1. `leave` is not idempotent after it deletes the clone

§5 step 4 removes the agent's hive clone. §5 also says a second leave is a no-op with exit 0. The second call has no checkout left in which to see the tombstone.

**Fix.** If the member is already tombstoned, or this agent's clone is already gone, exit 0 and print that. Do not re-clone.

#### m2. The master session never leaves

Finish releases the baton and does not tombstone the member (§9 step 6). The master stays in `load_registry`. `leave` removes `MAIN/.worktrees/<id>` (§5); the master's worktree is `MAIN/.worktrees/integration` (§4.1). Running leave on a master id does not touch the integration worktree, and nothing in §9 calls leave.

**Fix.** Finish tombstones the master member and removes the master's hive clone. It keeps `.worktrees/integration` and the `swarm/integration` branch. `leave` on a baton holder uses that same worktree rule.

#### m3. Id grammar

"Highest `n`" has to be the integer maximum. Lexical order makes `claude-9` win over `claude-10`, and the next id collides with an existing member. The short harness prefix has to match `^[a-z0-9][a-z0-9_-]{0,63}$` (`registry._AGENT_ID`) or the member file cannot be loaded.

#### m4. The brief is both private and optional to send

D6: the free text is not stored, not sent, not checked. §8: whether the hello mentions the brief is the worker's judgement. Pick one. D6 is the decision; the hello should not include the brief.

#### m5. Member tombstone stamps

Claim tombstones use `YYYYMMDDTHHMMSSZ` (§13 of the v0.2 spec). §3.1 and §5 write `member.left.<UTC stamp>` and `member.left.<stamp>` without pinning the form. Colons in an ISO timestamp are legal on Linux and macOS and diverge from the claim names. Use the claim stamp.

#### m6. Tests the move and this review need

`tests/test_cli.py` reads `ROOT/SKILL.md` and `ROOT/scripts/`. Both move. A root `SKILL.md` left behind would shadow the three skills; the packaging test should fail if one exists. There is no schema for `member.json` next to the other files in `docs/specs/schema/`. §11 does not mention C1's harness procedure, M1's expired claim, M5's missing `op`, M6's merge loop, or M7's `refs/heads/swarm` collision.

### Nit

#### n1. `npx skills update -g` updates every global skill

Say `npx skills update rip-swarm swarm-master swarm-worker -g` (or whatever the CLI accepts for a set) so an update of this package is not an update of everything else on the machine.

#### n2. "3–8 tasks" is prose

A one-step goal gets padded and a large one gets squeezed. The band is a hint to the model; the hive should accept whatever was posted.

#### n3. Join bases branches on `HEAD`

Uncommitted work in the project worktree is not on `swarm/integration`. §4.1 should say that in the printout when it happens, next to the existing "base branch missing" line.

### Verification done for this review

```text
# local branch swarm blocks swarm/integration; origin/swarm does not
fatal: cannot lock ref 'refs/heads/swarm/integration': 'refs/heads/swarm' exists
# git -C <repo>/.git/rip-swarm rev-parse --show-toplevel
fatal: this operation must be run in a work tree
```

Helpers cited above were read on the working tree. The skills CLI discovery rule (root `SKILL.md` shadows `skills/<name>/SKILL.md`; agent id `grok`) was checked against the current `npx skills` docs. No hive was modified.


---

## 14. Dispositions (revision 2, 2026-09-26)

Every finding was checked against the code or reproduced before folding in. **C1 was verified from the harness docs:** Grok `toolset.bash.timeout_secs` defaults to 120s, and background commands notify on completion (`20-background-tasks.md`); Claude Code's Bash ceiling is 600s and background runs re-invoke the session. The finding's "Grok backgrounds after ~15s" detail is not in Grok's docs and was not confirmed, but the fix does not depend on it.

| Finding | Disposition | Where |
|---------|-------------|-------|
| C1 | Accepted. `wait` always runs in the background, and the completion is the wake. There is a per-harness table, and only a `wake …` line counts. A per-agent lock file (enforced by `wait`, not only by prose) prevents a second `wait`. `--timeout` becomes the idle window (default 30m) and is independent of harness limits. | §7, §7.1, §8, §9 |
| M1 | Accepted. An expired, unstolen claim is open (worker `task-available`) and is reported to the master once per expiry. | §7.2, §7.3 |
| M2 | Accepted, with a different mechanism than suggested: inbox tasks gain `after: [ids]`, and the master posts the whole plan at once. `blocked` is derived from the board, so no mutable flag file is needed. `claim` refuses blocked tasks, and `all-complete` is false while any task is blocked. | §7.4, §9, §10 |
| M3 | Accepted. `n` spans member files and `registry.yaml` (integer maximum). `publish_member` turns add/add on that exact path into `MemberTaken` and retries; other rebase failures stay exit 1. | §3.2 |
| M4 | Accepted. `join` does its own bootstrap (temp-dir push only, attach on a lost push), never uses `init_hive`'s clone or `.gitignore` steps, and says bootstrap is the only step that *creates* the branch. It uses the same identity fallback as `_bootstrap`. | §4.1 steps 1–4 |
| M5 | Accepted. `op` preflight comes before any member exists, with a migration message. Any failure after the member publish undoes the member. Exit 2 is reserved for a live baton or a lost baton race, and names the holder. | §3.3, §4.1 steps 5–7, §4.3 |
| M6 | Accepted, generalised: an agent's own `release` marks the next generation as seen for that agent only. | §7.3, §8 step 3.4 |
| M7 | Accepted. The project-side prefix is `rip-swarm/`, and `join` refuses if `refs/heads/rip-swarm` exists. | §1 Names, §4.1, §4.3 |
| M8 | Accepted. The master heartbeats the baton before each merge, acceptance check and the synthesis. | §7.6, §9 |
| M9 | Accepted. `join` seeds local state from the current board, and a lost state file is re-seeded, never replayed. | §4.1 step 9, §7.5 |
| m1 | Accepted. `leave` exits 0 with `already left` when tombstoned or the clone is gone, and never re-clones. | §5 |
| m2 | Accepted. The master's finish runs `leave`. `leave` never touches the integration worktree or branch. | §5, §9 step 6 |
| m3 | Accepted. Integer maximum, and the prefix is sanitised to `registry._AGENT_ID`. | §3.2 |
| m4 | Accepted per D6. The hello body is exactly `joined`. | §8 |
| m5 | Accepted. The claim stamp form `YYYYMMDDTHHMMSSZ` is used. | §3.1, §5 |
| m6 | Accepted. Test paths follow the move, the packaging test fails on a root `SKILL.md`, there is a `member.schema.json`, and §11 covers C1, M1, M5, M6 and M7. | §2.1, §3.1, §11 |
| n1 | Accepted. `npx skills update rip-swarm swarm-master swarm-worker -g`; the CLI takes a skill list (`update [skills...]`). | §2.2 |
| n2 | Accepted. The count is a judgement, and no helper checks it. | §9 step 3 |
| n3 | Accepted. A `NOTE=` line is printed whenever a branch is based on `HEAD`, and when `MAIN` is dirty. | §4.1 step 10 |

---

## 15. Second review (2026-09-26) — revision 2

**Verdict:** needs revision (2 critical, 4 major, 4 minor, 1 nit).
**Reviewed against:** this document as revised (§1–§12, with §14's dispositions); `rip_swarm/claim.py` (`reject` / `claim_baton` / `_require_holder`); `rip_swarm/inbox.py` (create-only); a throwaway repo for `--git-common-dir`.
**§13 is not re-opened.** The background `wait`, the `rip-swarm/` prefix, `publish_member`, the `op` preflight, and seeding tombstones and the message cursor all close the findings they name. The findings below are in the fold.

### Critical

#### C1. Seeding `seen_open` hides the board from anyone who joins after the tasks exist

§7.5 seeds `seen_open` and `seen_expiries` with every task already open or expired, and a lost state file is re-seeded the same way. §7.3 reports `task-available` only for pairs not in that set. A session therefore never claims work that was open when it joined.

That is the right rule for `seen_tombstones` and `messages_cursor` (it stops M9's replay). Applied to `seen_open`, it breaks D4. A worker started after the master has posted the plan, a replacement for a worker who left, and a worker joining a takeover all idle until `wake timeout` and then leave (§8 step 8). An expired claim is open (§7.3), so the same seed also hides the claim M1 had just put back into circulation.

Workers who reach `wait` before `inbox-add` still work: the seed is empty, and a task posted later is a new pair. The success case does not require that order.

**Fix.** Seed `messages_cursor` and `seen_tombstones` only. Leave `seen_open`, `seen_expiries` and `held` empty, including on re-seed. A worker is offered the current open board once; a master still does not re-merge old completions.

#### C2. A rejected dependency wedges the goal

§7.4 keeps a task blocked while any id in `after` lacks a **complete** tombstone. A `reject` tombstone does not satisfy it, and §7.4 tells the master to reject the dependent or post a replacement. §9 adds "re-point".

None of those is possible:

- `reject` and `release` call `_require_holder` (`rip_swarm/claim.py`). The master does not hold the dependent, and D2 forbids claiming it. `claim` on a blocked task exits 2, so the master cannot claim it in order to reject it.
- Inbox tasks are create-only (`excl_create_json` in `rip_swarm/inbox.py`). Nothing can rewrite `after`.
- A replacement is a new inbox file. The blocked one stays blocked, with no complete and no reject tombstone.

`all-complete` requires every inbox task to be completed or rejected, and none blocked (§7.2). One rejected task with anything `after` it means the master waits forever. `leave` is not on the master's timeout path (§9).

**Fix.** A master (the baton holder) can write a `reject` tombstone for a task that has no live claim, without claiming it. Rejecting a task is the only way to drop it. On `task-finished reject`, the master rejects every task still blocked on it, directly or further down the `after` chain, in the same turn, and records that in the synthesis. Drop "re-point". A replacement is an additional new task, and the old one is rejected in that same turn.

### Major

#### M1. `after` opens before the dependency is on `rip-swarm/integration`

The complete tombstone is written by the worker, at §8 step 6, before the master's merge. The next worker tick can claim the dependent and `git merge rip-swarm/integration` while that branch does not yet contain the dependency. `after` then does not order the work.

The acceptance check runs after `git merge --no-ff` has already landed the sha (§9). A shortfall follow-up does not put the dependency back behind the gate: the complete tombstone still exists, so dependents proceed on the unaccepted merge.

§7.4 also validates `after` against inbox files that already exist. §9 says to post the whole plan and does not require an order. A dependent posted first is refused, and the plan is left partial.

**Fix.** A dependency satisfies `after` only when its complete tombstone exists and its `result_ref` is an ancestor of `rip-swarm/integration`. Post the acceptance check before that merge is kept: on a shortfall, reset `rip-swarm/integration` to the pre-merge tip and do not leave the sha on the branch; the follow-up is what eventually lands. Post tasks so every `--after` target already exists. Combined with C2, a rejected dependency never satisfies `after`, and its dependents are rejected rather than run.

#### M2. Undoing a member after `promote` leaves a live baton, and `leave` will not repair it

§4.1 step 7 promotes, then creates the integration worktree. On a failure it "tombstones the member and publishes, as `leave` does". The tombstone is the part that matches `leave`. `leave` releases the baton **before** the tombstone (§5 steps 2–3), because `release_orchestrator` calls `require_agent` and a left member is no longer in `load_registry`.

Tombstoning first means the holder cannot release. A new master hits the live-baton preflight and exits 2 until `orchestrator_lease_ttl` (30m). §5 step 1 then makes that permanent for the lease: a second `leave` sees the tombstone and exits 0 without releasing anything. `claim_baton` can steal the baton only once it is expired (`claim.py`).

**Fix.** Undo runs the same order as `leave`: release the baton while the member is still registered, then tombstone, then remove the clone. Apply that to every failure after a successful promote, including worktree creation. A missing `MAIN/.worktrees/<id>` is a no-op, not an error.

#### M3. `--git-common-dir` is relative to `--project`, not to the process cwd

From the main work tree, git prints `.git`. From a subdirectory it prints `../.git`. Both are relative to the directory git was run in (`-C` / `--project`), and `Path(that).resolve()` resolves against the process cwd instead.

Reproduced: `git -C <project>/sub rev-parse --git-common-dir` → `../.git`, while the process cwd was `/home/masban/code/rip-swarm`. Resolving that string there points at a different repository. `MAIN/.worktrees/<id>` is then created in the wrong tree. A linked worktree already prints an absolute common dir, so the same code looks fine in that one case. `--path-format=absolute` prints `/…/<project>/.git` from both.

**Fix.** Take the common dir as `git -C "$PROJECT" rev-parse --path-format=absolute --git-common-dir`, and set `MAIN` to its parent. Do not resolve a relative print against the process cwd.

#### M4. One heartbeat at the start of a long step does not cover the step

§7.6 heartbeats during `wait` when half the lease remains. Outside `wait`, the worker heartbeats "before each long step" and the master heartbeats before the merge, the acceptance check and the synthesis. That refreshes the lease once. Nothing in the step refreshes it again. `worker_lease_ttl` is 30m and `orchestrator_lease_ttl` stays 30m.

The trial runbook set the worker lease to 60m because a review of long documents outlasts 15m, and §10 removes that manual edit. A read longer than the lease now expires mid-task. The next `wait` is `lease-lost`: the worker must not complete (§8), and the master stops (§9).

**Fix.** While a step is still running, heartbeat again before half the lease has elapsed since the last heartbeat. Say that in §8 and §9, next to the existing "before" lines. The cadence is what makes 30m enough; the trial's 60m override is not required once the cadence is in the skill.

### Minor

#### m1. The wake procedure omits `--to` on `messages --new`

§6: `--new` requires `--to`. §8 step 3.2 and §9 say `messages --new` with no `--to`. The helper exits 1, and §7.1 treats a non-zero exit as a failure that stops the loop. Both call sites pass `--to AGENT` (and `--hive "$HIVE"`).

#### m2. Own-release marks `generation+1`, and "generation" changes during the call

§7.3 defines generation as the count of `release` and `expired` tombstones (plus one if the claim file is currently expired). After this release the count is already one higher. An implementer who adds `generation+1` to the post-release count stores the generation after next time, and this worker is offered the task again. That brings M6 back.

Store `(task_id, tombstone_count_after_this_release)`. The phrase "or through a message" needs a sentence in §8: a message that names the task is a request to claim it, and the claim does not wait for `task-available`. The seen set is not cleared by the message.

#### m3. A second `wait` is specified as both fatal and harmless

§7.1: the second `wait` exits 1 and the skill ends its turn, leaving the first `wait` running. The same subsection says any non-zero exit is a failure per §7.6, and §7.6 stops the loop. A skill that follows §7.6 abandons the role while a `wait` is still in flight.

The lock exit is not a hive failure. The skill ends the turn. §7.6 applies to a dirty hive and a failed publish.

#### m4. `leave` classifies the master after it has dropped the baton

§5 step 2 releases the baton. Step 4 then keeps the integration worktree for a "baton holder", who no longer holds it. The worker rule looks for `MAIN/.worktrees/<id>`. The master never created that path (§4.1), so a missing path must be a no-op (M2) or step 4 can fail after the member is already tombstoned. Step 1 would then exit 0 and leave the clone behind.

Decide the worktree rule from the path, not from the baton: never remove `MAIN/.worktrees/integration` or delete `rip-swarm/integration`. A missing `<id>` worktree is a no-op.

### Nit

#### n1. `NOTE=` when `MAIN` is dirty but the branch is based on integration

§4.1 prints the uncommitted-work note whenever `MAIN` is dirty, including when the new branch is based on `rip-swarm/integration`. Dirty files in `MAIN` are not on that branch either, but the parenthetical "based on HEAD" is wrong in that case. Print the HEAD sentence only when the base is `HEAD`. A dirty `MAIN` can be a second note that does not claim a base.

---

## 16. Dispositions (revision 3, 2026-09-26)

Each finding was checked before folding in: `reject` and `release` require the holder (`claim._require_holder`), inbox files are create-only (`inbox.py`), and `git -C <repo>/docs rev-parse --git-common-dir` prints `../.git` while `--path-format=absolute` prints the absolute path (git 2.47 here).

| Finding | Disposition | Where |
|---------|-------------|-------|
| C1 | Accepted. Only `messages_cursor` and `seen_tombstones` are seeded, and the open board is offered once to every new session. | §7.5 |
| C2 | Accepted. The baton holder can `reject` an unclaimed or expired-claim task without claiming it. A reject cascades to every task blocked on it in the same turn, and a replacement is an additional task. "Re-point" is dropped. | §7.4, §9 |
| M1 | Accepted, with a different mechanism than suggested: dependencies are satisfied by a hive-side **acceptance record** (`accepted/<T>.json`, written by the master after keeping the merge), not by a git ancestry check. `claim` and `wait` run against the hive clone and have no reliable path to the project repo (hand-managed `./_swarm` hives, later cross-machine). The record means exactly "on integration and accepted". A shortfall resets integration to the pre-merge tip, and `accept --via` lets a follow-up unblock the original's dependents. `after` targets must already exist, so the plan is posted in order. | §7.4, §9 |
| M2 | Accepted. The undo order matches `leave` (release the baton, then tombstone, then remove the clone), and a missing worktree is a no-op. | §4.1 step 7, §5 |
| M3 | Accepted. `--path-format=absolute --git-common-dir`, and the cwd is never used. | §4.1 step 1 |
| M4 | Accepted. Heartbeat again before half the lease while a step is still running, for both roles. The trial's 60m override is no longer needed. | §7.6, §8, §9 |
| m1 | Accepted. Every `messages --new` passes `--hive` and `--to AGENT`. | §8, §9 |
| m2 | Accepted. The seen pair is the generation after the release, and a message naming a task lets the worker claim directly. | §7.3 |
| m3 | Accepted. The lock case exits 3 and is not a failure; §7.6 covers exit 1 only. | §7.1, §7.6 |
| m4 | Accepted. `leave` decides the worktree rule by path, a missing `<id>` worktree is a no-op, and a failure there never strands the clone. | §5 |
| n1 | Accepted. Two separate notes: based on HEAD, and MAIN dirty. | §4.1 step 10 |

