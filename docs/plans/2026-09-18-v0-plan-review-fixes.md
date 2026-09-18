# rip-swarm — v0 plan review fixes (2026-09-18)

**Source:** `docs/plans/2026-09-18-v0-plan-review.md` (findings C1, m1–m4, n1).
**Spec:** `docs/specs/2026-09-17-design-spec.md` (v0.2).

## Global Constraints

- Python 3.10+, stdlib only. Tests: `PYTHONPATH=. python3 -m unittest discover -s tests` (278 pass at start).
- Follow TDD: write the failing test first, then the fix.
- Publish loop (spec §8.5): commit only the paths the op wrote, allow-listed per operation. Never widen an allowlist beyond what the op writes.
- Registry gate (spec §5): every mutating lifecycle helper calls `require_agent` before touching files.
- Do not force-push the hive. Do not touch `docs/specs/` except where a task says so.
- Each task is one commit (or a few), message ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

### Task 1: Operator-driven promote publishes (C1)

**Bug:** `orchestrator.promote` writes the `type:promote` message to the **`by`** agent's outbox (`rip_swarm/orchestrator.py` `write_message(hive, agent=by, ...)`), but the promote allowlist is `gitops.default_allow("orchestrator", agent)`, which only permits `agents/<agent>/outbox/*.json`. With `by != agent` (the documented `promote --agent A --by OPERATOR` form) `_commit_op` raises `op wrote paths outside its allowlist: agents/<by>/outbox/...`, the tree is reset and nothing is pushed. Existing promote-by-operator tests all use `--local`, which bypasses publish.

**Fix:**
1. In `rip_swarm/cli.py` `_promote`: compute `by = args.by or agent` and pass `allow=default_allow("orchestrator", agent) + [f"agents/{_glob_quote(by)}/outbox/*.json"]` to `_run_op` (import `default_allow` / `_glob_quote` from gitops, or add a small `promote_allow(agent, by)` helper in `gitops.py` and use it from both places — prefer the helper).
2. In `rip_swarm/gitops.py` `promote_and_publish`: same allow set.
3. Do not add the `by` outbox when `by == agent` twice (harmless duplicate is fine, but keep the helper tidy).

**Tests (TDD, in `tests/test_gitops.py` using the existing two-clone fixture, and one CLI test in `tests/test_cli.py` using `_hive_with_upstream`):**
- Register an operator id (e.g. `op`, harness `human`, role `operator`) in the seeded registry for that test; set `profiles/default.yaml` `operators: [op]` on the hive and push it.
- `promote_and_publish(hive, agent="alice", harness="claude-code", now=T0, lease_seconds=1800, reason="designated", allow_self_promote=False, operators=["op"], by="op")` succeeds; remote `origin/swarm` tree contains `claims/orchestrator.json`, `orchestrator/CURRENT.json`, `agents/op/outbox/<msg>.json`, and `store/messages.jsonl` has the promote line; hive tree clean afterwards.
- CLI: `main(["promote","--hive",hive,"--agent","alice","--by","op","--reason","designated"])` returns 0 on a publishable hive (no `--local`).
- Confirm the new tests fail on the current code with the allowlist error before the fix.

### Task 2: Flag release/reject crash windows as corrupt (m1)

**Bug:** `rip_swarm/fold.py` `has_complete_tombstone` only detects `<task>.complete.*.json` beside an active claim. `_finalize` is shared by complete, release and reject, so a crash between tombstone write and active unlink for release/reject leaves both files and `status` lists the task as held.

**Fix:** generalise to `has_final_tombstone(claims_dir, task_id)` matching `<task>.(complete|release|reject).*.json`; rename `COMPLETE_COEXIST_ERROR` to a message naming the action found, e.g. `active claim and <action> tombstone coexist`. Keep the `expired` tombstone out of it (an expired tombstone beside a fresh claim is the normal steal path). Update callers and any test referencing the old names.

**Tests (TDD, `tests/test_fold.py` and/or `tests/test_status.py`):** plant an active claim plus a `release` tombstone → `active_holder` returns `Corrupt` with the release message, `status_report["active_claims"]` empty, `corrupt_claims` lists it. Same for `reject`. Existing `complete` test still passes; an `expired` tombstone beside an active claim is still a `Holder`.

### Task 3: Message CLI type docs and guard (m2)

**Bug:** `SKILL.md` says `message.py --type` accepts `task|result|ops|promote|budget_block|note|heartbeat`, but `outbox._check_body` demands structured bodies for `promote` and `budget_block`, and the CLI always sends `{"text": ...}`; those two exit 1 with a body-shape error.

**Fix:**
1. In `rip_swarm/cli.py` `_message`: refuse `--type promote` and `--type budget_block` up front with `ValueError("--type promote/budget_block are written by the promote and claim helpers, not by message")` (exit 1, before any hive write).
2. `SKILL.md` "Message another agent": list only `task|result|ops|note|heartbeat` as `--type` values and add one sentence that `promote` and `budget_block` are emitted by the helpers. Mirror the same list in `README.md` if it lists the types.

**Tests (TDD, `tests/test_message.py`):** `--type promote` and `--type budget_block` return 1 with the new message and leave the outbox/messages.jsonl untouched; `--type note` still succeeds.

### Task 4: Registry gate on expired-baton release, and trim claim allowlist (m4, n1)

**m4:** `rip_swarm/orchestrator.py` `release_orchestrator` expired branch (`_release_expired`) skips `require_agent`; every other lifecycle path gates. Add `require_agent(hive, agent)` at the top of `release_orchestrator` (before reading the claim). Test in `tests/test_orchestrator.py`: unregistered agent releasing an expired baton it "holds" (plant the claim) raises `UnknownAgent`, and the claim/CURRENT files are untouched.

**n1:** `rip_swarm/gitops.py` `default_allow` includes `inbox/<task>.json` although no claim-lifecycle op writes it. Remove that pattern. Run the full suite; if any test relied on it, report which rather than re-adding it.

### Task 5: Retry instead of deny when an expired foreign claim lands mid-publish (m3)

**Behaviour today:** `gitops._push_with_retries`: push rejected → fetch → remote claim by another agent but *expired* passes `_unexpired_held_by_other` → `rebase @{u}` conflicts on `claims/<task>.json` → `_rebase_conflicts_claim` → `ClaimDenied("lost race on remote tip")`. The claim is actually stealable; a re-run steals it.

**Fix (minimal):** when the rebase conflict is on the claim file **and** the remote claim is expired (reuse `_read_remote_claim` result already in hand: dict, holder != ours, `expires_at <= now`), raise `ClaimDenied("expired claim reached the remote tip first; retry to steal it")` instead of the generic lost-race text. Do not auto-retry the op inside publish (the op closure has already run and the tree is reset; re-running it is a larger change). Keep the unexpired path's message unchanged.

**Tests (TDD, `tests/test_gitops.py` two-clone fixture):** clone B publishes a claim on task T with a short lease that is already expired relative to clone A's `now`; clone A, whose local tip predates B's push, runs `claim_and_publish` with `now` past B's expiry → `ClaimDenied` whose message contains `retry`; A's tree is clean and at `@{u}`; a second `claim_and_publish` by A then succeeds and the remote claim is A's.
