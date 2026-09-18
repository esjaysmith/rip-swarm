# rip-swarm — message-surface re-gate (2026-09-18)

**Status:** advise-only.  
**Reviewed tip:** `0070188` (`fix: land message-surface review revisions (m1/m2/n1).`) on `feat/message-surface`.  
**Prior review:** `docs/plans/2026-09-18-message-surface-review.md` @ `fb25880` (accept-with-revisions on `d215dab`).

## Verdict

**Accept. Recommend merge for two-harness trial.**

All three prior revisions are closed with evidence. Full suite **278 OK** (prior 275 + 3).

## Checklist

| Item | Result | Evidence |
|------|--------|----------|
| **m1** `write_message`: `require_agent` + harness ≠ registry → `ClaimDenied` | **PASS** | `rip_swarm/outbox.py` L43–49; `tests/test_outbox.py::test_harness_mismatch_refused_before_any_write`; CLI exit 2 covered in `tests/test_message.py::test_harness_mismatch_via_cli_exit_2` |
| **m2** README Helpers includes `scripts/message.py` | **PASS** | `README.md` L112 row + L114 note |
| **n1** message subparser without inherited `--agent` | **PASS** | `cli.py` L54–62 `base` vs `common`; message uses `parents=[base]` L104–106; `test_message_does_not_accept_agent_flag` |
| Tests green | **PASS** | Reviewer run: `Ran 278 tests in 9.486s` / `OK` |

## Residual

None blocking. Prior brief items 1–5 and non-goals confirmation from the first review still hold on this tip (impl unchanged aside from the three fixes).

## Recommendation

Merge `feat/message-surface` → `master` and run the two-harness trial. Marketplace pin / Grok Bot bridge / lookback→PR remain out of v0.
