# rip-swarm — implementation review (2026-09-17)

**Status:** advise-only. Does not modify implementation.  
**Reviewed tip:** `be0a66d` (`Fix defects from implementation review; 93 -> 231 tests.`) after `git pull --ff-only origin`.  
**Compared to:** `docs/specs/2026-09-17-design-spec.md` (v0.2).  
**Scope:** `rip_swarm/`, `scripts/`, `templates/_swarm/`, `tests/`, `docs/specs/schema/`, `SKILL.md`, `README.md`.  
**Constraint honored:** no implementation files changed in this review pass.

---

## Architecture picture (concise)

```
project repo
  main / feature branches     ← code only; `_swarm/` gitignored
  origin/swarm (orphan)       ← hive history on project remote
       ↓ clone --single-branch
  ./_swarm/                   ← nested hive work-tree root
       inbox/<task_id>.json          SoT: tasks
       claims/<task_id>.json         SoT: exclusivity (O_CREAT|O_EXCL; first push wins)
       claims/orchestrator.json      SoT: baton
       orchestrator/CURRENT.json     mirror (must match claim)
       store/{claims,messages}.jsonl audit only (merge=union)
       agents/<id>/outbox/           per-agent message writes
       profiles/ + PROTOCOL.md       operator-owned policy

CLI / scripts  →  policy + claim/promote/…  →  gitops.publish (fetch → tip check → op → commit → push≤5)
Fold /status /lookback read claim files for locks; JSONL never decides exclusivity.
```

Pillars A–E from the spec are present in code: Agent Skills packaging (`SKILL.md` + `scripts/` `sys.path` shim), munder-shaped layout with multi-push concurrency, create-only claims, profile deep-merge + defaults, `/status` + `/lookback` (no `/promote` slash).

---

## Verdict

**Acceptable for v0 with revisions.** The locked primitives are implemented and spike-tested (two-clone first-push-wins, union-merge audit, nested hive isolation, budget_block publish-then-deny, baton exclude-from-cap, CURRENT repair). Prior review defects clearly landed in `be0a66d`. Remaining issues are helper-boundary and publish staging correctness, plus doc/schema drift — not a redesign of the claim model.

---

## Strengths

1. **Exclusivity model matches §7–§8:** claim files are SoT; fold (`rip_swarm/fold.py`) ignores JSONL; `excl_create_json` uses `O_CREAT|O_EXCL`; remote tip + push-retry loop in `publish`.
2. **Nested `swarm` checkout isolation works:** `assert_hive_repo`, GIT_* unset, dirty code tree neither blocks nor is reset (`tests/test_gitops.py`).
3. **Publish recovery is careful about unpushed commits:** refuses `reset --hard` when `HEAD` is ahead of `@{u}` (`gitops.publish` docstring + tests around unpushed survival).
4. **Budget + baton interactions are correct on the CLI path:** orchestrator refused via `claim`; baton excluded from `open_claim_count`; `ClaimDenied(commit=True)` publishes `budget_block` then denies.
5. **Promote / release / heartbeat keep CURRENT as mirror:** reason stored on claim `note`; heartbeat repairs CURRENT from SoT; release deletes CURRENT in the same op.
6. **Packaging matches §11:** thin `scripts/*.py`, `SKILL.md` documents `$SKILL_DIR`, template PROTOCOL is non-empty and spike-aligned, schemas frozen under `docs/specs/schema/`.
7. **Test depth:** **231 tests, all OK** (`python3 -m unittest discover -s tests`).

---

## Findings

### Critical

*(none)*

### Major

#### M1. `publish` commits every untracked path, not “only the paths written”

