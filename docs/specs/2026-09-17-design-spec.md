# rip-swarm — design / protocol spec (v0.2)

**Status:** advise-only. First review (§15) folded 2026-09-17; second review (§17: spec + plan cross-check) folded 2026-09-17. v0 implementation defaults locked (§13). Plan: `docs/plans/2026-09-17-rip-swarm.md`. Not implemented.  
**Repo:** this repository’s root (no absolute machine path).  
**Date:** 2026-09-17  
**Audience:** operator + any harness that installs the skill

## 0. One-sentence model

A **git-backed hive** (folders + house rules on a dedicated **`swarm` branch of the project repo**, mounted as a worktree at `_swarm/`) is the coordination board; agents take work with **create-only claim files** (OpenMOSS-inspired leases; JSONL is audit, not the lock); install/update is an **Agent Skills / skills.sh-class skill**; site prefs live in **profiles** (with a default); `/lookback` reads the store and proposes coordination improvements.

No hosted queue is required for v0. The hive directory *is* the board. Multi-harness `git push` is a **known-weaker** concurrency model than munder’s single-committer house — exclusivity comes from create-only claim paths whose first push wins, not from shared JSONL earliest-wins.

---

## 1. Locked pillars

| Pillar | Name | Lock |
|--------|------|------|
| **A** | Install / update | Agent Skills `SKILL.md` installable via skills.sh-class CLIs (`npx skills` / equivalent). One *skill package*; harness invocation still differs (slash vs “ask the agent”). Not “one marketplace pin already chosen.” |
| **B** | Hive house | **munder-shaped layout names + PROTOCOL.md**, deliberately **not** munder concurrency. Munder: single committer, per-agent outbox, no co-edited mailbox. rip-swarm: multi-harness push allowed; writes use create-only / per-agent paths where races matter; shared append-only logs merge with git `merge=union`. The hive lives on its **own branch and own worktree** (`swarm` → `_swarm/`) so helpers can run git on it without touching code branches or the developer’s working tree. Document the fork — do not claim “we are a munder house.” |
| **C** | Claims | **Inspired by OpenMOSS leases, not a fork of OpenMOSS JSONL-as-claim.** Exclusivity = create-only file `claims/<task_id>.json` (`O_CREAT\|O_EXCL` locally; first push to the hive remote wins). `store/claims.jsonl` is append-only **audit**. Claim success = your claim file is on the **remote tip** (push accepted) before any project mutation. |
| **D** | Profiles | User/site preferences under `profiles/`. Missing override → **`profiles/default.yaml`**. Deep-merge; lists **replace** (not concat) unless a key documents otherwise. |
| **E** | Slash cmds | Few only. Required: **`/lookback`** (alias `/evaluate`). v0 also ships **`/status`** (read-only fold). **No `/promote` slash command in v0** — promote is a helper call governed by PROTOCOL. |

Prior queue shapes (CF Queues, Redis Streams, HTTP board, Omnigent) remain **future adapters**, not v0 core. Omnigent is a peer product to compare against, not a dependency.

---

## 2. Goals / non-goals

### Goals (v0)

1. Operator can point any installed harness agent at a hive path (`RIP_SWARM_HIVE`) and designate an orchestrator — promotion is **visible in the hive**, not only in chat memory.
2. Workers pick up work via **exclusive claim files**, not by racing on a shared JSONL fold alone.
3. **Budget (narrow):** profile soft caps on open claims + `spend_requires_operator` prose. **No** cross-harness token accounting in v0. Empty token budgets are cut, not faked.
4. Code and long-lived memories stay in **project git repos**; the hive holds **tasks, claims, ops messages, and lookback artifacts**.
5. Install is one skill; site config is profiles, not forks of the skill.

### Non-goals (v0)

- Hosted multi-tenant SaaS messageboard.
- Replacing git for source code or agent memory stores.
- Guaranteed exactly-once delivery / munder-strength single-committer safety.
- Auto-spawning cloud agents or spending money without operator policy.
- Full A2A / MCP bus reimplementation (adapters later).
- Token-accurate billing across Claude/Codex/Cursor.
- Building the skill package in the first doc-only pass (templates + PROTOCOL may scaffold after claim primitive spike).

