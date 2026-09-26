# Execution proposals: cascade, heartbeat, result messages, minimum review rounds

**Status:** proposal. Not folded into the roles-and-install spec or the skills.
**Date:** 2026-09-26
**Audience:** operator + implementer
**Related:** `docs/specs/2026-09-26-roles-and-install.md` §7.2, §7.4, §8 step 6, §9; `docs/specs/2026-09-17-design-spec.md` §9 (`reviews_required_per_plan` stays advisory); `skills/swarm-master/SKILL.md`; `skills/swarm-worker/SKILL.md`; `rip_swarm/waiter.py` (`tick`), `rip_swarm/acceptance.py` (`accept_task`), `rip_swarm/inbox.py`.

Four changes. Each is one mechanism. They come from a review of stability and speed on `fix/cascade-wake`, and from the operator's request for an easy way to require a minimum number of independent reviews for specs, plans and implementations.

---

## 1. What stays as it is

- The derived reject wake stays. While any task that is neither accepted nor rejected names a rejected id in `after`, every master tick reports `task-finished <id> reject` for the first such id. Recording that wake in the seen set is what stalled a master that died mid-cascade. Do not suppress the repeat when the board looks unchanged.
- One tombstone is one wake. `wait` marks it seen when it reports it, before the handler runs. Batching several `task-finished` events into one wake would let a crash during the first review swallow the rest.
- The idle poll stays at 30 seconds, with a `git fetch` each tick. On one machine that fetch is cheap next to a model turn. Shortening it does not shorten a review.
- `reviews_required_per_plan` stays advisory. Helpers keep ignoring it. The default stays 1, and that default is not applied to every task. The enforced rule is §5's `min_reviews`, per artifact kind.
- `fixes` keeps its meaning: accepting a follow-up accepts the original with `--via`, except for the stop in §5.5 (an artifact whose review rounds are not met).
- Replacements stay a master decision (still wanted or not, and what they wait on). The title suffix `(replaces <id>)` stays the way a repeat finds one that was already posted.
- One machine. The master does not push `rip-swarm/integration`. Workers hold one claim.

---

## 2. Reject the dependent chain in one publish

### Current

§9 step 4 tells the master to run `claim.py reject` once per unsettled dependent, each call its own fetch, commit, and push. A crash between two of those pushes leaves a dependent waiting on the rejected id. The next `wait` reports the same reject on the first tick, before the 30-second sleep. The skill says to stop if a pass changed nothing. That brake is a sentence. A master that waits again starts another full turn immediately.

### Change

A baton holder can reject the whole chain in one publish:

```text
claim.py reject --hive HIVE --task T --agent AGENT --cascade
```

The command walks `after` from `T`, including tasks that wait on a task already in the walk. For each task that is not yet rejected and has no live claim, it writes one reject tombstone with note `dependency <T> rejected`, in the same commit as the others. An expired claim is tombstoned `expired` first, as `master_reject` does today. A task with a live claim is skipped, not failed. The command prints each rejected id and each skip (`skipped <id> held by <agent> until <expires_at>`), then pushes once.

Exit 0 when the commit pushed, including when every dependent was skipped and the only output is skip lines. Exit 2 when the caller does not hold the baton, or when `T` itself is not rejected. A second run writes nothing for ids that already have a reject tombstone and prints those as already rejected.

§9 step 4 becomes this one command. Steps 1–3 (cascade note, replacements, orphaned original) stay prose, and replacements are still posted before the cascade command, so a crash while posting still leaves a dependent waiting and the derived wake still resumes that posting.

After the command, a repeat of the wake means a printed skip: someone still holds a dependent. The skill stops and reports those ids, as it already says to stop when a reject fails because of a live claim. The wake returns when that holder releases or expires and the task is unsettled again.

---

## 3. Heartbeat through a review from the profile

### Current

`wait` refreshes a lease once half of it is gone. A merge and an acceptance check run outside `wait`, after the lock is released. The master skill says to heartbeat every 15 minutes, which is half of the template's 30-minute `orchestrator_lease_ttl`. A long check with no heartbeat expires the baton. `join` then seats another master, and both can use `.worktrees/integration` until the old session's next heartbeat fails. A site profile that shortens the lease makes the hardcoded 15 minutes late: the code uses the profile, the skill does not.

### Change

`claim.py heartbeat` gains a loop mode for the orchestrator baton:

