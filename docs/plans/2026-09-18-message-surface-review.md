# rip-swarm — message-surface review (2026-09-18)

**Status:** advise-only. Does not modify implementation.  
**Reviewed tip:** `d215dab` (`feat: open-ended message CLI (no claim; allow-listed publish).`) on `feat/message-surface`.  
**Compared to:** Architect brief for open-ended messages + `docs/specs/2026-09-17-design-spec.md` §5–§6 / §14.  
**Scope:** `scripts/message.py`, `rip_swarm/cli.py` (`message`), `rip_swarm/outbox.py`, publish allow patterns for message, `tests/test_message.py`, `SKILL.md`, `README.md`, design-spec §14.  
**Constraint honored:** no implementation files changed in this review pass.

---

## Architecture picture (message path)

```
operator / harness
  scripts/message.py          # thin sys.path shim → cli.main(["message", …])
  python -m rip_swarm message # __main__ → same cli.main
       ↓
  cli._message
    --from / --to / --type / --body   (+ optional --harness via common parent)
    _resolve_harness(hive, from)      # registry SoT; mismatch → ClaimDenied (exit 2)
    body := {"text": <cli --body>}    # untrusted request shape
       ↓
  publish(allow=[agents/<from>/outbox/*.json, store/messages.jsonl])
       ↓ op
  outbox.write_message
    require_agent(from)               # UnknownAgent → refuse before write
    _check_to: to ∈ registry ∪ {orchestrator, *}
    excl_create agents/<from>/outbox/<msg_id>.json
    append store/messages.jsonl
       ↓
  gitops._commit_op stages only allow-matched changed paths; else GitopsError + reset
```

No claim SoT is read or written. Recipient outbox is never touched (no shared mailbox). Inbox/claim remain the exclusive-work path.

---

## Verdict

**Accept-with-revisions.** Every Architect brief item passes with file/line evidence. The CLI surface, trust gates on `from`/`to`, no-claim / no-mailbox contracts, allow-listed publish, docs/§14, and the six new tests are in place. Full suite is **275 OK**. Revisions are minor: `write_message` still does not enforce harness↔registry match (CLI does), and the README Helpers table omits `scripts/message.py`. Neither blocks a two-harness trial of the message path.

---

## Brief checklist (pass/fail + evidence)

| # | Brief item | Result | Evidence |
|---|------------|--------|----------|
| 1 | `scripts/message.py` + `python -m rip_swarm message --from/--to/--type/--body` | **PASS** | `scripts/message.py` L1–7 shim → `main(["message", …])`. Flags at `cli.py` L101–109 (`--from`→`from_agent`, `--to`, `--type`, `--body`, all required). `__main__.py` → `cli.main`. |
| 2 | Trust: `require_agent(from)`; `to ∈ registry ∪ {orchestrator,*}`; no claim; no shared mailbox | **PASS** | Primitive: `outbox.py` L42 `require_agent(hive, agent)`; L16 `_TO_LITERALS`; L68–74 `_check_to`. CLI docstring L208–212. Tests: `test_unregistered_from_refused`, `test_unknown_to_refused`, `test_a_to_b_without_a_claim` (claims empty; bob outbox absent), `test_a_to_star_without_a_claim`. Probe: ghost→`UnknownAgent`; `nobody`→`ValueError`; bob outbox never created. |
| 3 | Publish allow-list: `agents/<from>/outbox/<id>.json` + `store/messages.jsonl` ONLY | **PASS** | `cli.py` L241–244 explicit `allow=[f"agents/{agent}/outbox/*.json", "store/messages.jsonl"]` (replaces `default_allow`, `gitops.py` L410). Adversarial: `test_publish_does_not_drag_unrelated_paths` — mid-op `UNRELATED_LEAK.txt` → `GitopsError`, remote ls-tree has no leak, tree reset clean. |
| 4 | `SKILL.md` + `README`; design-spec §14 marked done | **PASS** | `SKILL.md` L46–65 “Message another agent” + when-to-use table. `README.md` L7 status line + L93–100 example. Spec §14 item 4 struck done at design-spec L407. |
| 5 | `tests/test_message.py` — 275 OK including 6 new; reviewer runs them | **PASS** | 6 tests in `tests/test_message.py` (5 surface + 1 publish allowlist). Reviewer run: `Ran 6 tests … OK`; full `discover -s tests`: **`Ran 275 tests in 9.627s` / `OK`**. |

---

## Strengths

1. **Brief contracts land on the primitive, not only the CLI, for agent/to trust.** `write_message` calls `require_agent(from)` and `_check_to` before any file write (`outbox.py` L42–50, L68–74) — avoids the prior M2 class of “CLI-only registry gate” for those two checks.
2. **Allow-list is tight and tested adversarially.** Message publish does not inherit claim-lifecycle `default_allow` (which would also permit `store/claims.jsonl` / claim paths). Mid-op unrelated write is refused and rolled back (`test_message.py` L223–268).
3. **No claim coupling.** Happy-path tests assert empty claims/inbox before and after send; remote tree after publish has no `claims/*.json` from messaging.
4. **Docs match the surface.** SKILL when-to-use table, README status/non-goals, and §14 done-line all describe the same CLI and trust story. Spelling of `orchestrator` is consistent in code, docs, and reserved sets (`outbox.py` L16, `registry.py` L11).
5. **CLI flag names match the brief exactly** (`--from` / `--to` / `--type` / `--body`).

---

## Findings

### Critical

*(none)*

### Major

*(none)*

### Minor