---

## 3. Repo / hive layout

**Each project that needs cooperation carries its own hive, on a dedicated branch.** The hive is an **orphan branch named `swarm`** in the project repository, pushed to the project’s normal remote (`origin/swarm`), and checked out locally as a **linked git worktree** at **`<repo>/_swarm/`**. Code branches list `_swarm/` in `.gitignore` so the directory is never committed onto `main` or a feature branch. The remote therefore holds the complete hive contents and history (`git log origin/swarm`, `git show origin/swarm:store/claims.jsonl`) without anything extra to host.

Helpers refuse to run git operations unless the hive directory **is** a git work-tree root (`git rev-parse --show-toplevel` == hive dir) with an upstream. A linked worktree satisfies this; so does a separate sibling repository, which remains the option for multi-repo products. Path override: `RIP_SWARM_HIVE` / `--hive`; default `./_swarm`.

Why a separate branch and worktree, not hive files on the code branch: (1) coordination state must be on exactly one shared line of history, while agents work on feature branches; (2) heartbeats and claims must push while the agent’s code tree is dirty, which `pull --rebase` on the code branch refuses; (3) lost-race recovery uses `reset --hard`, which inside the code worktree would destroy uncommitted work. In the `_swarm/` worktree it only ever touches hive files.

```bash
# first agent on a project (init does this)
git worktree add --orphan -b swarm _swarm     # git ≥ 2.42
echo '_swarm/' >> .gitignore
git -C _swarm push -u origin swarm
# every later clone (init attaches when origin/swarm exists)
git worktree add --track -b swarm _swarm origin/swarm
```

```text
_swarm/                       # linked worktree of branch `swarm` (work-tree root)
  .gitattributes              # store/*.jsonl merge=union
  PROTOCOL.md
  profiles/
    default.yaml
    <site>.yaml
  inbox/                      # SoT for tasks: one JSON file per task
    <task_id>.json
  claims/                     # SoT for exclusivity: create-only
    <task_id>.json            # active claim (exact path only)
    <task_id>.<action>.<UTC>.json   # tombstones (see §7)
  store/
    messages.jsonl            # audit / ops traffic (append-only, union-merged)
    claims.jsonl              # audit of claim lifecycle (append-only, union-merged)
  agents/
    <agent_id>/
      outbox/                 # agent-only writes (munder-like)
    registry.yaml             # {id, harness, role}[]
  orchestrator/
    CURRENT.json              # mirror of baton; must match claim on task_id=orchestrator
  lookback/
    YYYY-MM-DD-*.md
```

**No** shared co-edited mailbox file. **No** dual SoT for the same fact (see §4).

**Skill package** (this repo / publishable skill):

```text
rip-swarm/
  SKILL.md                    # name must match parent dir after install
  README.md
  rip_swarm/                  # stdlib Python runtime (package)
  scripts/                    # thin CLIs; each inserts the skill dir into sys.path so they run from any cwd
  tests/                      # unittest
  docs/
    specs/
      2026-09-17-design-spec.md
      schema/                 # JSON Schema once frozen post-spike
    plans/
      2026-09-17-rip-swarm.md
  templates/_swarm/           # copied by init into the hive dir — only after PROTOCOL survives spike
```

After install the skill lives under a harness skills directory, not the project. Commands are therefore documented as `python "$SKILL_DIR/scripts/<cmd>.py"`, never `PYTHONPATH=.`.

Agents never treat chat as durable coordination. If it matters for the swarm, it lands in the hive.

---

## 4. One writer-of-record per fact

| Fact | Source of truth | Audit / mirror |
|------|-----------------|----------------|
| Task | `inbox/<task_id>.json` | optional `type:task` line in messages.jsonl |
| Exclusive claim | `claims/<task_id>.json` (create-only) | `store/claims.jsonl` |
| Orchestrator baton | claim file `claims/orchestrator.json` | `orchestrator/CURRENT.json` **must** match; messages `type:promote` is audit |
| Large artifacts | files under paths referenced by messages | — |

If mirror and SoT disagree: **SoT wins**; rewriter of CURRENT must repair from claim file. Messages never override claims or inbox.

