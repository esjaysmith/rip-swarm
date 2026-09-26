# rip-swarm: first trial runbook (revised 2026-09-26 for role commands)

**Project:** `~/code/adgency` (remote `esjaysmith/nr-adgency`), from branch `m37-r5`.
**Harnesses:** Claude Code and Grok Build on one machine; the operator is `op`.
**Goal:** review the ECU Pro next-step proposals (explainer section 5) and name the highest-gain next step.

## What this trial tests

1. **Exclusivity:** two sessions never own the same task.
2. **Seeing each other's work** through `wait` wakes, without the operator relaying anything.
3. **Handoff by message and review:** at least one piece of work changes because of the other session (a message, a follow-up or a rebase task).
4. **Leases:** background `wait` and heartbeats keep claims alive through long reads.
5. **Friction:** every workaround is a finding.

## Success criteria

| # | Criterion | Evidence |
|---|-----------|----------|
| S1 | Zero double work | `store/claims.jsonl`; `rip-swarm/integration` history |
| S2 | At least one lost race, handled with exit 2 | worker transcripts |
| S3 | At least one message, follow-up or rebase task acted on by the other session | `messages`; inbox `fixes` |
| S4 | The master reaches `all-complete` and writes the synthesis | `rip-swarm/integration` |
| S5 | No hand edits to hive files and no operator repair | `git log origin/swarm` |
| S6 | No session needed a nudge to wake | transcripts |

S1 or S5 failing fails the trial.

## Before

1. Install once: `npx skills add esjaysmith/rip-swarm -g -a claude-code -a grok -s '*' -y`.
2. In each harness, check that `/swarm-master` and `/swarm-worker` appear in the slash menu.
3. `cd ~/code/adgency && git switch -c m37-swarm-trial m37-r5`. The integration branch will start from here.
4. Pre-approve `python3 ~/.agents/skills/rip-swarm/scripts/*` in both harnesses, so permission prompts do not stall the loops.

## Run

In three sessions opened in `~/code/adgency`, start within a minute of each other:

| Session | Type |
|---------|------|
| Claude Code #1 | `/swarm-master Review the ECU Pro next-step proposals in docs/explainers/202609/2026-09-24-ecupro-stand-van-zaken.html section 5 (lines 692–765), using docs/PM-LEDGER.md lines 36–54 and 83–87, docs/plans/2026-37-negative-keywords-r5/sdd/m37-r5-c/progress.md lines 30–53 and measurements/2026-09-24-ecupro-train-miss-classes.txt. One review per lever (reactive 36, proactive 37, default list 76, variants 8), one reconciliation of the lever bars against the miss waterfall, then a synthesis naming ONE next wheel turn. Reviews in English under docs/plans/2026-37-negative-keywords-r5/reviews/. No code changes, no eval runs, no account changes.` |
| Grok Build | `/swarm-worker` |
| Claude Code #2 | `/swarm-worker reviewer specialist, do not implement` |

## During

Watch from your own read-only clone, never from an agent's hive clone: syncing there races that agent's git operations. Once: `git clone -q --single-branch -b swarm git@github.com:esjaysmith/nr-adgency.git ~/hive-watch`. Then, whenever you look: `python3 ~/.agents/skills/rip-swarm/scripts/sync.py --hive ~/hive-watch && python3 ~/.agents/skills/rip-swarm/scripts/status.py --hive ~/hive-watch`. Intervene only on a stuck state, and log every intervention.

## Close-out

1. Review `git log m37-swarm-trial..rip-swarm/integration` and merge it where you want it.
2. Run the lookback from `~/hive-watch`. This one step writes and publishes, so it is the exception to "read-only": `python3 ~/.agents/skills/rip-swarm/scripts/sync.py --hive ~/hive-watch && python3 ~/.agents/skills/rip-swarm/scripts/lookback.py --hive ~/hive-watch`. If you would rather keep `~/hive-watch` untouched, attach a fresh clone first (`python3 ~/.agents/skills/rip-swarm/scripts/init.py --hive /tmp/hive-lookback`, run inside the project) and pass that as `--hive`.
3. Fill in Results below and file each finding as a rip-swarm fix.

## Results

*(fill in after the run)*

| Criterion | Result | Notes |
|-----------|--------|-------|
| S1 | | |
| S2 | | |
| S3 | | |
| S4 | | |
| S5 | | |
| S6 | | |

**Operator interventions:**

**Helper friction:**

**Changes for trial 2:**