```text
claim.py heartbeat --hive HIVE --task orchestrator --agent AGENT --loop
```

It loads `orchestrator_lease_ttl` from the profile, sleeps until half of that remains, heartbeats, and repeats until the process is killed. A `ClaimDenied` (baton gone) prints the refusal and exits 2.

The master skill starts that loop in the background before the review-branch merge and kills it when the worktree is back on `rip-swarm/integration`, on every path (pass, shortfall, reject, conflict, error). The worker skill does the same for its own task around a long edit or test run, with `--task <id>` and `worker_lease_ttl`. The prose stops naming 15 minutes.

`wait` is unchanged. The loop runs only while the agent is not inside `wait`.

---

## 4. Drop the completion message

### Current

§8 step 6 and the worker skill: after `complete`, the worker publishes `message --to orchestrator --type result` with a one-line headline. `tick` reports unread messages before it looks at tombstones. The master wakes, reads the headline, and only on the next wait starts the review. That is one extra hive push and one extra model turn on every completion, and a completion ping jumps ahead of the review.

The headline is already the commit subject on the result sha. The complete tombstone is already the signal, and it carries `result_ref`.

### Change

Delete that message from §8 step 6 and from the worker skill. `complete` is the handoff.

Keep messages for questions, for a release or a conflict the master would not otherwise see a reason for, and for the goal broadcast. `tick`'s order (message before tombstones) stays, so a real question still arrives before the next review.

---

## 5. Minimum independent review rounds per artifact kind

**Status:** design agreed with the operator (2026-09-26). It replaces the earlier sketch of consecutive clean reviews.

Today the master accepts a completed task after one judgment of its own. Nothing lets the operator say "a spec needs two independent reviews" in a way that a takeover master still sees and that no model slip can skip. This section adds three things:
- a number per artifact kind, stated once;
- review tasks that workers do;
- a rule that `accept` enforces.

### 5.1 What the operator states

**Profile.** A new key, `min_reviews`, maps each artifact kind to the minimum number of independent review rounds:

```yaml
min_reviews:
  spec: 0
  plan: 0
  implementation: 0
```

The template and `DEFAULT_PROFILE` ship zeros. Zero is today's behaviour: no review tasks, and the master's own check alone. A hive profile sets its standing numbers, for example `spec: 2`, `plan: 2`, `implementation: 1`. `reviews_required_per_plan` stays advisory and unread (§1).

**Goal override.** The goal may contain the fixed phrase `reviews: <kind>=<N> …`, for example `/swarm-master Build the importer. reviews: spec=3 implementation=2`. Kinds that the phrase does not name fall back to the profile. The master broadcasts the goal as today (`goal: <goal>`), so a takeover master can read the override from that message.

**Resolution.** For each task the master posts as a spec, plan or implementation, N comes from the goal phrase, then the profile, then 0.

### 5.2 What lands on the board

| Field | On | Set with | Meaning |
|---|---|---|---|
| `kind` | an artifact | `inbox-add --kind spec\|plan\|implementation` | Which profile entry applies. Read by `status` and the synthesis; helpers do not act on it. |
| `min_reviews` | an artifact | `inbox-add --min-reviews N` | Integer ≥ 0. Absent means 0. The helpers enforce it (§5.5). |
| `reviews` | a review task | `inbox-add --reviews <A>` | The artifact this task reviews. It is not `fixes`. |
| `verdict` | the `complete` tombstone of a review task | `complete --verdict clean\|findings` | The reviewer's verdict. |

All inbox fields are create-only. `--reviews` follows the same existence rule as `after` and `fixes`: the named task must already be posted. Rebase, follow-up and review tasks get no `kind` and no `min_reviews`.

### 5.3 The review chain

A reviewed artifact `A` is a task with `min_reviews ≥ 1`. Its **chain** is:
- `A`;
- every task with `fixes: A`;
- every task with `reviews: A`.

In a reviewed chain, the master posts every revise, rebase and shortfall follow-up with `--fixes A`, never with `--fixes` of another chain task. That keeps the chain flat. Each chain task builds on the sha of the one before it, so the history is linear:

```
A (spec, min_reviews 2)  complete @a
 └ R1 reviews A        builds on a,  appends "Review 1" → complete --verdict findings @r1
    └ F1 fixes A       builds on r1, folds the findings, adds dispositions → complete @f1
       └ R2 reviews A  builds on f1, appends "Review 2" → complete --verdict clean @r2
          └ R3 …       only while rounds < N
master: merge r2 into a review branch, run its own acceptance check, accept R1, F1, R2, then A
```

