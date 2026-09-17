# rip-swarm — design / protocol spec (v0)

**Status:** advise-only first lock. Not implemented.  
**Repo root:** `/home/box/src/rip-swarm`  
**Date:** 2026-09-17  
**Audience:** operator + any harness that installs the skill

## 0. One-sentence model

A **git-backed hive** (folders + house rules) is the coordination board; agents claim work with **OpenMOSS-style JSONL claims**; install/update is a **skills.sh-style skill**; site prefs live in **profiles** (with a default); `/lookback` reads the store and proposes coordination improvements.

No serverless queue is required for v0. The repo *is* the board.

---

## 1. Locked pillars

| Pillar | Name | Lock |
|--------|------|------|
| **A** | Install / update | Matt-Pocock-style skill package: `SKILL.md` + installer compatible with common skills marketplaces / `skills.sh`-class flows. One install surface across Claude Code, Codex, Cursor, etc. |
| **B** | Hive house | Munder-difflin-shaped **house**: fixed folder layout, PROTOCOL.md house rules, agents read/write the tree; git is source of truth for coordination state that should survive sessions. |
| **C** | Claims | Fork **OpenMOSS / claude-codex-handoff** claim mechanics: append-only JSONL claims, claim → work → release/complete, race-safe enough for two harnesses on one hive. |
| **D** | Profiles | User/site preferences (review count, budgets, harness roles, …) live under `profiles/`. Missing override → **`profiles/default`**. |
| **E** | Slash cmds | Few custom commands only. Required: **`/lookback`** (alias `/evaluate`) — analyze hive + protocol traffic and suggest skill/coordination improvements. Optional later: `/status`, `/promote`. |

Prior queue shapes (CF Queues, Redis Streams, HTTP board, Omnigent meta-harness) remain **future adapters**, not v0 core. Omnigent is a peer product to compare against, not a dependency.

---

## 2. Goals / non-goals

### Goals (v0)

1. Operator can point any installed harness agent at a hive path and say: “you are orchestrator” — and that promotion is **visible in the hive**, not only in chat memory.
2. Workers pick up work via **claims**, not by racing on the same file without protocol.
3. Budget / subscription awareness is expressible in profile + claim metadata (soft limits), without building a billing system.
4. Code and long-lived memories stay in **project git repos**; the hive holds **tasks, claims, ops messages, and lookback artifacts**.
5. Install is one skill; site config is profiles, not forks of the skill.

### Non-goals (v0)

- Hosted multi-tenant SaaS messageboard.
- Replacing git for source code or agent memory stores.
- Guaranteed exactly-once delivery across machines (git+JSONL is best-effort + race rules).
- Auto-spawning cloud agents or spending money without operator policy in profile.
- Full A2A / MCP bus reimplementation (adapters later).
- Building the skill package itself in this doc pass (spec only).
- Linux-first packaging concerns unrelated to swarm (out of scope).

---

## 3. Repo / hive layout

Recommended on-disk shape for a **site hive** (may live inside a project repo or as a sibling `hive/`):

```text
hive/
  PROTOCOL.md                 # house rules (B) — human + agent readable
  profiles/
    default.yaml              # D — always present
    <site-or-user>.yaml       # overrides
  inbox/                      # unclaimed work items (one file or dir per item)
  claims/                     # active claim records (or pointer files)
  store/
    messages.jsonl            # append-only protocol / ops traffic
    claims.jsonl              # append-only claim log (C)
  topics/                     # optional topic partitions (by name)
    ops/
    tasks/
    results/
  orchestrator/               # current orchestrator lease + promote history
    CURRENT.md                # or CURRENT.json — who holds the baton
  lookback/                   # E outputs
    YYYY-MM-DD-*.md
  agents/                     # optional registry of known agent ids / harnesses
    registry.yaml
```

**Skill package** (separate from any one hive; what A installs):

```text
rip-swarm/                    # this repo / publishable skill
  SKILL.md                    # when-to-use + procedures
  docs/
    2026-09-17-design-spec.md # this document
  templates/
    hive/                     # scaffold copied on `init`
    profiles/default.yaml
  scripts/                    # optional helpers (claim, promote, lookback)
```