**CURRENT vs claim states:** both present and `agent` + `claim_id` equal and claim unexpired → healthy. Both absent → *no orchestrator* (not an error). Any other combination (one missing, fields differ, claim expired but CURRENT present) → **mismatch**, reported by `/status`.

One commit per promote: claim create + CURRENT write + audit append + promote outbox message. One commit per baton release: claim tombstone + CURRENT delete + audit append. Partial commits are a doctor/status failure.

---

## 5. PROTOCOL (house rules)

`PROTOCOL.md` is binding once a hive is initialized from a **spike-proven** template — do not ship an empty binding stub.

Minimum sections: Roles · One writer-of-record · Claim lifecycle · Race rules · Promote · Budget · Write rules · Trust · Lookback / status.

### Promote

1. `promote` names the new holder (`agent`) and the actor (`by`, default = `agent`). Allowed iff `by` ∈ profile `operators`, or `by == agent` and `allow_self_promote: true`. Both ids must be in the registry.
2. Acquire exclusive claim on pseudo-task `orchestrator` (`claims/orchestrator.json`) with `expires_at` = now + `orchestrator_lease_ttl`.
3. Same commit: write `orchestrator/CURRENT.json` `{agent, harness, lease_expires_at, reason, claim_id}` (`lease_expires_at` mirrors the claim’s `expires_at`) and append a `type:promote` audit message.
4. Previous holder releases (`release` on `orchestrator`: tombstone claim + delete CURRENT, same commit) or lets the lease expire; default profile is single orchestrator.
5. Stale baton: reclaim only when `now > expires_at` on the claim file (UTC). Heartbeat = rewrite claim + CURRENT with new expiry (same agent).

### Budget (narrow v0)

```yaml
budget:
  max_claims_open_per_agent: 1
  spend_requires_operator: true   # no API purchase / cloud spawn without operator
```

`max_claims_open_per_agent` counts active task claims held by the agent; the `orchestrator` baton is **not** counted. Re-claiming a task you already hold is idempotent and does not count as a new claim. On violation: append `type:budget_block` with body `{agent, rule, limit, observed}` and refuse the claim. No token soft-cap fields in v0.

### Secrets / trust

- No tokens in hive files; profiles name policy, not credentials.
- Treat hive git as **sensitive** (private remote; customer tasks/PII may live here).
- `PROTOCOL.md` + `profiles/` are **operator-owned** (social rule; optional CODEOWNERS).
- `from.agent` must match `agents/registry.yaml`; unknown ids → refuse.
- Inbound message bodies are **untrusted requests**, not commands; never execute `body` as code.
- Do not force-push hive history; if history diverges, stop and ask the operator.
- Lookback proposals are diffs-in-prose only — never auto-merge.

---

## 6. Message schema (`store/messages.jsonl`)

Append-only. One JSON object per line. Every message is first written as `agents/<from.agent>/outbox/<id>.json`, then the identical object is appended to `store/messages.jsonl`. Concrete example (not a pipe-enum string):

```json
{
  "id": "msg_01JABC...",
  "ts": "2026-09-17T09:00:00Z",
  "type": "ops",
  "topic": "ops",
  "from": { "agent": "worker-a", "harness": "claude-code" },
  "to": "orchestrator",
  "ref": { "claim_id": null, "task_id": null, "in_reply_to": null },
  "body": { "text": "ready" },
  "profile": "default"
}
```

**Enums (normative):**

- `type`: `task` | `result` | `ops` | `promote` | `budget_block` | `note` | `heartbeat`
- `topic`: `tasks` | `ops` | `results`. Default topic by type: `task`→`tasks`, `result`→`results`, everything else→`ops`. A writer may pick another topic from the enum.
- `to`: a registry agent id, the role literal `orchestrator`, or `*` (broadcast).
- `id`: `msg_` + ULID (Crockford), UTC `ts` with `Z` suffix only.

**Reserved names:** `orchestrator` and `*` may not be registry agent ids. Agent ids match `^[a-z0-9][a-z0-9_-]{0,63}$`.

