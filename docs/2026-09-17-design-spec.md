# rip-swarm — design / protocol spec (v0)

**Status:** advise-only first lock. Not implemented. Design review in §15 (needs revision; issues open).  
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
| 2026-09-17 | Design review recorded in §15. Spec body above is unchanged; review is additive. Issues remain open. |

---

## 15. Design review (2026-09-17)

**Verdict:** needs revision.  
**Counts:** 1 critical, 8 major, 3 minor, 2 nit (14 issues, all open).  
**Sources checked:** this file + README (repo is spec-only); [OpenMOSS/claude-codex-handoff PROTOCOL.md](https://github.com/OpenMOSS/claude-codex-handoff/blob/main/PROTOCOL.md) v1.12; [munder-difflin HIVE.md](https://github.com/chaitanyagiri/munder-difflin/blob/main/HIVE.md).

The v0 model (git hive as the board, no hosted queue, profiles + `/lookback`) is a coherent product bet, but an engineer cannot implement exclusive claims, promote, or lease expiry from this spec, and the OpenMOSS / munder “forks” omit the primitives that made those systems race-safe.

This section does **not** rewrite §§0–14. It is the review of that lock.

### Strengths

- Honest status: advise-only, skill not built; repo contents match (README + this spec only).
- Locked pillars A–E and the v0 bet (“repo *is* the board; queues are adapters”) are easy to carry into a spike; non-goals (SaaS, exactly-once, auto-spend, full MCP bus) are the right cuts.
- Profiles + `profiles/default` (pillar D) and “site config is profiles, not forks of the skill” is a clean install/config split.
- `/lookback` as required, non-auto-applying, diffs-in-prose is a good evaluation loop; alias `/evaluate` is fine.
- Write rule “append-only JSONL; corrections are new events” and “no silent steal unless `allow_preempt`” are the right instincts even if the primitive is wrong.
- Next-move 4–5 (prove races with two harnesses, then write `SKILL.md`) is the correct *shape* of a PR plan once issues 1–5 below are resolved.
- Omnigent called as peer, not dependency — accurate versus the public meta-harness.
- Goal 4 (code/memory in project git; hive holds tasks/claims/ops/lookback) is a clear data-plane split.

### What to lock before scaffolding

1. **Claim primitive:** `O_EXCL` claim files (OpenMOSS) vs JSONL-in-git. If JSONL stays: claim → commit → push → pull/re-fold → **only then mutate**.
2. **One writer-of-record per fact:** tasks, claims, orchestrator, messages-as-audit.
3. **Concurrency model:** munder single-writer paths, or document multi-push git as a known-weaker model for 2–5 agents.
4. **Helpers required** until a two-harness spike proves otherwise.
5. **Budget in v0 or not.** Empty `budget_block` is worse than cutting Goal 3.

Suggested reorder of §13: (1) decision lock on claim primitive + source of truth, (2) write fold/lease/promote + schemas, (3) spike two harnesses against that PROTOCOL, (4) scaffold templates from the PROTOCOL that survived the spike, (5) `SKILL.md` with valid frontmatter, (6) marketplace pin in parallel anytime. Do not ship a binding stub `PROTOCOL.md`.

### Issue 1 — Exclusive claims are not implementable as specified

- **Severity:** critical
- **Section:** §6 Claim schema, §7 Race rules, Goal 2
- **Description:** Pillar C and Goal 2 require workers to pick up work via exclusive claims. The spec gives two contradictory fold rules and no algorithm an implementer can code:
  - §6: “**Active set:** derived by folding `claims.jsonl` (**last wins** per `task_id` subject to lease expiry).”
  - §7.2: “two `claim` lines for same `task_id`: **earliest** valid claim by `(ts, id)` wins.”
  Last-wins and earliest-wins disagree as soon as two `claim` lines exist. File order after `pull --rebase` is not `(ts, id)` order. Unspecified: whether heartbeats are fold events or lease extensions; expiry origin (`claim.ts + lease_seconds` vs last `heartbeat.ts + lease_seconds` vs an `expires_at` field, which OpenMOSS uses and this schema lacks); whether `complete`/`release`/`reject` from a non-holder is ignored; `exclusive: false`; two claims with equal `ts`.

  Git cannot make §7.2 true before a successful push. Two harnesses can each append `action:claim`, start work, then rebase and discover the other line. Earliest-`(ts, id)` then asks the loser to `release`, but mutation of the project repo may already have happened (“claim before mutate” is local, not cluster-visible). `ts` is a wall-clock string with offsets (`2026-09-17T11:01:00+02:00`); clock skew plus ULID `id` (`clm_01J...`) means `(ts, id)` can disagree with causal order. Race rule 5 (“pull --rebase then re-fold; do not force-push”) is the recovery path, not an exclusivity primitive.

  §7 itself admits these are “**protocol** guarantees, not distributed-system proofs,” but still claims “race-safe enough for two harnesses on one hive.” Goal 2 is not met until claim success is defined as *visible in the shared hive*, not *appended in a local working tree*.
- **Suggestion:** Pick one exclusivity primitive and write a fold function in prose or pseudocode (inputs: log lines + now; output: active holder or free). Strong default, matching the cited OpenMOSS kit: claim files created with `O_CREAT|O_EXCL` (or git `add` of a create-only path that loses on merge), `expires_at` on the record, work only after the claim is on the pulled tip. If JSONL earliest-wins is kept: require claim → commit → push → pull/re-fold → **only then mutate**; loser must not have started work. Sort keys in UTC; do not use naive string compare on offset timestamps. Add tests for: two claims, heartbeat vs steal, expired lease, rebase that interleaves two appends.

### Issue 2 — “Fork OpenMOSS JSONL claims” is factually the wrong primitive

- **Severity:** major
- **Section:** §1 pillar C, §6, §0
- **Description:** Pillar C: “Fork **OpenMOSS / claude-codex-handoff** claim mechanics: **append-only JSONL claims**, claim → work → release/complete, race-safe enough for two harnesses on one hive.” Verified against OpenMOSS PROTOCOL.md (v1.12):

  | OpenMOSS actual | This spec |
  |---|---|
  | Claims are **files** `.handoff-runtime/claims/<side>-handles-<message-id>.json` created with `os.open(..., O_CREAT\|O_EXCL)` — “不得先检查再普通写入” | Claims are **lines** in `store/claims.jsonl`; earliest `(ts, id)` after the fact |
  | JSONL is the **message streams** (`claude-to-codex.jsonl` / reverse); **one writer per stream**, serialized by `locks/<side>-send.lock` | Shared multi-writer `messages.jsonl` + `claims.jsonl` |
  | Runtime is **gitignored**; protocol lives in `.handoff/` | Hive *is* git; the board is committed |
  | Lease via `expires_at`; steal of expired claim is **atomic rename** to `*.expired-<timestamp>` | `lease_seconds` with no expiry field; steal is protocol text |
  | Ids are per-side seq (`claude-000001`), `ts` is UTC `Z` | ULID-ish ids, offset timestamps |
  | Iron law: side effects first, cursor last; inbound is untrusted (§12) | No cursor model; no trust section |
  | Helpers (`send.py`, `doctor.py`, `archive.py`) are load-bearing | “helper scripts are convenience, not required for correctness” (§10) |

  Calling this a “fork” of OpenMOSS claim mechanics will cause implementers to copy JSONL logging and believe they inherited OpenMOSS’s race safety. They did not. (There is a different GitHub project named OpenMOSS that is a company-of-agents product with an API queue; the spec clearly means `claude-codex-handoff`. That part is correct.)
- **Suggestion:** Rewrite pillar C as “inspired by OpenMOSS leases, **not** a fork of the claim files.” Decision log should record: JSONL-in-git vs `O_EXCL` files vs gitignored runtime. If the intent is to stay close to OpenMOSS, adopt claim files + single-writer streams + a send helper; keep JSONL for audit only.

### Issue 3 — Munder “hive house” omits the constraints that make a file hive work

- **Severity:** major
- **Section:** §1 pillar B, §3 layout
- **Description:** Pillar B: “Munder-difflin-shaped **house**: fixed folder layout, PROTOCOL.md house rules, agents read/write the tree; git is source of truth.” Verified against munder-difflin `HIVE.md`:

  Munder’s locked decisions are: **(1) single committer** — “only the Electron main process commits. Agents never call git”; **(2) single-writer-per-file** — each agent writes only under `agents/<id>/`; a **router** moves `outbox/` → `inbox/`; **(3) one JSON file per message** via temp+rename — “never a co-edited shared mailbox file (those conflict under git)”; **(4) `board.md` has a single scribe**. Layout is `PROTOCOL.md`, `registry.json`, `board.md`, `tasks.json`, `log.jsonl`, `agents/<id>/{identity,memory,inbox,outbox,cursor}`.

  rip-swarm keeps the folder name and `PROTOCOL.md`, then does the opposite: many harnesses `git add` / `git push` the same JSONL files, a shared `inbox/`, shared `claims.jsonl`, and a co-edited `orchestrator/CURRENT`. Munder called out `.git/index.lock` corruption and JSONL merge conflicts as the reason for those constraints. §7.5’s rebase rule is an attempt to cope, not a substitute. Shared `inbox/` + shared logs is not a munder-shaped house; it is a shared-blackboard house, which munder explicitly rejected except for god-scribed `board.md`.
- **Suggestion:** Either (a) document a deliberate fork: “munder layout names, **not** munder concurrency — multi-push git is a known weaker model for 2–5 agents,” with conflict examples; or (b) adopt single-writer paths (per-agent outbox, one file per claim/task) even without an Electron router. Do not tell implementers they are building a munder house.

### Issue 4 — Four writable sources of truth, no commit order

- **Severity:** major
- **Section:** §3, §4 Promote, §5, §6, §7.4, Goal 1
- **Description:** Goal 1: promotion is “**visible in the hive**, not only in chat memory.” The spec then stores the same facts in several places without a winner except one parenthetical:

  1. **Tasks:** `inbox/` (“one file or dir per item”) **or** `type:task` message. No inbox schema, no `task_id` mapping, no rule for who deletes/renames the inbox file on claim. Two workers can both see an inbox file and both append claims (Issue 1).
  2. **Claims:** `store/claims.jsonl` vs optional `claims/<task_id>.json` mirrors. “if mirror and log disagree, **log wins**” is the only conflict rule in the spec — and it is not applied to the other pairs.
  3. **Promote:** append `type: promote` to `messages.jsonl` **and** update `orchestrator/CURRENT` **and** §7.4 “exclusive claim on pseudo-task `orchestrator`.” After two concurrent promotes, git last-push wins on `CURRENT`, earliest `(ts, id)` wins on claims, and `messages.jsonl` has both events. Goal 1 can be “visible” and **wrong**. Format is “`CURRENT.md` or `CURRENT.json`” — not chosen. Update fields listed: “agent id, harness, timestamp, reason.” Stale lease in §4 refers to “`CURRENT` heartbeat” which is not a field. Dual-orchestrator is named (“unless profile says dual-orchestrator (default: single)”) with no algorithm.
  4. **Topics:** `topics/ops|tasks|results/` vs `messages.jsonl` `topic` field vs `body` path to “large artifacts.” Dual-write again.

  Open question 2 (“mirrors mandatory or log-only”) does not block a scaffold, but it does block any agent that must know where to look. Inbox vs messages is not even listed as an open question.
- **Suggestion:** One writer-of-record per fact. Concrete v0: tasks = inbox files (or only `type:task` lines, not both); claims = log *or* `claims/<id>` create-only files, not both; orchestrator = one of `CURRENT` **or** folded `orchestrator` claims, with messages as audit. Specify a single commit that includes every dual-write, and a read rule if the commit is partial. Freeze `CURRENT.json` (not md-or-json) with `agent`, `harness`, `lease_expires_at`, `reason`, `claim_id`.

### Issue 5 — Record types are examples, not a protocol; several lifecycle fields are missing

- **Severity:** major
- **Section:** §5, §6, §8, §13.2
- **Description:** Next move 2 (“Freeze message/claim JSON schemas as JSON Schema files”) is the right instinct, but JSON Schema of the current examples would still not be implementable:

  - `type` / `topic` / `action` are shown as pipe-strings inside one JSON value (`"task|result|ops|..."`), not enums.
  - `body` is `{}` with “type-specific” and no per-type shapes. `budget_block`, `promote`, `complete` result pointer, and lookback proposals have no fields.
  - §6 lifecycle 4: “`complete` **with result pointer**” — the claim object only has `note`.
  - §6 lifecycle 5: “N distinct **review claims**” — `action` enum is `claim|heartbeat|complete|release|reject` (no `review`). Profile has both `reviews_required_per_plan` and `budget.max_reviews_per_plan` (values 1 vs 2 in different examples).
  - Inbox items have no schema at all (“one file or dir”).
  - Message `id` / claim `id` format (`msg_01J...`) is not specified (ULID? prefix rules? uniqueness domain?).
  - Profile “deep-merge (site wins)” does not define list merge (replace vs concat) for `allowed_harnesses` / `slash.enabled`. Duration types mix `30m` (`orchestrator_lease_ttl`) and integer seconds (`lease_seconds`).
  - Who may write `PROTOCOL.md`, `profiles/`, `agents/registry.yaml` is not in §4 write rules (only JSONL append-only).

  §12 open questions correctly defer marketplace pins and hive-vs-sibling; they do **not** defer fold, inbox, or promote. Those absences block the spike in next move 4, not just `SKILL.md`.
- **Suggestion:** Before scaffolding a binding `PROTOCOL.md`, publish JSON Schema **and** the missing algorithms (fold, lease, promote, profile merge). Add `review` or define reviews as ordinary claims with a `role` field. One reviews-required key. Inbox: one file per task, ULID name, required `{id, title, created_ts, created_by}`. `complete.result_ref` path. UTC `ts`.

### Issue 6 — Goal 3 (budget) is not in the claim/message model

- **Severity:** major
- **Section:** Goal 3, §4 Budget, §6, §5
- **Description:** Goal 3: “Budget / subscription awareness is **expressible in profile + claim metadata** (soft limits).” Profile YAML has `max_claims_open_per_agent`, `daily_token_soft_cap`, `spend_requires_operator`. The claim record has no token, spend, subscription, or harness-quota fields — only `lease_seconds` / `note`. There is a `budget_block` message type with empty `body`. No event for actual spend, no way to compute “open claims per agent” other than the unspecified fold, and no operator-approval object when `spend_requires_operator: true`. Soft-cap refusal (“emit a `budget_block` … instead of silent drop”) is good policy text sitting on an empty schema. Omnigent already enforces spend caps at a meta-harness layer; this spec neither reuses that nor specifies a hive-native substitute.
- **Suggestion:** Either cut Goal 3 from v0 (keep `spend_requires_operator` as “do not buy APIs / spawn cloud agents” prose in PROTOCOL) or add claim metadata (`est_tokens`, `open_claim_count_at_accept`) and a `budget_block` body `{agent, rule, limit, observed}`. Token accounting across Claude/Codex/Cursor will be approximate; say so.

### Issue 7 — Security is one line; the hive-in-git threat model is not

- **Severity:** major
- **Section:** §4.7 Secrets, §3, §7.5, §4 Roles
- **Description:** Full secrets policy: “no tokens in hive files; profiles may name *policy*, not credentials.” Missing, given v0 is **committed coordination state**:

  - **Hive git leak.** Tasks, ops messages, lookback reports, and agent ids are the product. If the hive is inside a project repo or a pushed sibling, that is customer work, PII, and planning text in git history. `.gitignore` for hive is not discussed (OpenMOSS gitignores runtime; munder’s hive is local to the harness home).
  - **Impersonation.** `from.agent` / `from.harness` / promote target are self-asserted. Any worker can append `type: promote` or `action:claim` as `agent: "orchestrator"`. “Operator designates” has no object (signed note, well-known file only the operator writes, or out-of-band).
  - **PROTOCOL.md / profiles are mutable.** An untrusted worker can edit house rules, raise `allow_preempt`, or set `allow_self_promote: true`. Write rules only protect JSONL history.
  - **History rewrite.** §7.5 says do not force-push; nothing detects or recovers from it. Rebase rewrites commit SHAs if anyone stored them.
  - **Inbound trust.** OpenMOSS PROTOCOL §12: inbound messages are untrusted requests, not commands; destructive/out-of-scope → `question`/`error`. rip-swarm has no equivalent, while `/lookback` is asked to propose PROTOCOL patches from that same traffic.

  This is worse than a missing “auth section” because the design *chooses* git as the board.
- **Suggestion:** v0 minimum: hive treated as sensitive (private remote / no public GitHub); `PROTOCOL.md` and `profiles/` operator-only (document the social rule; optional `CODEOWNERS`); agents verify `from` against `agents/registry.yaml` and refuse unknown ids; never execute `body` as a command; lookback suggestions are diffs-in-prose only (already stated — keep it). Add “do not force-push; if history diverges, stop and ask the operator.”

### Issue 8 — Alternatives table skips the real forks; Decision log does not lock them

- **Severity:** major
- **Section:** §11, §14, §10
- **Description:** §11 compares git hive vs MCP mailbox vs CF Queues vs Redis vs Omnigent. Those are real *later adapters*, and “Omnigent is a peer product… not a dependency” is correct. The trade-off table does not compare the decisions this spec actually made against the systems it claims to fork:

  - Atomic claim **files** (OpenMOSS) vs JSONL earliest-wins (here).
  - Gitignored runtime (OpenMOSS) vs committed hive (here).
  - Single-committer + single-writer paths (munder) vs multi-harness `git push`.
  - Helper-required append (OpenMOSS `send.py` + send.lock) vs “ordinary file tools” (§10).
  - Omnigent-style policy engine vs profile YAML soft caps (Goal 3).

  §14 Decision log only restates pillar locks and “queues deferred.” An engineer still has to re-litigate the claim primitive. §10’s sentence “v0 skill teaches agents to obey PROTOCOL with ordinary file tools; helper scripts are convenience, not required for correctness” is a key decision presented as packaging. OpenMOSS’s helpers exist because agents cannot atomically append+lock by convention.
- **Suggestion:** Add 4–5 decision rows (claim primitive, git vs gitignore, single vs multi committer, helpers required?, budget in hive vs harness). Keep §11 as adapter roadmap. If helpers stay optional, say they are optional *after* a spike proves two harnesses can claim without them — not before.

### Issue 9 — Next moves are in the right family but the wrong order

- **Severity:** major
- **Section:** §13, §4, §10, §12
- **Description:** Recommended next moves: (1) scaffold `templates/hive` + stub `PROTOCOL.md`, (2) freeze JSON Schema, (3) Researcher: pin skills installer, (4) spike two harnesses, claim race + promote, (5) then `SKILL.md`.

  What is right: `SKILL.md` last; marketplace pin is not on the critical path for a hive spike; two-harness demo is the correct proof of §7; spec-only this pass matches repo reality.

  What is wrong: §4 says `PROTOCOL.md` **is binding**. Copying a stub into every hive in step 1 freezes an empty contract. Step 2 freezes object shapes, not fold/lease/promote (Issues 1, 4, 5). Step 4 will fail or be inconclusive unless those algorithms exist; conversely, a spike **before** schema freeze is how you learn the schema. Step 3 can run in parallel. Missing steps: (0) lock the claim primitive vs OpenMOSS files; gitignore vs commit hive; (6) `doctor`-style read-only checker; compaction/archive for JSONL (OpenMOSS `archive.py` — unbounded `store/*.jsonl` is not mentioned).

  Pillar A “one install surface across Claude Code, Codex, Cursor” overstates skills.sh. Matt Pocock’s own install split is **plugin (Claude Code, subscribed, read-only) vs `npx skills@latest add` (editable copies)** — exclusive, not one surface. Agent Skills spec requires `SKILL.md` YAML `name` + `description`, and `name` must match the parent directory. §3 puts `SKILL.md` at repo root `rip-swarm/SKILL.md`, which matches a single-skill repo, but slash commands (`/lookback`) are Claude-shaped; Codex/Cursor invocation is not specified. Exact pins TBD is fine; pretending there is already one surface is not.
- **Suggestion:** Reorder as in “What to lock before scaffolding” above. Treat a claim helper as required until the spike says otherwise.

### Issue 10 — `/lookback` and stale orchestrator leases are not operable

- **Severity:** minor
- **Section:** §4 Promote (stale lease), §9, pillar E, Goal 1
- **Description:** `/lookback` reads JSONL + PROTOCOL, writes `lookback/YYYY-MM-DD-*.md`, does not auto-merge — good. Missing: report schema (the spec wants “actionable diffs-in-prose” but not a required heading list); what “coordination failure modes” to scan (double claim, expired lease, split CURRENT, rebase conflict, budget_block storms); clock used for “now”; whether it folds claims or greps. Open question 5 (PR vs markdown) is correctly deferred.

  Stale orchestrator: “if `CURRENT` heartbeat older than `profile.orchestrator_lease_ttl`, a worker may propose reclaim via ops message; reclaim still follows race rules (claim the promote slot).” No heartbeat writer, interval, or field; two workers can both propose reclaim (Issue 1 on `task_id=orchestrator`); split-brain if `CURRENT` is not folded from the log. No `doctor` equivalent. No compaction. `/status` is “optional later” in pillar E and specified in §9 / `slash.enabled` — pick one for v0.
- **Suggestion:** v0 lookback template: double-claims, expired leases, CURRENT vs last promote event, inbox items with no claim, JSONL parse errors. Heartbeat: last `CURRENT` write *or* a `type: ops` `heartbeat` from the holder; reclaim only when `now > lease_expires_at` on the folded orchestrator claim. Add a read-only `scripts/status` for humans. Drop `/promote` from v0 if chat+ops is enough (already hedged in §9).

### Issue 11 — Profile merge, hive location, and agent identity are under-specified

- **Severity:** minor
- **Section:** §3, §8, §12.1, §12.4
- **Description:** Resolution order “explicit profile name in message / CLI → env `RIP_SWARM_PROFILE` → `default`” is clear. Missing: env/CLI for **hive path** (“operator can point any installed harness at a hive path” — Goal 1) e.g. `RIP_SWARM_HIVE`. Open question 1 (in-repo vs sibling `hive/`) does not block templates but does block SKILL.md init. `init` “does not overwrite existing `profiles/`” — good; silent about overwriting `PROTOCOL.md` / `store/`. `agents/registry.yaml` has no schema; Grok Bot mapping is correctly optional. `allow_self_promote: false` vs “any agent the operator designates may become orchestrator **if** profile allows self-promote / operator-promote” — operator-promote path is unnamed (file? message `from: operator`?).
- **Suggestion:** `RIP_SWARM_HIVE` + profile env as documented. Init: refuse to clobber `PROTOCOL.md` unless `--force`. Registry minimum: `{id, harness, role}`. Operator promote: a promote message whose `from.agent` is in an `operators:` list in the profile.

### Issue 12 — Skill packaging claims are directionally right, locally imprecise

- **Severity:** minor
- **Section:** §1 pillar A, §3 skill package, §10
- **Description:** SKILL.md + `npx skills` / skills.sh-class install is a real ecosystem (Anthropic Agent Skills; `npx skills add owner/repo`). “Matt-Pocock-style” is fair as a *product* analogy (install skill, configure site via a setup flow, do not fork the skill — pillar D). Imprecise: Pocock’s engineering setup is a **prompt-driven** `/setup-matt-pocock-skills`, not a hive `init` script; plugin vs skills.sh exclusivity (Issue 9); this repo does not yet contain `SKILL.md` (status line is honest). Slash-command surface is harness-specific; a portable skill should trigger on description keywords (`lookback`, `evaluate`, hive, claims) as well as `/lookback`.
- **Suggestion:** Name the install target “Agent Skills SKILL.md, installable via skills.sh-class CLIs” without implying one marketplace pin. Put `name: rip-swarm` in a `rip-swarm/` directory (or repo root with matching folder name after install). Document `/lookback` as Claude Code and “ask the agent to run lookback” elsewhere.

### Issue 13 — Repo root path in the spec is wrong

- **Severity:** nit
- **Section:** header
- **Description:** Spec: “**Repo root:** `/home/box/src/rip-swarm`.” Actual workspace is this repository’s root (git `init` commit, working tree as of the review). Harmless for readers of a published repo; confusing for anyone following the spec as a runbook.
- **Suggestion:** Drop the absolute path or say “this repository’s root.”

### Issue 14 — Illustrative JSON will be copy-pasted as schema

- **Severity:** nit
- **Section:** §5, §6
- **Description:** `"type": "task|result|ops|promote|budget_block|note"` and `"to": "orchestrator|*|agent-id"` look like instance data. Combined with empty `"body": { }` they will leak into fixtures.
- **Suggestion:** Use one concrete example (`"type": "ops"`) plus a bullet enum, or wait for the JSON Schema files in next move 2.