Agents **never** treat chat as durable coordination. If it matters for the swarm, it lands in `hive/`.

---

## 4. PROTOCOL (house rules)

`PROTOCOL.md` is binding for agents in the hive. Minimum sections:

1. **Roles** — orchestrator vs worker vs observer; who may promote whom.
2. **Claim lifecycle** — see §6.
3. **Race rules** — see §7.
4. **Promote** — how `/promote` or ops messages flip `orchestrator/CURRENT`.
5. **Budget** — how profile budgets constrain claim acceptance and tool use.
6. **Write rules** — append-only for `store/*.jsonl`; never rewrite history; corrections are new events.
7. **Secrets** — no tokens in hive files; profiles may name *policy*, not credentials.
8. **Lookback** — when to run; where to write; lookback does not auto-apply skill changes.

### Promote (orchestrator baton)

- Any agent the operator designates may become orchestrator **if** profile allows self-promote / operator-promote.
- Promote writes:
  - append event to `store/messages.jsonl` (`type: promote`)
  - update `orchestrator/CURRENT` (agent id, harness, timestamp, reason)
- Previous orchestrator becomes worker unless profile says dual-orchestrator (default: single).
- Stale lease: if `CURRENT` heartbeat older than `profile.orchestrator_lease_ttl`, a worker may propose reclaim via ops message; reclaim still follows race rules (claim the promote slot).

### Budget

Profile fields (illustrative):

```yaml
budget:
  max_claims_open_per_agent: 1
  max_reviews_per_plan: 2
  daily_token_soft_cap: null    # advisory
  allowed_harnesses: [claude-code, codex, cursor]
  spend_requires_operator: true
```

Workers **refuse** new claims that would violate soft caps and emit a `budget_block` message instead of silent drop.

---

## 5. Message schema (`store/messages.jsonl`)

One JSON object per line. Append-only.

```json
{
  "id": "msg_01J...",
  "ts": "2026-09-17T11:00:00+02:00",
  "type": "task|result|ops|promote|budget_block|note",
  "topic": "tasks|ops|results",
  "from": { "agent": "string", "harness": "string" },
  "to": "orchestrator|*|agent-id",
  "ref": { "claim_id": null, "task_id": null, "in_reply_to": null },
  "body": { },
  "profile": "default"
}
```

`body` is type-specific. Keep it small; large artifacts live as files under `topics/...` with a path in `body`.

---

## 6. Claim schema (`store/claims.jsonl` + optional `claims/` pointers)

Fork of OpenMOSS claim idea: **claim before mutate**.

```json
{
  "id": "clm_01J...",
  "ts": "2026-09-17T11:01:00+02:00",
  "action": "claim|heartbeat|complete|release|reject",
  "task_id": "task_...",
  "agent": "string",
  "harness": "string",
  "exclusive": true,
  "lease_seconds": 900,
  "note": "optional"
}
```

Lifecycle:

1. Task appears in `inbox/` (or `type:task` message).
2. Agent appends `action:claim` if no open exclusive claim exists for `task_id`.
3. Heartbeats renew lease while working.
4. `complete` with result pointer, or `release` / `reject` with reason.
5. Orchestrator (or profile-defined reviewer count) may require N distinct review claims before task closes.

**Active set:** derived by folding `claims.jsonl` (last wins per `task_id` subject to lease expiry). Optional `claims/<task_id>.json` mirrors the fold for humans; if mirror and log disagree, **log wins**.

---

## 7. Race rules

Git + multi-harness implies races. Rules:

1. **Append-only logs** — never edit prior JSONL lines.
2. **Exclusive claim** — two `claim` lines for same `task_id`: earliest valid claim by `(ts, id)` wins; loser must `release` or ignore and pick other work.
3. **File creates** — prefer create-new with unique ids over overwrite; use content-addressed or ULID names.
4. **Promote** — treat as exclusive claim on pseudo-task `orchestrator`; same earliest-wins rule.
5. **Push conflicts** — pull --rebase (or merge) then re-fold claims; do not force-push hive history.
6. **No silent steal** — stealing an unexpired exclusive claim requires `ops` + profile `allow_preempt: true` (default false).