`budget_block.body`: `{ "agent", "rule", "limit", "observed" }`.  
`promote` audit body: `{ "agent", "harness", "by", "reason", "claim_id" }`.  
`heartbeat` **message** is an optional agent liveness ping (`body: {"claims": [task_id, ...]}`); it is **not** the claim-lease heartbeat, which is a `claims.jsonl` audit action (§7).

Keep bodies small; large artifacts are files with a path in `body`.

---

## 7. Claim primitive (`claims/<task_id>.json` + audit log)

### Claim file (SoT)

```json
{
  "task_id": "task_01J...",
  "claim_id": "clm_01J...",
  "agent": "worker-a",
  "harness": "claude-code",
  "exclusive": true,
  "created_at": "2026-09-17T09:01:00Z",
  "expires_at": "2026-09-17T09:16:00Z",
  "note": "optional"
}
```

Create with `O_CREAT|O_EXCL` (helpers) — local exclusivity only. **Cross-clone exclusivity is “first push wins”:** work (mutate project / inbox completion) only after your push was accepted, or after a re-fetch shows *your* claim file on the remote tip (§8).

`try_claim` requires `inbox/<task_id>.json` to exist, except for the reserved `orchestrator` id. A holder re-claiming its own unexpired claim gets the existing claim back (idempotent, same `claim_id`). Worker lease length comes from profile `worker_lease_ttl` (default `15m`); the orchestrator lease from `orchestrator_lease_ttl`.

### Tombstones

`complete` / `release` / `reject` / `expired` **rename** the active file to `claims/<task_id>.<action>.<YYYYMMDDTHHMMSSZ>.json`. The active path is only the exact `claims/<task_id>.json`. A tombstone keeps the claim body; `complete` adds `result_ref`, and any action may update `note`.

### Audit line (`store/claims.jsonl`)

```json
{
  "claim_id": "clm_01J...",
  "ts": "2026-09-17T09:01:00Z",
  "action": "claim",
  "task_id": "task_01J...",
  "agent": "worker-a",
  "harness": "claude-code",
  "expires_at": "2026-09-17T09:16:00Z",
  "result_ref": null,
  "note": null
}
```

`action`: `claim` | `heartbeat` | `complete` | `release` | `reject` | `expired`. `claim_id` repeats across the lifecycle lines of one claim; lines have no id of their own.  
Reviews (**locked**, §13): helpers do not enforce `reviews_required_per_plan`; the orchestrator creates ordinary child inbox tasks for reviews. There is no `review` action.

`complete` **requires** `result_ref` (path). Non-holder `heartbeat`/`complete`/`release`/`reject` are refused by helpers and ignored by fold/doctor. An **expired** holder is treated as a non-holder: heartbeat before expiry, or re-claim (which tombstones the expired file as `expired` and creates a fresh claim) before completing.

### Lifecycle

1. Task file appears in `inbox/<task_id>.json` with `{id, title, created_at, created_by}` (optional `body`).
2. Agent runs helper (or PROTOCOL steps): fetch/pull hive tip → create claim file → append audit → commit → push.
3. If push is rejected: fetch; if the claim file on the remote tip is not yours, reset the hive to the remote tip and pick other work (§8). Do not mutate the project.
4. Heartbeat: update `expires_at` on claim file + audit `heartbeat`; push.
5. Complete/release/reject: tombstone rename + audit line in the same commit; push.

### Fold / active holder (pseudocode)

```
active(task_id, now):
  path = claims/task_id.json
  if path missing: return FREE
  doc = read(path)
  if doc.expires_at <= now: return EXPIRED(doc.agent, doc.claim_id)   # steal only via rename-to-expired then create
  return HOLDER(doc.agent, doc.claim_id)
```

JSONL is **not** consulted for exclusivity. Earliest-wins-on-JSONL is **removed** as a lock rule.

---

## 8. Race rules