- **Evidence:** `rip_swarm/gitops.py` `_commit_op` L248–253 runs `git add -u` then stages **all** untracked paths. Spec §8.5: “commit only the paths written”.
- **Verification (adversarial):** a `publish` op that called `try_claim` and also wrote `UNRELATED_LEAK.txt` pushed that file to `origin/swarm` (`REMOTE_HAS_UNRELATED True`).
- **Impact / blast radius:** any scratch, secret, or accidental file left in the hive during an op is permanently appended to hive history; protocol forbids force-push, so cleanup is painful. Normal CLI ops today only write intended paths, but `publish` is a generic helper and the contract is violated.
- **Recommended fix:** capture the clean-tree baseline (or have `op` return `paths_written`), stage only that set; refuse if unexpected dirty paths remain.

#### M2. Registry / budget enforcement is CLI-claim-only; lifecycle helpers and `claim_and_publish` bypass it

- **Evidence:**
  - `claim_and_publish` → raw `try_claim` (`gitops.py` L392–412): no `require_agent`, no `max_claims_open_per_agent`.
  - `try_claim` itself (`claim.py` L103–144) does not consult the registry.
  - CLI `_complete` / `_release` / `_reject` / non-orchestrator `_heartbeat` (`cli.py` L249–307, L221–246) never call `require_agent`.
  - Contrast: `try_claim_with_policy` (`policy.py` L27) and `write_message` / `promote` do enforce registry.
- **Verification:** `try_claim(..., agent="ghost", ...)` succeeded with an empty registry; `try_claim_with_policy` raised `UnknownAgent`.
- **Impact / blast radius:** Spec §5 trust (“`from.agent` must match `agents/registry.yaml`; unknown ids → refuse”) and §5 budget are not true of the library/helper surface used by tests and any harness that imports `claim_and_publish` or completes via CLI after a planted claim. Unregistered ids can own the full claim lifecycle.
- **Recommended fix:** route `claim_and_publish` through `try_claim_with_policy` (needs a profile); call `require_agent` at the start of heartbeat/complete/release/reject (and orchestrator variants); consider moving registry check into `try_claim` itself.

#### M3. `try_claim("orchestrator")` can create baton SoT without CURRENT

- **Evidence:** `try_claim` explicitly allows `task_id == "orchestrator"` without inbox (`claim.py` L114–115). Policy/CLI `claim` refuse it (`policy.py` L25–26), but the primitive and `claim_and_publish` do not write `CURRENT.json`. Probe: claim-only → `orchestrator_state` `{matches_claim: False, present: False}`.
- **Impact / blast radius:** Spec §4 requires baton = claim + CURRENT mirror in one promote commit; partial states are a doctor failure. Any caller that uses `try_claim`/`claim_and_publish` for the baton (including mistaken harness code) creates a standing mismatch until promote/repair.
- **Recommended fix:** refuse `orchestrator` inside `try_claim` (promote-only), or make `claim_and_publish` reject that id and keep `promote` as the sole entry.

### Minor

#### m1. Promote/claim trust CLI `--harness` over registry

- **Evidence:** `promote` (`orchestrator.py` L97–103) writes CURRENT/`body.harness` from the argument; only `from.harness` on the audit message uses `by_rec["harness"]`. Probe: `harness="totally-wrong"` accepted while registry says `claude-code`.
- **Impact:** misleading doctor/lookback and audit; no exclusivity break.
- **Fix:** require CLI harness to equal `require_agent(... )["harness"]`, or drop `--harness` and always take registry.

#### m2. Tombstone crash window: active claim + complete tombstone can coexist; status tells both stories

- **Evidence:** `_finalize` (`claim.py` L74–81) writes the tombstone then `unlink`s the active path; comment claims “never both”, but a kill between those steps leaves both files. `status_report` lists the active holder (`active_claims`) while `_completed_task_ids` (`status.py` L122–128) also treats the task as settled for `inbox_without_claim`.
- **Verification:** planted both files → `active_claims` non-empty and `inbox_without_claim` empty.
- **Impact:** confusing doctor output after a crash; publish usually finishes the op before commit, so the window is local dirty-tree unless the operator force-commits.
- **Fix:** teach status/lookback to flag `active + matching complete tombstone` as corrupt/partial; optionally finalize via rename-only without a dual-visible window (harder portably).

