# rip-swarm — v0 plan completion review (2026-09-18)

**Status:** advise-only at time of writing; findings are fixed in the pass that follows (see Dispositions).
**Reviewed tip:** `baeaea1` (`Merge branch 'feat/message-surface'`) on `master`.
**Compared to:** `docs/plans/2026-09-17-rip-swarm.md` (v0 implementation plan, Tasks 1–14) and `docs/specs/2026-09-17-design-spec.md` (v0.2).
**Scope:** `rip_swarm/`, `scripts/`, `templates/_swarm/`, `tests/`, `docs/specs/schema/`, `SKILL.md`, `README.md`.

---

## Is the plan implemented?

**Yes.** Every plan task has its module, script, template, schema and test file on `master`:

| Task | Deliverable | Present |
|------|-------------|---------|
| 1 | `ids.py`, `timeutil.py` | yes |
| 2 | `io.py`, `paths.py` | yes |
| 3 | `inbox.py` | yes |
| 4 | `claim.py` (lifecycle + tombstones) | yes |
| 5 | `fold.py`, `audit.py` | yes |
| 6 | `simpleyaml.py`, `registry.py` | yes |
| 7 | `outbox.py` | yes |
| 8 | `orchestrator.py` | yes |
| 9 | `profile.py`, `policy.py` | yes |
| 10 | `status.py` | yes |
| 11 | `gitops.py` + two-clone spike tests | yes |
| 12 | `PROTOCOL.md`, 7 schemas, template, `init_hive.py` | yes |
| 13 | `lookback.py` | yes |
| 14 | `cli.py`, `scripts/*.py`, `SKILL.md`, `README.md` | yes |
| — | message surface (`scripts/message.py`, `cli message`) | yes (post-plan) |

Suite: **278 tests, OK** (`PYTHONPATH=. python3 -m unittest discover -s tests`).
The plan's `- [ ]` checkboxes were never ticked; that is tracking hygiene only.

---

## Verdict

**One blocker for the two-harness trial; otherwise acceptable.** The documented operator-driven promote path fails on every publishable hive. Everything else is minor.

---

## Findings

### Critical

#### C1. Operator-driven promote cannot publish

- **Evidence:** `orchestrator.promote` writes the `type:promote` audit message from the **`by`** agent's outbox (`rip_swarm/orchestrator.py` L115 `agent=by`). The publish allowlist for promote is `default_allow("orchestrator", agent)` (`rip_swarm/cli.py` L428; `gitops.promote_and_publish`), which permits only `agents/<agent>/outbox/*.json` (`rip_swarm/gitops.py` L268). When `by != agent` — the README/SKILL documented form `promote --agent A --by OPERATOR` — `_commit_op` refuses.
- **Reproduction (bare remote, registry alice/op/bob, `operators: [op]`):**
  ```
  $ promote --hive _swarm --agent alice --by op --reason designated
  exit 1: op wrote paths outside its allowlist: agents/op/outbox/msg_….json
  git status --porcelain: ''            # tree reset
  origin/swarm:claims/orchestrator.json  # absent
  ```
  Self-promote (`by == agent`, `allow_self_promote: true`) publishes fine.
- **Why tests miss it:** every promote-by-operator test in `tests/test_cli.py` runs with `--local`, which bypasses `publish` and its allowlist.
- **Impact:** the only non-self way to seat an orchestrator does not work over git. Blocks the trial for any project that uses an orchestrator.
- **Fix:** include `agents/<by>/outbox/*.json` in the promote allow set (CLI `_promote` and `promote_and_publish`); add a publishing regression test with `by != agent`.

### Major

*(none)*

### Minor

#### m1. Release / reject crash windows are not flagged as corrupt

- **Evidence:** `fold.has_complete_tombstone` (`rip_swarm/fold.py` L63) only checks `<task>.complete.*.json`. `_finalize` is shared by complete, release and reject; a crash between tombstone write and active unlink for release/reject leaves both files and `status` reports the task as held.
- **Reproduction:** active claim + planted `<task>.release.<stamp>.json` → `active_claims` lists it, `corrupt_claims` empty.
- **Fix:** extend the coexistence check to `complete|release|reject` tombstones.

#### m2. `SKILL.md` lists message types the message CLI refuses

- **Evidence:** `SKILL.md` L55 says `--type` is one of `task|result|ops|promote|budget_block|note|heartbeat`; `outbox._check_body` requires structured bodies for `promote` / `budget_block`, and the CLI always sends `{"text": …}`. `message --type promote` exits 1: `promote body must be {agent, harness, by, reason, claim_id}`.
- **Fix:** document `promote` / `budget_block` as helper-only (or refuse them in `cli._message` with a clear message).

#### m3. Expired foreign claim reaching the remote mid-publish reports "lost race"

- **Evidence:** `_push_with_retries` checks `_unexpired_held_by_other`; an *expired* foreign claim passes that check, then `rebase` hits add/add on `claims/<task>.json` and `_rebase_conflicts_claim` converts it to `ClaimDenied("lost race")`. The claim is actually stealable; a re-run steals it.
- **Impact:** UX only; no exclusivity break.
- **Fix (optional):** on that path, retry the op after `_reset_upstream` instead of denying, or word the denial as "retry".

#### m4. Expired-baton release skips the registry gate

- **Evidence:** `orchestrator.release_orchestrator` expired branch → `_release_expired` never calls `require_agent`; every other lifecycle path does (`claim._require_holder`).
- **Impact:** low (promote already gated the holder); inconsistency only.
- **Fix:** call `require_agent(hive, agent)` at the top of `release_orchestrator`.

### Nit

#### n1. Claim allowlist over-permits `inbox/<task>.json`

- **Evidence:** `gitops.default_allow` includes `inbox/{tid}.json` although no claim-lifecycle op writes it.
- **Fix:** drop it from `default_allow` (inbox-add already passes its own allow).

---

## Strengths

1. Publish loop refuses to `reset --hard` over unpushed commits, stages only the op's write set, and converts claim-file rebase conflicts into `ClaimDenied`.
2. Trust gates (registry + harness) live in the primitives (`claim._require_registered`, `outbox.write_message`), so library callers get the CLI's guarantees.
3. Two-clone race, union-merged JSONL, nested-clone isolation and unreadable-remote-claim fail-closed are covered by real git tests.
4. Corrupt claim files and complete-tombstone coexistence are surfaced by `status` rather than silently folded.

---

## Verification evidence

```text
$ git log -1 --oneline
baeaea1 Merge branch 'feat/message-surface'
$ PYTHONPATH=. python3 -m unittest discover -s tests
Ran 278 tests in 29.300s
OK
# probes (temp dirs only)
- promote --by op (op != agent) over bare remote → exit 1 allowlist; remote unchanged   ← C1
- promote self (allow_self_promote) over bare remote → exit 0
- message --type promote → exit 1 body shape                                           ← m2
- active claim + release tombstone → status active_claims lists it, corrupt empty      ← m1
```

---

## Dispositions

Filled in by the fix pass that follows this review.