1. Append-only for `store/*.jsonl` — never edit prior lines. Concurrent appends from different clones are merged by git with `merge=union` (`_swarm/.gitattributes`: `store/*.jsonl merge=union`). Line order in the audit may therefore differ from wall-clock order; readers sort by `ts` when it matters.
2. Exclusivity = create-only claim file whose commit reaches the remote tip first; the loser does not start work.
3. Prefer unique ids (ULID) for all new files. Claim files and inbox files never get `merge=union`: an add/add conflict on them means you lost.
4. Promote = claim on `orchestrator` + CURRENT + promote message in one commit.
5. **Publish procedure (helpers):** require a clean hive work-tree → `git fetch` → check the remote tip for `claims/<task_id>.json` → run the local operation → commit only the paths written → `git push`. On rejection: `git fetch`; if the remote tip now has that claim file held by someone else, `git reset --hard @{u}` and report *lost race*; otherwise `git rebase @{u}` (union merge handles JSONL) and push again. At most 5 attempts, then stop and report. `reset --hard` is safe only because the hive is its own worktree on its own branch and that tree was clean.
6. Never force-push the hive.
7. No silent steal of unexpired claims. `allow_preempt` is reserved for a later version: v0 helpers ignore it and never preempt. Expired: rename to `expired` then create (helper).
8. Leases use each machine’s clock in UTC. Assume skew under one minute; heartbeat at or before half the lease.

Helpers (`scripts/claim.py`, etc.) are **required for correctness until** a two-clone spike proves raw file tools suffice.

---

## 9. Profiles (pillar D)

`profiles/default.yaml` ships with the template. Site files deep-merge (site wins; **lists replace**).

```yaml
name: default
reviews_required_per_plan: 1   # advisory for the orchestrator; helpers do not enforce
orchestrator_lease_ttl: 30m
worker_lease_ttl: 15m
allow_self_promote: false
allow_preempt: false           # reserved; ignored by v0 helpers
operators: []                  # agent ids allowed to promote anyone (incl. themselves)
budget:
  max_claims_open_per_agent: 1
  spend_requires_operator: true
lookback:
  min_messages_before_run: 20
  write_dir: lookback/         # relative to hive root
slash:
  enabled: [lookback, status]  # advisory for harness integrations; helpers ignore
```

Resolution: explicit profile in message/CLI → `RIP_SWARM_PROFILE` → `default`.  
Hive path: `--hive` → `RIP_SWARM_HIVE` → `./_swarm` if it exists → error.

Durations (**locked**, §13): JSON timestamps are UTC `Z` only; helper calls take integer `lease_seconds`; profile `*_ttl` keys are duration strings (`45s`, `30m`, `1h`) or bare integers (seconds), parsed to seconds.

---

## 10. Slash commands (pillar E)

| Command | Purpose |
|---------|---------|
| `/lookback` (`/evaluate`) | Scan hive; write `<write_dir>/YYYY-MM-DD.md` (`-2`, `-3` suffixes on the same day). Does not auto-merge. |
| `/status` | Read-only: orchestrator (agent, matches claim, expired), active claims, expired claim files still at the active path, inbox tasks without a claim, unknown agents on claims, CURRENT mismatch, JSONL parse errors. |

**Lookback report minimum headings:** Double claims · Expired leases · CURRENT vs last promote · Inbox with no claim · JSONL parse errors · Suggested PROTOCOL/profile diffs (prose).

“Double claims” means audit drift: `claims.jsonl` shows a `claim` for a task by an agent other than the file holder with no tombstone in between. Two active files for one task cannot exist by construction.

Portable skill also triggers on description keywords (`lookback`, hive, claims) for harnesses without slash commands.

---

## 11. Install surface (pillar A)

- `SKILL.md` with YAML `name` + `description`; `name` matches parent directory after install.
- Install via Agent Skills / skills.sh-class CLIs — exact marketplace row TBD (Researcher).
- Do not claim plugin-subscribe and `npx skills add` are the same surface; document both if both matter.
- `init` (run from inside the project repo): if `origin/swarm` already exists, **attach** — `git worktree add --track -b swarm _swarm origin/swarm` and stop (the hive is already initialized). Otherwise **scaffold** — `git worktree add --orphan -b swarm _swarm`, copy `templates/_swarm` in, commit `init hive`, and append `_swarm/` to the project’s `.gitignore` if missing; the operator then runs `git -C _swarm push -u origin swarm`. Refuse to clobber an existing `PROTOCOL.md` / `profiles/` unless `--force`. `--no-git` copies the template only (tests, or a sibling repo the operator manages). Old git without `worktree add --orphan`: `worktree add --detach` then `checkout --orphan swarm` and clear the tree.
- Scripts run from any cwd: each `scripts/*.py` inserts its own skill directory into `sys.path` before importing `rip_swarm`.