#### m3. Spec / checkout note still describe pre-implementation or pre-option-C state

- **Evidence:** design spec L3 still ends with “Not implemented.” `docs/specs/2026-09-17-hive-checkout.md` L8 still says checkout is a “linked git worktree” even though the note’s status line and §13 lock option C (nested clone).
- **Impact:** operators reading status lines get false signals; implementation and README are correct.
- **Fix:** update spec status to “v0 implemented”; fix hive-checkout intro to match option C.

#### m4. `message.schema.json` omits `ref` while runtime always emits it

- **Evidence:** schema required list L6 has no `ref`; `outbox.write_message` always sets `ref`. Spec §6 example includes `ref`.
- **Impact:** schema-as-contract drift only (runtime not schema-validated today).
- **Fix:** add `ref` to properties/required (or document it as optional with default nulls).

#### m5. Claim path accepts non-ULID task ids if an inbox file is planted

- **Evidence:** `inbox.validate_task_id` requires `task_<ULID>`; `try_claim` only rejects `/`, `\`, `..`, and leading `.`. Probe: planted `inbox/weird-task.json` → claim succeeded.
- **Impact:** low if helpers are the only writers; raw/planted files can create odd lock names.
- **Fix:** share inbox id rules (plus `orchestrator` exception) inside `try_claim`.

### Nit

#### n1. Undocumented `--local` on a publishable hive

- **Evidence:** `cli.py` L57, L112–113, L404–411 warn on stderr but `SKILL.md` / PROTOCOL never mention `--local`.
- **Impact:** leaves a dirty hive that blocks later publish.
- **Fix:** document as test-only, or refuse `--local` when upstream exists unless `RIP_SWARM_ALLOW_LOCAL=1`.

#### n2. Lookback heading “CURRENT vs last promote” compares CURRENT vs claim file

- **Evidence:** `lookback._mismatch_bullets` uses `status_report["current_mismatch"]` / `orchestrator_state`, not the last `type:promote` message. Spec §10 heading text vs §4 SoT rule (claim wins) — behavior is SoT-correct; heading is slightly misleading.
- **Fix:** rename heading to “CURRENT vs orchestrator claim”.

---

## Spec drift summary

| Spec lock | Implementation | Notes |
|-----------|----------------|-------|
| Create-only claim SoT; JSONL audit | Yes | fold ignores JSONL |
| Inbox SoT; helpers require inbox (except orchestrator) | Yes on try_claim | |
| Baton = claim + CURRENT same commit | Yes | M3 fixed: baton only via `claim_baton` (promote-only) |
| Publish: clean → fetch → tip check → op → commit paths → push ≤5 | Yes | M1 fixed: staging is allow-listed per op |
| `merge=union` on `store/*.jsonl` | Yes | template + two-clone test |
| Registry refuse unknown | Yes | M2 fixed: gate moved to the claim primitive |
| Budget cap + budget_block | Yes | M2 fixed: enforced at the primitive, not only the CLI |
| `spend_requires_operator` prose only | Yes | correctly not coded as a meter |
| `/status` + `/lookback`; no `/promote` slash | Yes | |
| Agent Skills + `$SKILL_DIR` scripts | Yes | |
| Spec header status line | Fixed | now records v0 implemented at `be0a66d` |
| Hive-checkout intro | Fixed | states option C (nested clone); note kept as decision record |

---

## Verification evidence

```text
# environment
$ git -C <reviewer's checkout> status
On branch master
Your branch is up to date with 'origin/master'.
nothing to commit, working tree clean

$ git pull --ff-only origin
Updating f702fbb..be0a66d
Fast-forward
# tip now be0a66d

# unit + integration suite
$ python3 -m unittest discover -s tests
Ran 231 tests in ~6.4s
OK

# adversarial probes (temp dirs only; no repo mutation)
- try_claim without registry → allowed (agent=ghost)
- try_claim_with_policy without registry → UnknownAgent
- publish op writing UNRELATED_LEAK.txt → file present on origin/swarm tip
- try_claim(orchestrator) without CURRENT → matches_claim False
- promote harness mismatch vs registry → accepted
- mid-_finalize both active+complete → active_claims set, inbox_without_claim empty
- path-traversal task ids → ClaimDenied
```

Two-clone race / union-merge / dirty-code isolation covered by existing `tests/test_gitops.py` (executed in the 231).

---

## Prioritized next moves

1. **Fix `_commit_op` path staging (M1)** — highest leverage correctness/safety fix; add a regression test that fails if an unrelated untracked file is committed.
2. **Unify trust gates (M2)** — `require_agent` + budget on every mutating helper path; stop using raw `try_claim` in `claim_and_publish`.
3. **Close orchestrator partial path (M3)** — promote-only acquisition of the baton at the primitive layer.
4. **Doctor the crash window (m2)** and align harness with registry (m1).
5. **Doc/schema hygiene (m3, m4, n1, n2)** — cheap, prevents operator confusion.
6. Parallel: marketplace pin (spec §16 Q3) remains off the implementation critical path.

---

## Reviewer note

This review intentionally does **not** change implementation. Prior tip `be0a66d` already absorbed an earlier implementation-review fix pass (93 → 231 tests); findings above are residual relative to that tip and the v0.2 spec.

---

## Dispositions (2026-09-17)

Every finding above was addressed in the pass that followed this review (implementation by the gitops/CLI and claim/status agents; docs, spec and schema by this pass). Nothing is deferred to a later version.

| Finding | Disposition |
|---------|-------------|
| M1. `publish` commits every untracked path | **Fixed (this pass)** — publish stages only the allow-listed paths each operation writes; spec §8.5 reworded to "commit only the paths written (allow-listed per operation)". |
| M2. Registry / budget enforcement is CLI-claim-only | **Fixed (this pass)** — the registry + harness gate moved to the claim primitive, so every lifecycle path is covered; spec §5 trust updated. |
| M3. `try_claim("orchestrator")` can create baton SoT without CURRENT | **Fixed (this pass)** — the baton is acquired only through `claim_baton` (promote-only); `try_claim` refuses the reserved id. |
| m1. Promote/claim trust CLI `--harness` over registry | **Fixed (this pass)** — `--harness` is optional and defaults to the registry value; a supplied mismatch exits 2. Spec §5/§11 and SKILL.md updated. |
| m2. Tombstone crash window: active claim + complete tombstone coexist | **Fixed (this pass)** — status flags an active claim with a matching complete tombstone as corrupt instead of telling both stories. |
| m3. Spec / checkout note describe pre-implementation or pre-option-C state | **Fixed (this pass)** — spec status line now records v0 implemented at `be0a66d` (231 tests) and points here; `docs/specs/2026-09-17-hive-checkout.md` states option C is locked and keeps the note as the decision record. |
| m4. `message.schema.json` omits `ref` | **Fixed (this pass)** — `ref` added to properties and required (nullable `claim_id` / `task_id` / `in_reply_to`); `claim.schema.json` also gained the tombstone-only `result_ref`. All seven schemas re-checked against runtime output. |
| m5. Claim path accepts non-ULID task ids if an inbox file is planted | **Fixed (this pass)** — the claim primitive shares the inbox id rules, with the `orchestrator` exception. |
| n1. Undocumented `--local` on a publishable hive | **Fixed (this pass)** — helpers refuse `--local` on a hive with an upstream unless `RIP_SWARM_ALLOW_LOCAL=1`; documented in SKILL.md and spec §11. |
| n2. Lookback heading "CURRENT vs last promote" | **Fixed (this pass)** — heading renamed to "CURRENT vs orchestrator claim" in `rip_swarm/lookback.py`, its test, spec §10 and the plan's Task 13. |

Decision-log row for this pass: spec §13, dated 2026-09-17.