These are **protocol** guarantees, not distributed-system proofs. Good enough for 2–5 agents on one hive.

---

## 8. Profiles (pillar D)

`profiles/default.yaml` ships with the skill template. Site files override by deep-merge (site wins).

Minimum keys:

```yaml
name: default
reviews_required_per_plan: 1
orchestrator_lease_ttl: 30m
allow_self_promote: false
allow_preempt: false
budget:
  max_claims_open_per_agent: 1
  max_reviews_per_plan: 1
  spend_requires_operator: true
lookback:
  min_messages_before_run: 20
  write_dir: lookback/
slash:
  enabled: [lookback, status]
```

Resolution order: explicit profile name in message / CLI → env `RIP_SWARM_PROFILE` → `default`.

---

## 9. Slash commands (pillar E)

Keep the surface tiny.

| Command | Purpose |
|---------|---------|
| `/lookback` (alias `/evaluate`) | Read `store/messages.jsonl` + `claims.jsonl` (+ PROTOCOL), summarize coordination failure modes, propose skill/PROTOCOL/profile patches. Write report under `lookback/`. **Does not auto-merge** changes. |
| `/status` | Fold claims + show orchestrator CURRENT + open inbox counts. |
| `/promote <agent>` | Operator-gated baton move (optional if promote-via-chat+ops is enough). |

`/lookback` output should be actionable diffs-in-prose (suggested PROTOCOL bullets, profile knobs, claim hygiene), not a generic “swarm health score.”

---

## 10. Install surface (pillar A)

- Publish as a skill with `SKILL.md` describing when to use rip-swarm and the claim/promote/lookback procedures.
- Install path aligns with common skills installers (`npx skills` / skills.sh-class / harness-native skill dirs). Exact marketplace pins TBD after Researcher marketplace pass.
- `init` (doc’d in SKILL, optional script): copies `templates/hive` into a target path, does not overwrite existing `profiles/`.

v0 skill teaches agents to obey PROTOCOL with ordinary file tools; helper scripts are convenience, not required for correctness.

---

## 11. Relationship to earlier queue options

| Shape | Role in rip-swarm |
|-------|-------------------|
| Git hive (this spec) | **v0 core** |
| MCP / local mailbox bus | Optional adapter: mirror topics ↔ hive |
| CF Queues + Worker | Optional cross-machine fan-out later |
| Redis Streams | Optional for always-on terminal workers |
| Omnigent | External meta-harness; compare; do not hard-depend |

Promotion and claims stay hive-native so adapters cannot fork the source of truth.

---

## 12. Open questions (do not block v0 scaffold)

1. Single-repo hive vs always-sibling `hive/` for multi-repo products.
2. Whether claim fold mirrors are mandatory or log-only.
3. Exact skills marketplace packaging after install-path research.
4. How Grok Bot swarm maps agent ids into `agents/registry.yaml` (optional bridge).
5. Whether `/lookback` may open a PR vs markdown-only suggestions.

---

## 13. Recommended next moves

1. Scaffold `templates/hive` + `profiles/default.yaml` + stub `PROTOCOL.md` (still no full skill logic).
2. Freeze message/claim JSON schemas as JSON Schema files under `docs/schema/`.
3. Researcher: pin the preferred skills installer URL / marketplace row for pillar A.
4. Spike: two harnesses, one hive, claim race + promote — prove §7 by demo, not essay.
5. Only then author `SKILL.md` procedures tightly against the frozen schemas.

---

## 14. Decision log

| When | Decision |
|------|----------|
| 2026-09-17 | Pillars A–E locked as above; first spec is this file. |
| 2026-09-17 | v0 coordination = git hive + JSONL claims; queues deferred to adapters. |
| 2026-09-17 | Profiles + default required; `/lookback` required among few slash cmds. |
| 2026-09-17 | Skill not built in this pass — spec only. |