---

## 12. Relationship to earlier queue options

| Shape | Role |
|-------|------|
| Git hive (this spec) | **v0 core** |
| MCP / local mailbox | Optional adapter later |
| CF Queues + Worker | Optional cross-machine fan-out |
| Redis Streams | Optional always-on workers |
| Omnigent | Peer meta-harness; compare; no hard dependency |

---

## 13. Decision log (including review locks)

| When | Decision |
|------|----------|
| 2026-09-17 | Pillars A–E locked; first spec drafted. |
| 2026-09-17 | v0 coordination = git hive; queues → adapters. |
| 2026-09-17 | Design review §15: needs revision (14 issues). |
| 2026-09-17 | **Claim primitive:** create-only `claims/<task_id>.json` + `expires_at`; JSONL audit only. Not OpenMOSS JSONL-as-claim. |
| 2026-09-17 | **munder:** layout/PROTOCOL names only; concurrency is deliberate weaker multi-push model. |
| 2026-09-17 | **SoT:** inbox files, claim files, orchestrator claim; CURRENT + JSONL are mirrors/audit. |
| 2026-09-17 | **Helpers required** until two-harness spike proves otherwise. |
| 2026-09-17 | **Budget:** cut token accounting from v0; keep open-claim caps + spend_requires_operator. |
| 2026-09-17 | **Next-move order:** lock primitives (done here) → write fold/lease/promote in PROTOCOL → spike → scaffold templates → SKILL.md; marketplace pin parallel. |
| 2026-09-17 | **Implementation defaults (operator lock):** (1) exclusivity = `O_CREAT\|O_EXCL` claim files + `expires_at`; JSONL audit only. (2) SoT as §4 — inbox files, `claims/<task_id>.json`, baton = `claims/orchestrator.json` with `CURRENT.json` required mirror. (3) multi-harness `git push` + per-agent outbox; not munder single-committer. (4) helpers required until two-clone spike says otherwise. (5) no token accounting; `spend_requires_operator` is PROTOCOL prose; `max_claims_open_per_agent` stays as the only numeric cap. |
| 2026-09-17 | **Tombstone:** complete/release/reject/expired rename `claims/<task_id>.json` → `claims/<task_id>.<action>.<YYYYMMDDTHHMMSSZ>.json`. Active path is only the exact `claims/<task_id>.json`. |
| 2026-09-17 | **Durations:** JSON timestamps are UTC `Z` only; `lease_seconds` is an integer on helper calls; profile `*_ttl` keys are duration strings (`30m`) parsed to seconds. |
| 2026-09-17 | **Reviews:** helpers do not enforce `reviews_required_per_plan`; orchestrator creates ordinary child inbox tasks. No `review` action. |
| 2026-09-17 | **`/promote`:** not a v0 slash command. Promote via helper + PROTOCOL (claim `orchestrator`). |
| 2026-09-17 | **On-disk name:** each hive checkout is a directory named `_swarm/` (not `hive/`). `RIP_SWARM_HIVE` / `--hive` still name the path; default is `./_swarm`. |
| 2026-09-17 | **Docs layout:** `docs/specs/` for design specs + JSON Schema; `docs/plans/` for implementation plans. No `docs/superpowers/`. |
| 2026-09-17 | **Hive = `swarm` branch + `_swarm/` worktree** (review 2, R1; operator lock after discussion): the hive is an orphan branch of the project repo, pushed to the project remote, mounted as a linked worktree at `_swarm/`, gitignored on code branches. Helpers refuse git ops unless the hive dir is a work-tree root with an upstream. A sibling separate repo is the multi-repo option. The remote holds full hive contents + history for analysis. Resolves §16 Q1. |
| 2026-09-17 | **JSONL union merge** (R2): `_swarm/.gitattributes` ships `store/*.jsonl merge=union`. Claim/inbox files never union-merge. |
| 2026-09-17 | **Publish loop** (R3): clean tree → fetch → check remote tip → op → commit → push; on rejection re-fetch, lost race ⇒ `reset --hard @{u}`, else rebase + retry (max 5). One generic helper serves claim, heartbeat, complete/release/reject, promote. |
| 2026-09-17 | **Baton release + cap** (R4/R5): `release` on `orchestrator` tombstones the claim and deletes CURRENT in one commit; the baton does not count toward `max_claims_open_per_agent`. |
| 2026-09-17 | **Promote actor** (R6): `promote` carries `by` (default = `agent`); allowed iff `by ∈ operators` or self-promote with `allow_self_promote`. |
| 2026-09-17 | **Profile keys** (R7): add `worker_lease_ttl: 15m`; `allow_preempt`, `reviews_required_per_plan`, `slash.enabled` are advisory/reserved in v0; `lookback.write_dir` is honored. |
| 2026-09-17 | **Audit line key** (R8): `claims.jsonl` lines carry `claim_id` (not `id`); `expired` is an audit action. |
| 2026-09-17 | **Install paths** (R9): scripts add the skill dir to `sys.path`; SKILL.md documents `python "$SKILL_DIR/scripts/<cmd>.py"`, never `PYTHONPATH=.`. `init` creates or attaches the `swarm` worktree. |