**Review task.** Its body names:
- the sha to read;
- where the review goes: a numbered section appended to the reviewed doc (`## <n>. Review <k> (<agent>, <harness>)`), or `docs/reviews/<A>-r<k>.md` for code;
- the rule that nothing else is edited.

The worker builds on the named sha, as it already does for a `fixes` task. It commits the review and runs `complete --verdict clean|findings`.

**Revise task.** The master posts it after a review with findings (`--fixes A`). Its body names the review's sha to build on. The worker folds each finding and adds a dispositions table after the review section, as the operator's own review workflow does.

**Definitions** (all from the board):
- The chain is sequential: the master posts the next chain task only when `reviews.py` says so (§5.4), and it says so only while no chain task is open, claimed or blocked. So chain tasks complete in the order they were posted.
- The **head** is the chain task posted last among those that are completed and not rejected. Its sha is the short sha of its `result_ref`.
- A **round** is a review task in the chain that is completed and not rejected. One reviewer may do several rounds.
- `A` is **ready** when rounds ≥ `min_reviews` **and** the head is a review task whose verdict is `clean`.

Any change after the last clean review makes the head a fix, so `A` needs another round. That change may be a revise, a rebase after a merge conflict, or a follow-up after the master's own check falls short. A review the master judges too thin is rejected. It then is not a round, and the head falls back to the task before it.

### 5.4 `reviews.py`: the next step, decided by a helper

```text
reviews.py --hive HIVE --task <T>
```

`T` may be `A` or any task in its chain; the helper resolves `A` through `reviews` or `fixes`. It prints one line and exits 0:

```text
NEXT=<post-review|post-revise|merge|wait|done> ARTIFACT=<A> ROUNDS=<k>/<N> HEAD=<id> SHA=<sha> CHAIN=<id,id,…>
```

| Board | `NEXT` |
|---|---|
| `A` is accepted or rejected | `done` |
| `A` is not completed, or some chain task is open, claimed or blocked | `wait` |
| The head is `A` or a fix | `post-review` (round k+1, building on `SHA`) |
| The head is a review with verdict `findings` | `post-revise` (building on `SHA`) |
| The head is a clean review and k < N | `post-review` |
| The head is a clean review and k ≥ N | `merge` (`SHA` is what to merge) |

`CHAIN` lists the chain tasks that are completed and not rejected, in posting order, without `A`. It exits 1 when `T` is not a reviewed artifact and not in the chain of one.

Because `NEXT=post-…` appears only while no chain task is open, a repeated wake, a restart or a takeover never posts a chain task twice.

### 5.5 Helper guards

- **`inbox-add`** (exit 1, like an unknown `after` target):
  - The target of `--reviews` must exist and have `min_reviews ≥ 1`.
  - `--reviews` cannot be combined with `--fixes`, `--min-reviews` or `--kind`.
  - `--min-reviews` must be an integer ≥ 0.
  - `--kind` must be a key of the profile's `min_reviews` map.
- **`claim`** (exit 2, checked in `try_claim`, so every path is covered): a task with `reviews: A` is refused to any agent that completed `A` or any task with `fixes: A`. The message is `<agent> wrote <A>; its review must come from another agent`. This is the independence rule: the reviewer is not the author.
- **`complete`** (exit 1, a usage error): a task with `reviews` needs `--verdict clean|findings`, and any other task refuses `--verdict`. This is not exit 2, because the worker skill reads exit 2 from `complete` as a lost lease. Nothing is written when it fails.
- **`accept`** (exit 2, nothing written): `A` with `min_reviews ≥ 1` that is not ready (§5.3) is refused with `<A> needs <N> review rounds ending clean, has <k>`. `--via` does not bypass this. The `fixes` walk ends at this message without stopping the master, as it ends at a rejected task. Chain tasks themselves are accepted under today's rules.
- **`status`** shows the kind and progress next to an artifact, for example `(spec, reviews 1/2)`, and `(reviews <A>)` next to a review task.

### 5.6 Master skill

- **§4 Plan and post.**
  1. Read `min_reviews` from the profile and the `reviews:` phrase from the goal.
  2. For each spec, plan or implementation task, resolve N (§5.1) and post it with `--kind <kind> --min-reviews <N>`.
  3. Dependents are posted `--after` the artifact as today, so they stay blocked until it is accepted.