#### m1. `write_message` does not enforce harness↔registry match (CLI does) — residual helper-boundary gap vs claim primitive

- **Evidence:** `outbox.write_message` takes `harness: str` and writes `from.harness` as given (`outbox.py` L33, L42, L56) after only `require_agent(hive, agent)`. No comparison to `rec["harness"]`. Contrast claim primitive `_require_registered` (`claim.py` L113–121) which refuses mismatches. CLI path is correct: `_message` → `_resolve_harness` (`cli.py` L214–215, L168–184); probe: `--harness nope` → exit 2. Probe: `write_message(..., harness="WRONG")` → **writes** `{"agent": "alice", "harness": "WRONG"}`.
- **Impact / blast radius:** Spec §5 (“harness must match the registry”) is true for the operator CLI but not for any harness/library caller that imports `write_message` (including `policy.write_message` for `budget_block`). Audit / lookback can record a false harness. No exclusivity break; same class of boundary risk Architect flagged (prior M2), narrowed to harness only — `from`/`to` are already gated in the primitive.
- **Recommended fix:** Inside `write_message`, after `require_agent`, refuse if `harness != rec["harness"]` (raise `ValueError` or reuse `ClaimDenied` / a small trust error). Add a unit test next to `tests/test_outbox.py`.

#### m2. README Helpers table omits `scripts/message.py`

- **Evidence:** `README.md` L104–112 lists init/inbox/claim/promote/status/lookback only. Body already documents the message example (L93–100) and status line (L7), so discovery is uneven.
- **Impact:** Operators scanning the table miss the new helper; SKILL.md is complete.
- **Recommended fix:** Add a row `| scripts/message.py | Open-ended agent→agent message (no claim) |` and mention `message` in the `python -m rip_swarm <cmd>` note.

### Nit

#### n1. Message subparser inherits unused `--agent` from the common parent

- **Evidence:** `common` adds `--agent` (`cli.py` L57); `message` uses `parents=[common]` (L101–103) but reads `args.from_agent` only (L214). Passing `--agent` alongside `--from` is silently ignored.
- **Impact:** Confusing operator UX; no trust bypass.
- **Fix:** Drop `--agent` from the message parser (custom parent without it), or reject `--agent` when `--from` is set.

---

## Spec drift (§5–§6)

| Spec lock | Implementation | Notes |
|-----------|----------------|-------|
| §6 `from.agent` in registry; unknown refuse | Yes | In `write_message` |
| §6 `to` ∈ registry ∪ `{orchestrator,*}` | Yes | `_TO_LITERALS` + `require_agent` |
| §6 messaging **not** gated on a claim | Yes | CLI + tests |
| §6 write outbox then identical JSONL append | Yes | `outbox.py` L63–64 |
| §6 type/topic enums + defaults | Yes | `TYPES`/`TOPICS`/`default_topic` |
| §6 body untrusted request | Yes | CLI wraps `{"text":…}`; SKILL/README warn |
| §5 harness must match registry | **CLI yes / primitive no** | Finding m1 |
| §5 gate covers message lifecycle | Partial | Agent/to yes; harness only on CLI |
| `message.schema.json` includes `ref` | Yes | Prior m4 disposition still holds |
| Naming `orchestrator` | Consistent | No `orchestator`-class typos |

---

## Non-goals confirmation (NOT built)

| Non-goal | Confirmation |
|----------|--------------|
| Marketplace pin | README L7 / L13 “no marketplace row yet”; spec §16 Q3 still open; no installer pin in package. |
| Grok Bot ↔ registry bridge | Spec §16 Q4 still open; no bridge module or registry sync code under `rip_swarm/` / `scripts/`. |
| Lookback → PR | Spec §16 Q5 / SKILL L101 / `lookback.py` write markdown only; no `gh`/`pull_request` usage in runtime. |

---

## Verification evidence

```text
# environment (reviewer box, Europe/Amsterdam)
$ cd /home/box/src/rip-swarm && git status && git log -1 --oneline
On branch feat/message-surface
Your branch is up to date with 'origin/feat/message-surface'.
nothing to commit, working tree clean
d215dab feat: open-ended message CLI (no claim; allow-listed publish).

# message tests
$ PYTHONPATH=. python3 -m unittest tests.test_message -v
...
Ran 6 tests in 0.312s
OK

# full suite
$ PYTHONPATH=. python3 -m unittest discover -s tests
Ran 275 tests in 9.627s
OK

# adversarial probes (temp dirs only; no repo mutation)
- write_message(from=ghost) → UnknownAgent
- write_message(to=nobody) → ValueError unknown message recipient
- write_message(to=orchestrator|*) → OK
- write_message(harness="WRONG") → WRITES mismatched from.harness  ← m1
- CLI --harness nope → exit 2
- alice→bob leaves bob/outbox absent (no shared mailbox)
- publish allowlist adversarial covered by test_publish_does_not_drag_unrelated_paths
```

---

## Recommended next moves

1. **Land harness check inside `write_message` (m1)** — align with claim’s `_require_registered`; one test. Highest-value residual trust fix.
2. **README Helpers row for `message.py` (m2)** — one-line doc hygiene.
3. Optional: refuse stray `--agent` on the message subparser (n1).
4. Parallel / unchanged: marketplace pin, Grok Bot bridge, lookback-PR remain off the critical path (spec §16).

---

## Reviewer note

This review intentionally does **not** change implementation. Tip `d215dab` is the Backend message-surface delivery under review; findings above are residual relative to that tip and the Architect brief / design-spec §5–§6. Prefer committing this doc on `feat/message-surface` so the PR carries the gate record.