---

## 14. Recommended next moves (reordered)

1. ~~Decision lock on claim primitive + SoT~~ — done in §4, §7, §13.
2. Execute `docs/plans/2026-09-17-rip-swarm.md` (stdlib helpers → two-clone git spike → PROTOCOL/templates → SKILL.md).
3. Parallel anytime: Researcher pins skills installer / marketplace row.

---

## 15. Design review (2026-09-17) — record + dispositions

**Original verdict:** needs revision (1 critical, 8 major, 3 minor, 2 nit).  
**Sources:** OpenMOSS/claude-codex-handoff PROTOCOL.md; munder-difflin HIVE.md.  
**Fold status (v0.1):** body above revised. Issue dispositions:

| # | Sev | Disposition |
|---|-----|-------------|
| 1 | critical | **Accepted.** Fold contradiction removed; exclusivity = claim files + pulled tip; pseudocode in §7. |
| 2 | major | **Accepted.** Pillar C rewritten: inspired by OpenMOSS leases, not JSONL fork. |
| 3 | major | **Accepted.** Pillar B documents deliberate munder concurrency fork; per-agent outbox added. |
| 4 | major | **Accepted.** §4 one SoT table; CURRENT.json frozen; messages audit-only for baton. |
| 5 | major | **Accepted in prose.** Enums, `result_ref`, inbox shape, UTC Z; JSON Schema files still next-move 3. |
| 6 | major | **Accepted.** Goal 3 narrowed; token caps cut; `budget_block` body specified. |
| 7 | major | **Accepted.** §5 secrets/trust expanded. |
| 8 | major | **Accepted.** Decision log rows for real forks. |
| 9 | major | **Accepted.** §14 reordered; no binding stub PROTOCOL before spike. |
| 10 | minor | **Accepted.** Lookback headings + `/status` in v0; lease via `expires_at`. |
| 11 | minor | **Accepted.** `RIP_SWARM_HIVE`, operators list, init `--force`, registry minimum. |
| 12 | minor | **Accepted.** Install wording corrected (Agent Skills; slash not universal). |
| 13 | nit | **Accepted.** Absolute path dropped. |
| 14 | nit | **Accepted.** Concrete JSON examples + separate enums. |

Historical review narrative (strengths + full issue writeups) lived in the pre-fold §15 commit `c897abf`; this section keeps dispositions as the standing record. Full verbatim review text is recoverable from that commit if needed.

---

## 16. Open questions (remaining)

1. ~~In-repo `_swarm/` vs always-sibling `_swarm/`~~ — **locked:** in-repo as the `swarm` branch worktree; sibling repo only for multi-repo products (§3, §13 R1). Init default is `./_swarm`.
2. ~~Claim tombstone vs delete-on-complete~~ — **locked:** rename tombstone (see §13).
3. Exact skills marketplace pin (Researcher; not on the implementation critical path).
4. Optional Grok Bot ↔ `agents/registry.yaml` bridge (out of v0 plan).
5. Whether `/lookback` may open a PR later (v0 = markdown only).

---