- **`wake task-finished <T> complete`**, a new first step. If `T` has `min_reviews ≥ 1`, or has `reviews`, or has `fixes: A` where `A` has `min_reviews ≥ 1`, run `reviews.py --task <T>` and act on `NEXT`. The remaining steps apply only through `merge`.
  - `done` or `wait`: nothing.
  - `post-review`: read the head first. If the head is a review too thin to count, reject it (`claim.py reject … --note "review too thin: <why>"`) and run `reviews.py` again. Otherwise post `Review <k+1> of <title>` with `--reviews <A>`, whose body names `SHA` and where the review goes (§5.3).
  - `post-revise`: judge the review in the same way first. Otherwise post `Revise <title> after review <k>` with `--fixes <A>`, whose body names `SHA` and the review's findings.
  - `merge`: run the §6 step 4 review block with `T=<A>` and `SHORT=<SHA>`. On a pass, accept each id in `CHAIN` in order, then `A`, all with `--integration-sha <NEW_TIP>`. On a conflict, a shortfall or "not worth pursuing", follow today's steps, with every follow-up posted `--fixes <A>`.
- **Reject handler.**
  - Step 2 never replaces a chain task (a review or a fix of a reviewed artifact): `reviews.py` posts the next chain task.
  - A replacement of an artifact copies its `kind` and `min_reviews`.
  - Step 4 also rejects every chain task of a rejected artifact that is not settled yet (`reviews.py … CHAIN`, plus any open review or fix), with `--note "dependency <T> rejected"`. Otherwise completed reviews and fixes would stay unaccepted, and `all-complete` would never fire.

### 5.7 Worker skill

A task whose inbox file has `reviews` is a review task:
1. Build on the sha its body names. This is the same step-1 block that `fixes` tasks use.
2. Read the artifact at that sha, and write the review where the body says.
3. Edit nothing else.
4. Commit it and run `complete --verdict clean|findings`. The verdict is `clean` only when the review has no finding that needs a change.

A worker's brief still does not travel to the hive; the task body is what tells the claimer to review rather than implement. A worker refused with `<agent> wrote <A>` goes back to waiting, like any other exit 2 on `claim`.

### 5.8 What stays

- With every `min_reviews` at 0, or the field absent, the path is exactly today's. No review tasks exist, `reviews.py` is never run, and `accept` checks nothing new.
- The master's own acceptance check stays and is not one of the N rounds.
- One tombstone is one wake (§1). The chain needs no new wake reason: each chain task's `complete` is the wake that moves it on.
- A master that dies mid-merge and restarts with its state kept is not woken for that `complete` again. That limit exists today for any `complete`; a takeover master re-seeds and is woken.

### 5.9 Testing

- **Unit tests:**
  - `inbox-add` field rules;
  - the independence refusal in `try_claim`;
  - `complete --verdict` required and refused;
  - `accept`'s readiness rule, as a table of chains (findings last, clean but short, clean and met, fix after clean, rejected review, `--via` walk stop);
  - `reviews.py`, as a table of boards, including `CHAIN` order and exit 1;
  - the `min_reviews` profile default;
  - the `status` annotations.
- **Rehearsal cases:**
  - spec with N=2: findings, revise, clean, then accept, with the review sections and dispositions in `rip-swarm/integration`;
  - the author is refused the review;
  - a takeover mid-chain posts nothing twice;
  - a thin review is rejected and does not count;
  - a rebase after a clean review needs another round;
  - rejecting a reviewed artifact rejects its chain and reaches `all-complete`;
  - N=0 is today's path.
- **Packaging needles:**
  - the goal phrase `reviews: <kind>=<N>`;
  - `--kind` and `--min-reviews` on the master's post;
  - `reviews.py` in the `complete` handler;
  - `--verdict` in the worker skill.

---

## 6. Order

1. §2 and §3 together. They close the two ways a master loses the board: a cascade split across pushes, and a baton expiring during the review.
2. §4. Skill and spec text. No helper change beyond tests that expect the result message.
3. §5 last. It adds inbox fields, `reviews.py`, a `claim` refusal, a `complete` flag and an `accept` refusal. The fast path is an artifact with `min_reviews` 0 or absent, which is every artifact until a profile or a goal names a number.