## 17. Second review (2026-09-17) — spec v0.1 × plan cross-check

**Verdict:** needs revision (2 critical, 7 major, 12 minor). All folded into this v0.2 and into the plan.

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| R1 | critical | Plan ran `pull --rebase` / lost-race recovery inside the *project* repo (`<repo>/_swarm`); `reset --hard` there destroys uncommitted project work, and hive commits interleave with code commits. | **Accepted.** Hive is a dedicated `swarm` branch mounted as a worktree at `_swarm/`; helpers refuse anything that is not a work-tree root (§3, §8.5, §11). |
| R2 | critical | Concurrent appends to `store/*.jsonl` from two clones are add/add conflicts in git, so every second push would fail to rebase. | **Accepted.** `.gitattributes` `merge=union` for `store/*.jsonl`; two-clone test added (plan Task 11). |
| R3 | major | Plan step “pull --rebase then check file” lands in a conflicted rebase for the claim file; procedure was underspecified and hard-coded `master`. | **Accepted.** Publish loop in §8.5; branch from `@{u}`; generic `publish` helper in plan Task 11. |
| R4 | major | No path to release the orchestrator baton; `complete` on `orchestrator` would demand a `result_ref`; CURRENT left dangling. | **Accepted.** `release_orchestrator` (§4, §5.4). Both-absent = no orchestrator, not mismatch. |
| R5 | major | Orchestrator baton counted toward `max_claims_open_per_agent: 1`, so the orchestrator could never claim a task. | **Accepted.** Baton excluded from the cap (§5). |
| R6 | major | `operators` comment (“promote without self-promote”) and plan test disagreed on who is checked; no actor field. | **Accepted.** `by` actor on promote (§5.1, §6). |
| R7 | major | Worker lease hard-coded (`900`) in plan while orchestrator lease came from profile; `write_dir`, `allow_preempt`, `slash.enabled` present but unused. | **Accepted.** `worker_lease_ttl` added; others marked advisory/reserved; `write_dir` honored (§9). |
| R8 | major | Spec §7 (“pick one in PROTOCOL”), §7.5 (“delete or tombstone”), §9 (“freeze one”) still read as open although §13 had locked them; plan relied on “§13 wins”. | **Accepted.** Body text now states the locks. |
| R9 | major | SKILL.md documented `PYTHONPATH=. python -m rip_swarm`, which fails once the skill is installed outside the project. | **Accepted.** `sys.path` shim in scripts; `$SKILL_DIR` commands (§3, §11). |
| R10 | minor | §5 used `lease_expires_at` for the claim file; the claim field is `expires_at`. | **Accepted.** |
| R11 | minor | Message `heartbeat` type undefined vs claim heartbeat; `to` values and reserved ids undefined; no type→topic default. | **Accepted.** §6. |
| R12 | minor | Claim re-claim by holder (idempotent) and expired-holder behaviour unspecified. | **Accepted.** §7. |
| R13 | minor | `claims.jsonl` `id` repeated across lifecycle lines; `expired` missing from audit actions. | **Accepted.** `claim_id`; `expired` added. |
| R14 | minor | Tombstone body shape (`result_ref`, `note`) unspecified. | **Accepted.** §7. |
| R15 | minor | `/status` output in spec (4 items) narrower than plan report keys. | **Accepted.** §10 lists the plan’s keys. |
| R16 | minor | Skill package layout omitted `rip_swarm/` and `tests/`. | **Accepted.** §3. |
| R17 | minor | Pillar E still said `/promote` optional. | **Accepted.** |
| R18 | minor | Plan Task 4 carried a stale “correct the expected string” note that matched the test already; Task 11 setUp pulled into an empty clone; Task 6 loader lacked flow lists (`[]`, `[a, b]`) that the templates use; Task 2 `resolve_hive` lacked the `./_swarm` default. | **Accepted** in plan. |
| R19 | minor | Plan ended with chat residue (“Which approach?”). | **Accepted.** Removed. |
| R20 | minor | README pillar line still said “OpenMOSS claims”. | **Accepted.** |
| R21 | minor | Clock skew across machines unaddressed. | **Accepted.** §8.8 (assumption + heartbeat at half lease). |
