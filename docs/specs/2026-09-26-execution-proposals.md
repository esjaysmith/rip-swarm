# Execution proposals: cascade, heartbeat, result messages, minimum review rounds

**Status:** proposal. §5 reviewed at `dc611ab` (§7): needs revision (2 critical, 5 major, 3 minor, 1 nit). Revision 2 folds §7 into §5 (dispositions in §8). §5 re-reviewed at `b74679a` (§9): needs revision (1 critical, 2 minor, 1 nit). Revision 3 folds §9 (dispositions in §10). §5 re-reviewed at `515ed0a` (§11): needs revision (1 major, 1 nit). Revision 4 folds §11 (dispositions in §12). A Grok review of `1e84398` (§13) is folded as revision 5 (dispositions in §14). The operator's fourth review, of `1e84398` (§15), is folded as revision 6 (dispositions in §16). A Grok review of `8c44ed2` (§17) is folded as revision 7 (dispositions in §18), and one of `2c044c7` (§19) as revision 8 (dispositions in §20). Not folded into the roles-and-install spec or the skills.
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

**Status:** design agreed with the operator (2026-09-26). Revision 2 folds the review in §7 (dispositions in §8); revision 3 folds the second review in §9 (dispositions in §10); revision 4 folds the third review in §11 (dispositions in §12); revision 5 folds a Grok review in §13 (dispositions in §14); revision 6 folds the fourth review in §15 (dispositions in §16); revision 7 folds a Grok review in §17 (dispositions in §18); revision 8 folds a second Grok review in §19 (dispositions in §20). It replaces the earlier sketch of consecutive clean reviews.

Today the master accepts a completed task after one judgment of its own. Nothing lets the operator say "a spec needs two independent reviews" in a way that a takeover master still sees and that the helpers enforce. With this section, every task the master marks as an artifact carries its number on the board, and no helper lets that number be lowered or its rounds skipped. Marking is the master's plan step: a task posted without `--kind` is not an artifact, and nothing forces one (§5.5). This section adds three things:
- a number per artifact kind, stated once;
- review tasks that workers do;
- a rule that `accept` enforces.

It builds on §2: the `--cascade` reject also walks a reviewed artifact's chain (§5.5).

### 5.1 What the operator states

**Profile.** A new key, `min_reviews`, maps each artifact kind to the minimum number of independent review rounds:

```yaml
min_reviews:
  spec: 0
  plan: 0
  implementation: 0
```

The template and `DEFAULT_PROFILE` ship zeros. Zero is today's behaviour: no review tasks, and the master's own check alone. A hive profile sets its standing numbers, for example `spec: 2`, `plan: 2`, `implementation: 1`. `reviews_required_per_plan` stays advisory and unread (§1).

**The profile is a floor.** The goal may contain the fixed phrase `reviews: <kind>=<N> …`, for example `/swarm-master Build the importer. reviews: spec=3 implementation=2`. The phrase can raise a kind's number above the profile; it cannot lower it (§5.5). Kinds that the phrase does not name use the profile. To run a goal with fewer reviews, the operator starts the master with a lighter profile, for example `RIP_SWARM_PROFILE=quick` with a `profiles/quick.yaml` that has lower numbers. Every helper resolves the profile that way.

**Resolution.**
- **Which number.** The master that posts the plan reads the phrase from its own goal argument. For each artifact, N is the larger of the phrase's number and the profile's.
- **What a later master uses.** It trusts the numbers already written on the inbox tasks. A replacement copies `kind` and `min_reviews` from the task it replaces (§5.6). For any other artifact a later master posts, it uses the profile.
- **The message log is never read for this.** Nobody scans it for the phrase: a new master's message cursor starts after the goal broadcast, and any worker note could contain the same words.

**Which tasks are artifacts.** One task per kind is the artifact: the spec, the plan, the implementation. That holds unless the goal names more than one of a kind. Subtasks of an implementation stay unkinded and are posted `--after` the artifact, so they do not each grow a chain.

### 5.2 What lands on the board

| Field | On | Set with | Meaning |
|---|---|---|---|
| `kind` | an artifact | `inbox-add --kind spec\|plan\|implementation` | Which profile entry applies. Read by `status` and the synthesis; helpers use it only for the floor (§5.5). |
| `min_reviews` | an artifact | `inbox-add --min-reviews N` | Integer ≥ 0. Absent means 0. With `--kind K` it defaults to the profile's `min_reviews[K]`, and it may not be lower (§5.5). The helpers enforce it. |
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
A (spec, min_reviews 2)  complete @a   by worker-1
 └ R1 reviews A        builds on a,  appends "Review 1" → complete --verdict findings @r1   by worker-2
    └ F1 fixes A       builds on r1, folds the findings, adds dispositions → complete @f1   by worker-1
       └ R2 reviews A  builds on f1, appends "Review 2" → complete --verdict clean @r2   by worker-2
          └ R3 …       only while rounds < N
master: merge r2 into a review branch, run its own acceptance check, accept R1, F1, R2, then A
```

**Review task.** Its body names:
- the sha to read;
- where the review goes: a numbered section appended to the reviewed doc (`## <n>. Review <k> (<agent>, <harness>)`), or `docs/reviews/<A>-r<k>.md` for code;
- the rule that nothing else is edited.

The worker builds on the named sha, as it already does for a `fixes` task. It commits the review and runs `complete --verdict clean|findings`. A review never resolves a merge conflict (§5.7).

**Revise task.** The master posts it after a review with findings (`--fixes A`). Its body names the review's sha to build on. The worker folds each finding and adds a dispositions table after the review section, as the operator's own review workflow does.

**Definitions** (all from the board):
- **The chain is sequential.** The master posts the next chain task only when `reviews.py` says so (§5.4), and it says so only while no chain task is open, claimed or blocked. So chain tasks complete in the order they were posted. **Posting order** is `created_at`, then task id: `created_at` has one-second resolution.
- **Head.** The chain task latest in posting order among those that are completed and not rejected. Its sha is the short sha of its `result_ref`.
- **Round.** A review task in the chain that is completed and not rejected.
- **Ready.** `A` is ready when rounds ≥ `min_reviews` **and** the head is a review task whose verdict is `clean`.

**Independence.** Every agent that works on the chain takes one of two roles for `A`. `claim` checks the roles from the board (§5.5); the head plays no part:
- **Authors** of `A`: every agent that completed `A` or a fix of `A`.
- **Reviewers** of `A`: every agent that has claimed a review of `A`. That means it holds a live claim on one, or has a tombstone on one (`complete`, `release`, `reject` or `expired`).
- **The two rules.** A review task is refused to an author, and a fix of `A` is refused to a reviewer.
  - No agent reviews text it wrote.
  - A reviewer never folds, rebases or follows up on the artifact, even when it only released a review that could not build.
  - One reviewer may do every round: after its own clean review, and after an author's fold.
- **With two workers**, the author folds and the other worker reviews every round. A third agent may fold instead; it then becomes an author and cannot review a later round.
- **When nobody may claim the next chain task**, `idle-board` reports it to the operator. That happens when a review is due and every remaining worker is an author, or a fix is due and every remaining worker is a reviewer.

Any change after the last clean review makes the head a fix, so `A` needs another round. That change may be a revise, a rebase after a merge conflict, or a follow-up after the master's own check falls short. A review the master rejects is not a round, and the head falls back to the task before it. The master rejects a review that is too thin, or whose verdict disagrees with its text (§5.6).

### 5.4 `reviews.py`: the next step, decided by a helper

```text
reviews.py --hive HIVE --task <T>
```

`T` may be `A` or any task in its chain; the helper resolves `A` through `reviews` or `fixes`. It prints one line and exits 0:

```text
NEXT=<post-review|post-revise|post-rebase|reject-review|merge|wait|done> ARTIFACT=<A> ROUNDS=<k>/<N> HEAD=<id> SHA=<sha> CHAIN=<id,id,…> [REVIEW=<id>]
```

| Board | `NEXT` |
|---|---|
| `A` is accepted or rejected | `done` |
| `A` is live and a review `R` of it is open (no claim, not completed, not rejected), and the note of `R`'s latest `release` tombstone is `review <R> cannot build on <sha>: conflict`: no fix of `A` has been posted since that release | `post-rebase` (`REVIEW=<R>`, and `SHA` is the `<sha>` from the note) |
| The same, but a fix of `A` has been posted since that release | `reject-review` (`REVIEW=<R>`: the rebase is already posted) |
| `A` is not completed, or some other chain task is open, claimed or blocked | `wait` |
| The head is `A` or a fix | `post-review` (round k+1, building on `SHA`) |
| The head is a review with verdict `findings` | `post-revise` (building on `SHA`) |
| The head is a clean review and k < N | `post-review` |
| The head is a clean review and k ≥ N | `merge` (`SHA` is what to merge) |

`CHAIN` lists the chain tasks that are completed and not rejected, in posting order, without `A`.

It exits 1 and prints no `NEXT` line in two cases:
- `T` is neither a reviewed artifact nor in the chain of one;
- the head is a review whose tombstone has no verdict, or a verdict other than `clean` or `findings`. `complete` never writes such a tombstone, so this means the board was edited by hand; the master reports it to the operator.

Because `NEXT=post-review` and `post-revise` appear only while no chain task is open, and `post-rebase` only while no fix has been posted since the release, a repeated wake, a restart or a takeover never posts a chain task twice.

**All of it is read from the board.** A review that could not build is found through its `release` tombstone, not through the wake or message that announced it. A new master marks old `release` tombstones seen and starts its message cursor after old messages (roles spec §7.5), so it would never see either. It still gets `NEXT=post-rebase` from the first chain wake it handles, for example the artifact's unaccepted `complete`.

### 5.5 Helper guards

- **`inbox-add`** (exit 1, like an unknown `after` target):
  - The target of `--reviews` must exist and have `min_reviews ≥ 1`.
  - `--reviews` cannot be combined with `--fixes`, `--min-reviews` or `--kind`.
  - `--min-reviews` must be an integer ≥ 0.
  - `--kind` must be a key of the profile's `min_reviews` map. With `--kind K`, a missing `--min-reviews` takes the profile's `min_reviews[K]`, and a lower one is refused: `min_reviews for <K> is at least <n> (profile)`. A slip cannot post a spec with fewer reviews than the profile asks. **One exception, a replacement's copy.** A post titled `… (replaces <X>)` may copy `X`'s number even below the current profile, if `X` is a rejected task with the same `kind` and exactly that `min_reviews`. `X` may have been posted under a lighter profile (§5.1) that the current master was not started with. The helper checks `X` on the board, so the exception covers an exact copy and nothing else.
  - A master that leaves out `--kind` entirely still posts an unreviewed task. The skill's plan step (§5.6) is what marks the artifacts.
- **`claim`** (exit 2, checked in `try_claim`, so every path is covered):
  - The independence rules of §5.3. A review task claimed by an author is refused with `<agent> wrote part of <A>; its review must come from another agent`. A fix of `A` claimed by a reviewer is refused with `<agent> reviewed <A>; its fixes must come from another agent`.
  - Any chain task of a rejected artifact is refused with `<A> is rejected`, so no new work starts on a dropped artifact.
- **`complete`** (exit 1, a usage error): a task with `reviews` needs `--verdict clean|findings`, and any other task refuses `--verdict`. This is not exit 2, because the worker skill reads exit 2 from `complete` as a lost lease. Nothing is written when it fails.
- **`accept`** (exit 2, nothing written):
  - `A` with `min_reviews ≥ 1` that is not ready (§5.3) is refused with `<A> needs <N> review rounds ending clean, has <k>`. `--via` does not bypass this.
  - The `fixes` walk ends at this message without stopping the master, as it ends at a rejected task.
  - Chain tasks themselves are accepted under today's rules.
- **`claim.py reject --cascade`** (§2) walks the chain of a rejected artifact as well as `after`. Each chain task that is neither accepted nor rejected and has no live claim gets a reject tombstone with the same note, in the same publish. A live claim is skipped and printed, as for `after`. A skip line for a chain task reads `skipped <id> (chain of <A>) held by <agent> until <expires_at>`, so it cannot be mistaken for a skipped `after` dependent.
- **The derived reject wake** (roles spec §7.2) also names a rejected artifact `A` while some task in its chain is neither accepted nor rejected and has **no live claim**. A chain task claimed at the moment `A` was rejected is left alone until its claim ends. It ends with `complete` or `release`, or by expiring, and the wake then returns and rejects it. A live claim never makes the wake spin.
- **`status`** shows the kind and progress next to an artifact, for example `(spec, reviews 1/2)`, and `(reviews <A>)` next to a review task.

### 5.6 Master skill

- **§4 Plan and post.**
  1. Read `min_reviews` from the profile and the `reviews:` phrase from the goal.
  2. Mark one task per kind as the artifact (§5.1).
  3. Post each artifact with `--kind <kind> --min-reviews <N>`, with N the larger of the phrase's number and the profile's.
  4. Post dependents and subtasks `--after` the artifact, so they stay blocked until it is accepted.
- **`wake task-finished <T> complete`**, a new first step. If `T` has `min_reviews ≥ 1`, or has `reviews`, or has `fixes: A` where `A` has `min_reviews ≥ 1`, run `reviews.py --task <T>` and act on `NEXT` with the arms below. The remaining steps of the skill's `complete` handler apply only through `merge`. **Every chain wake uses these same arms:** this one, the reject handler, and the release, message and `idle-board` wakes of a review that cannot build.
  - `post-rebase` or `reject-review`: act as under *A review that cannot build* below. Check these first, before reading the head: these values are about an open released review, and the head may be an earlier review that is fine.
  - **For `post-review`, `post-revise` and `merge`, read the head first when it is a review.** If the review is too thin, or its verdict disagrees with its text (for example `clean` over listed findings), reject it (`claim.py reject … --note "review rejected: <why>"`), run `reviews.py` again, and act on the new `NEXT`.
  - `done` or `wait`: nothing.
  - `post-review`: post `Review <k+1> of <title>` with `--reviews <A>`. The body names `SHA` and where the review goes (§5.3).
  - `post-revise`: post `Revise <title> after review <k>` with `--fixes <A>`. The body names `SHA` and the review's findings.
  - `merge`. This arm is entered from the wake of the clean head `R`, but everything it does is about `A`. **In every skill command below, `<T>` is `<A>`, not the woken task.** The review branch is `rip-swarm/review-<A>`, the `REVIEW=` value on the `OUTCOME=` line.
    1. Run `/swarm-master` §6 step 4, the `OUTCOME=` block, with `T=<A>` and `SHORT=<SHA>`. (That is the skill's §6, not this document's.) On `OUTCOME=merged` it stops with `WORKTREE` on `rip-swarm/review-<A>`. Run the master's acceptance check there, as in step 9.
    2. On a pass, heartbeat and run `reviews.py --task <A>` again **before** step 9's fast-forward command (*Passes*, item 1). It must print `NEXT=merge` with the same `HEAD=` and `SHA=` as the `reviews.py` line that entered this arm. `HEAD=` is the head's task id, and `SHA=` is its short sha. The `OUTCOME=` line's `SHA=` is the full id of the same commit and is not what is compared. Otherwise, run the *Falls short* command with `T=<A>`: it switches to `rip-swarm/integration` first, then deletes `rip-swarm/review-<A>` with `git branch -D`, because the branch is unmerged. Then act on the new `NEXT`.
    3. Run the *Passes* item 1 fast-forward command with `T=<A>` and the `TIP` from the `OUTCOME=` line. In place of *Passes* items 2 and 3, accept each id in `CHAIN` in order, then `A`, all with `--integration-sha <NEW_TIP>`. If `accept` still refuses after the fast-forward, report it to the operator and stop. Do not reset integration: the skill never forces, and a worker may already have merged the new tip.
    4. **Conflict.** The block has already deleted `rip-swarm/review-<A>`. Post the rebase with the skill's own rebase command (`/swarm-master` §6 step 5) and `T=<A>`. It is posted with `--fixes <A>`, titled `Rebase <title> onto rip-swarm/integration`, and its body is `Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.`, with `<SHA>` taken from the `OUTCOME=` line. A fix builds on the sha its body names, so without it the worker would rebuild from integration alone and drop the chain's commits. Then message the author of `A` (the agent that completed `A`), as today's step messages the worker. In a chain the head's completer is a reviewer, who is refused the rebase (§5.3), so the author is the worker who can take it.
    5. **Falls short.** Run the *Falls short* command with `T=<A>`, then post the follow-up with `--fixes <A>`, as today's *Falls short* step 2 does. Its body names the `SHA` from the `OUTCOME=` line to build on, and the gap to close.
    6. **Not worth pursuing.** Run the *Falls short* command with `T=<A>`, then reject **`A`**, not the woken review. Its reject handler cascades over the chain (§5.5).
    7. **`OUTCOME=dirty`** and **`OUTCOME=error`**: as the skill's §6 steps 7 and 8, with `T=<A>`. Clean up only leftovers of the master's own check and run step 1 again, or report and stop.
    8. **`OUTCOME=badsha`**: the head's `result_ref` is not a commit. At `merge` the head is always a review, so the bad result is that review, not the artifact. Reject the head review (`--note "result_ref <result_ref> is not a commit"`) and message its worker. Its reject wake runs `reviews.py` (reject handler below), and the head falls back to the task before it.
- **A review that cannot build** (§5.7). The seated master usually learns of it twice:
  - from the message `review <R> cannot build on <sha>: conflict`, which `tick` reports first;
  - from `wake task-finished <R> release` with the same note. The skill's release handler, today "nothing required", gains this case.

  Both arrivals, and an `idle-board` wake for a chain review, run `reviews.py --task <R>` and act on `NEXT` with the same arms as the `complete` wake, so no handler parses the note. A takeover master gets the same `NEXT` from its first chain wake, usually the artifact's unaccepted `complete` (§5.4), and acts on it there.
  - `post-rebase`:
    1. Post a rebase with `--fixes <A>` whose body is `Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.`, with `<SHA>` from that line, which is the sha from the note. It is a new head and costs another round.
    2. Then reject `REVIEW` (`--note "superseded by rebase <id>"`).
  - `reject-review`: the rebase was posted before a crash. Reject `REVIEW` only.

  The rebase is posted first, so the reject's own wake finds the chain busy and posts nothing. A later arrival finds `R` rejected, and `reviews.py` prints `wait` while the rebase is open.
- **Reject handler.**
  - **`T` is a chain task and `A` is live**, whether the master rejected a review or a worker rejected what it held. Run step 1 (the cascade note), skip steps 2 and 3, and run step 4. Step 2 must not replace a chain task. Step 3 would treat a rejected revise as orphaning `A`, which is still live and awaiting its next round. Then run `reviews.py --task <T>` and act on `NEXT`. This is also what resumes a master that died between rejecting a review and posting the next round: the reject tombstone is a wake it has not seen yet.
  - **`T` is a reviewed artifact.** Step 4 uses `--cascade`, which rejects its chain along with its `after` dependents (§5.5). A skipped chain task, `(chain of <A>)`, is not §2's stop. Go back to wait: the derived wake stays quiet while that claim is live and returns when it ends. §2's stop still applies to a skipped `after` dependent.
  - **Replacements.** A replacement of an artifact copies its `kind` and `min_reviews`, whatever the profile says now. A number raised by the goal phrase survives a takeover that way (§5.1), and a number lowered by a lighter profile is accepted as an exact copy (§5.5).
  - **A chain task that completes after `A` was rejected** gets `NEXT=done` from `reviews.py`. The derived wake then names `A` again, and its reject handler rejects the task. That is one path, not two.

### 5.7 Worker skill

A task whose inbox file has `reviews` is a review task:
1. Build on the sha its body names. This is the same step-1 block that `fixes` tasks use.
2. **If the block does not merge cleanly** (`SYNC=error`, or `BUILD=conflict` or `BUILD=error`):
   1. abort any merge in progress (`git -C "$WORKTREE" merge --abort`);
   2. release the task with `--note "review <id> cannot build on <sha>: conflict"`, where `<id>` is this review task;
   3. message the master with the same text.
   
   A review never resolves a conflict: a resolution would land inside the review commit and could be stamped `clean`. The master posts a rebase (§5.6).
3. Read the artifact at that sha, and write the review where the body says.
4. Edit nothing else.
5. Commit it and run the worker skill's usual `complete` command, `--result-ref` included, with `--verdict clean|findings` added. The verdict is `clean` only when the review has no finding that needs a change.

A worker's brief still does not travel to the hive; the task body is what tells the claimer to review rather than implement. A worker refused on `claim` goes back to waiting, like any other exit 2 on `claim`. That covers `<agent> wrote part of <A>`, `<agent> reviewed <A>` and `<A> is rejected`.

### 5.8 What stays

- With every `min_reviews` at 0, or the field absent, the path is exactly today's. No review tasks exist, `reviews.py` is never run, and `accept`, `claim` and the derived wake check nothing new.
- The master's own acceptance check stays and is not one of the N rounds.
- One tombstone is one wake (§1). The chain needs no new wake reason: each chain task's `complete` or `reject` is the wake that moves it on.
- **Two limits stay.** In both, a takeover master re-seeds and is woken:
  - A master that dies mid-merge and restarts with its state kept is not woken for that `complete` again. That limit exists today for any `complete`.
  - A master that dies inside the reject handler of a chain task, after the tombstone was reported, is not woken for it again. That limit exists today for a reject that nothing waits on.

### 5.9 Testing

- **Unit tests:**
  - `inbox-add` field rules, including the profile floor, the default from `--kind`, and the exact-copy exception for a `(replaces <X>)` post (refused when `X` is not rejected, or its `kind` or number differs);
  - the independence refusals in `try_claim`: an author refused a review; a reviewer refused a fix, whether it completed, released or still holds its review; and the same reviewer allowed a second round after its own clean review;
  - `claim` refused on a chain task of a rejected artifact;
  - `complete --verdict` required and refused;
  - `accept`'s readiness rule, as a table of chains (findings last, clean but short, clean and met, fix after clean, rejected review, `--via` walk stop);
  - `reviews.py`, as a table of boards, including `CHAIN` order, the `created_at` then id tie-break, and both exit-1 cases;
  - `--cascade` over a chain, with a live claim skipped;
  - the derived wake for a rejected artifact's chain, silent while a claim is live;
  - the `min_reviews` profile default and the `status` annotations.
- **Rehearsal cases** (two workers, as the role skills assume):
  - spec with N=2: findings, a revise by the author, clean by the other worker, then accept, with the review sections and dispositions in `rip-swarm/integration`;
  - the author is refused the review, and the reviewer is refused the revise;
  - N=2 with two clean rounds, both by the same reviewer;
  - a worker that released a review is refused the rebase, and the author takes it;
  - a takeover mid-chain posts nothing twice;
  - a thin review, or one whose verdict contradicts its text, is rejected and does not count;
  - a master that dies after rejecting a review posts the next round on the reject wake;
  - a review that cannot build releases, the rebase is posted on the sha from its note, and it costs another round. The release wake alone posts it, and the message plus the release together post it once;
  - `reviews.py` is checked again before the fast-forward, comparing its `HEAD=` and short `SHA=` with the line that entered the arm, and a clean chain is accepted;
  - a takeover after a review released as unable to build posts the rebase from the artifact's `complete` wake (the `post-rebase` arm of that wake), and a crash between that post and the reject ends in `reject-review` on the rebase's own `complete` wake;
  - rejecting a revise of a live artifact runs `reviews.py` and never the orphan step;
  - a skipped claimed review in the artifact cascade does not stop the master;
  - `OUTCOME=badsha` at `merge` rejects the head review, not `A`;
  - the pass, shortfall and "not worth pursuing" paths of `merge`, entered from a review's wake, fast-forward or delete `rip-swarm/review-<A>`, and the last one rejects `A`;
  - rejecting a reviewed artifact while one of its reviews is claimed: the review completes, the derived wake rejects it, and `all-complete` is reached;
  - N=0 is today's path.
- **Packaging needles:**
  - the goal phrase `reviews: <kind>=<N>` and the "larger of" rule;
  - `--kind` and `--min-reviews` on the master's post;
  - `reviews.py` in the `complete` and reject handlers, and the re-check before the fast-forward;
  - `--verdict` and the review task's conflict release in the worker skill.

---

## 6. Order

1. §2 and §3 together. They close the two ways a master loses the board: a cascade split across pushes, and a baton expiring during the review.
2. §4. Skill and spec text. No helper change beyond tests that expect the result message.
3. §5 last. It needs §2's `--cascade`. It adds inbox fields, `reviews.py`, `claim` refusals, a `complete` flag, an `accept` refusal and a derived wake for a rejected artifact's chain. The fast path is an artifact with `min_reviews` 0 or absent, which is every artifact until a profile or a goal names a number.

---

## 7. Review of §5 (2026-09-26) — `dc611ab`

**Verdict:** needs revision (2 critical, 5 major, 3 minor, 1 nit).
**Reviewed tip:** `dc611ab` (`docs: execution proposals, §5 minimum independent review rounds`) on `feat/min-reviews`, based on `fix/cascade-wake` at `2017874`.
**Scope:** §5 only. §1–§4 and §6 were not re-reviewed.

The chain and the `accept` gate are the right shape. A review task uses `reviews`, not `fixes`, so accepting it does not accept the artifact. The master's own check is not one of the N rounds. `complete --verdict` is exit 1, so a worker does not treat a bad flag as a lost lease. `NEXT` is computed only while no chain task is open, claimed or blocked, so a repeat does not post a second review. With `min_reviews` at 0 the old path is unchanged.

### Critical

#### C1. A findings round excludes both workers

§5.5 refuses a review to any agent that completed the artifact or any task with `fixes` pointing at it. §5.3 allows one reviewer to do several rounds. Those two rules meet only while nobody has folded anything.

`/swarm-master` and `/swarm-worker` are built for two workers, and an empty brief claims whatever is offered. Both are offered the revise. If the reviewer claims it, the excluded set is both agents: the author completed the artifact, the reviewer completed a fix. The next review stays open. `idle-board` reports it, and nothing in the protocol frees it. The N=2 rehearsal in §5.9 (findings, revise, clean) hits this as soon as the reviewer folds.

**Fix.** Refuse a review to the completer of the sha it builds on, and still refuse the completer of the artifact, so the author stays out of every round. Do not exclude every historical fixer. Also refuse a `fixes` task of a reviewed artifact to the agent who completed the current head when that head is a review, so the reviewer cannot take the revise and then be locked out of the next round. The author, or a third agent, folds. The reviewer stays eligible.

#### C2. Rejecting the artifact does not keep a wake on the chain

§5.3's chain tasks point at the artifact with `reviews` or `fixes`, not `after`. The derived reject wake only sees `after` (§1, roles spec §7.2). §5.6 tells the reject handler to reject the chain in the same turn as the artifact, including open reviews.

That wake does not come back for the chain. A live claim makes today's reject handler stop (§9: report and stop, do not wait again). A chain task that completes afterward sees `NEXT=done`, because the artifact is already rejected, and §5.6 says `done` is nothing. The task sits completed and unaccepted. `all-complete` stays false.

A takeover does not repair it. Reject tombstones are seeded (roles spec §7.5), and the derived wake still does not look at `reviews` or `fixes`.

**Fix.** Reject the chain in the same publish as the artifact, skipping live claims and printing them, as §2 does for `after`. While the artifact is rejected and any chain task is neither accepted nor rejected, the derived wake names that artifact. On that wake, and on a later `complete` of a chain task, reject the chain task instead of treating `NEXT=done` as nothing.

### Major

#### M1. Rejecting one chain task while the artifact lives has the same gap

§5.6 rejects a thin review inside the `complete` handler, then runs `reviews.py` again. The reject tombstone is a later wake, and the reject handler does not call `reviews.py`. Step 2 is forbidden to replace a chain task. If the master dies between the reject and the new post, nothing posts the next round. A worker rejecting a review it holds takes the same path.

**Fix.** On `task-finished <T> reject`, when `T` is a chain task and the artifact is still live, run `reviews.py` and act on `NEXT`. The "nothing open" rule already makes that post idempotent.

#### M2. The pass path fast-forwards before `accept`

§5.6 `merge` runs the existing review block (roles spec §9). That block fast-forwards `rip-swarm/integration` and only then accepts. §5.5 refuses `accept` when the artifact is not ready, and a `--via` walk can hit the same refusal. The sha is already on integration, which is the leak the review branch exists to prevent.

**Fix.** Check readiness before the fast-forward. If `accept` then fails, reset integration to `TIP` and delete the review branch, as a shortfall does.

#### M3. An intermediate `clean` is the worker's word

§5.4 trusts `--verdict`. §5.6 re-reads the head only to reject a thin review, and only on `post-review` and `post-revise`. A review that reports findings and completes `clean` counts as a round (§5.3). At `k < N` that skips the revise. At `k ≥ N` the master's acceptance check still runs, so only the last round is protected.

**Fix.** Before acting on `NEXT`, the master reads the review. If the text and the verdict disagree, it rejects the review and runs `reviews.py` again, the same as "too thin".

#### M4. A review task is told to resolve merge conflicts

§5.7 uses the `fixes` step-1 block (roles spec §8). In that block a conflict is the work: the worker resolves it and commits. §5.3 says a review edits nothing else. A conflict with `rip-swarm/integration` would be folded into the review commit and can be stamped `clean`.

**Fix.** If either merge conflicts, the worker releases and tells the master. The master posts a rebase with `--fixes` of the artifact. That is a new head and costs another round. The worker does not resolve the conflict inside the review.

#### M5. The helper will accept a slip that sets the number to 0

§5's aim is that a takeover still sees the number and a model slip cannot skip it. The enforced value is whatever the master copied onto the inbox file (§5.2, §5.5). `kind` is not checked against the profile. `--min-reviews 0` on a spec, while the profile says `spec: 2`, is a legal post, and no review task is required.

**Fix.** `inbox-add --kind K` refuses a `--min-reviews` below `min_reviews[K]` in the profile. The goal phrase can still raise the number. It cannot lower it. A replacement already copies the artifact's own fields (§5.6), so this binds the original post.

### Minor

#### m1. The goal message is the wrong place to leave the override

§5.1 says a takeover reads the `reviews:` phrase from the broadcast goal. A new master seeds `messages_cursor` at the newest message (roles spec §7.5), so `messages --new` does not show `goal: …`. The number that survives is the inbox field, plus the profile for anything the takeover posts itself. A worker note can contain `reviews: spec=0`, so a search of message text is not a safe parser.

**Fix.** The operator's goal argument is parsed by the master who posts the tasks, and the inbox field is what a later master trusts. The profile is the floor for an artifact that master posts itself (§5.5, M5). Do not scan the message log for the phrase.

#### m2. Every task of a kind grows a full chain

§5.6 marks each spec, plan or implementation task with `--kind` and `--min-reviews`. A master that marks every coding subtask `implementation` gives each one its own rounds.

**Fix.** Mark one task per kind, the artifact itself, unless the goal names more than one. Subtasks stay unkinded and wait `--after` the artifact.

#### m3. `reviews.py` has no row for a review with no verdict

§5.5 makes `complete` refuse a review without `--verdict`, so a new tombstone should not lack one. §5.4's table still has no row when the head is a review and the verdict is missing or neither `clean` nor `findings`.

**Fix.** That board exits 1 and prints no `NEXT` line.

### Nit

#### n1. "Posted last" does not name a tie-break

§5.3 and §5.4 order `CHAIN` and the head by posting time. Two posts in the same second need `created_at`, then task id.

---

## 8. Dispositions (revision 2, 2026-09-26)

Each finding was checked against the code and the roles spec before it was folded. The operator ruled on M5.

| Finding | Disposition | Where |
|---|---|---|
| C1. A findings round excludes both workers | Accepted. A review is refused to the author and to the completer of the head it builds on, not to every historical fixer. A fix is refused to the completer of a review head, so a reviewer does not fold its own findings. With two workers, the author folds and the other reviews. | §5.3 Independence, §5.5 `claim`, §5.9 |
| C2. Rejecting the artifact does not keep a wake on the chain | Accepted. §2's `--cascade` walks the chain in the same publish, skipping live claims. The derived reject wake also names a rejected artifact while a chain task is unsettled and unclaimed, so a task claimed at the moment of the reject is rejected when its claim ends. `claim` refuses a chain task of a rejected artifact. A late `complete` goes through the same reject path, not a second one. | §5.5, §5.6 reject handler, §6 |
| M1. Rejecting one chain task while the artifact lives has the same gap | Accepted. The reject handler runs `reviews.py` for a chain task of a live artifact and acts on `NEXT`. That also resumes a master that died between the reject and the post. | §5.6 reject handler |
| M2. The pass path fast-forwards before `accept` | Accepted in part. `reviews.py` is re-run before the fast-forward and must still say `merge` with the same sha. Declined: resetting integration if `accept` still fails. The master skill never forces, and a worker may already have merged the tip (roles spec §17 C1). The master reports and stops instead. | §5.6 `merge` |
| M3. An intermediate `clean` is the worker's word | Accepted. The master reads every review head before acting on `NEXT`, and rejects a review whose verdict disagrees with its text. | §5.3, §5.6 |
| M4. A review task is told to resolve merge conflicts | Accepted. A review that cannot build aborts, releases with a fixed note and messages the master. The master posts a rebase (a new head, another round), then rejects the released review. | §5.3, §5.6, §5.7 |
| M5. The helper will accept a slip that sets the number to 0 | Accepted (operator: the profile is a floor). `--kind K` defaults `--min-reviews` to the profile's value and refuses a lower one. A goal can only raise the number; fewer reviews need a lighter profile (`RIP_SWARM_PROFILE`). | §5.1, §5.2, §5.5 |
| m1. The goal message is the wrong place to leave the override | Accepted. The posting master parses its own goal argument; a later master trusts the inbox fields and uses the profile for what it posts. The message log is never scanned. | §5.1 |
| m2. Every task of a kind grows a full chain | Accepted. One artifact per kind unless the goal names more; subtasks stay unkinded and wait `--after` the artifact. | §5.1, §5.6 |
| m3. `reviews.py` has no row for a review with no verdict | Accepted. It exits 1 with no `NEXT` line, and the master reports it. | §5.4 |
| n1. "Posted last" does not name a tie-break | Accepted. Posting order is `created_at`, then task id. | §5.3 |

---

## 9. Second review of §5 (2026-09-26) — `b74679a`

**Reviewed tip:** `b74679a` on `feat/min-reviews`. §7 was not re-opened. (The operator pasted this review in chat; it is recorded here as given.)

Revision 2 needs another pass. The §7 fold holds on the reject wake, the fast-forward re-check, the verdict re-read, and the profile floor. The independence rules, as now written, lock out the only legal reviewer in the round the section itself posts when a review comes back clean.

What holds: --cascade and the derived wake cover a rejected artifact's chain, including a review that was still claimed. A chain-task reject runs reviews.py, so a thin review rejected inside the complete handler is resumed from the reject tombstone. reviews.py is run again before the fast-forward, and a refusal after that is reported rather than reset, as the disposition says. A review that cannot merge is released instead of resolved. --kind cannot be posted below the profile. The goal phrase is not read from the message log.

### Critical

#### C1. A clean intermediate round excludes both workers

The findings path is fixed. The author folds, because a fix is refused to the completer of a review head, and the other worker can review the fold.

The other path is N greater than 1 and a clean review before the last round. §5.4 then posts the next review on that review's sha. §5.3 refuses the new review to the author, and also to the agent who completed the head. The head is the clean review, so that agent is the reviewer. With the two workers the rehearsals assume, nobody can claim the next round. idle-board is the only way out.

The same rule lets the reviewer write artifact text whenever the head is not a review. A first review that cannot merge is released, not completed, so the head is still the artifact. The rebase is a fix, and the fix-refusal applies only when the head is a review. If the reviewer claims that rebase, the next review is refused to both of them again.

§5.3 says one reviewer may do several rounds. The claim rule makes that impossible as soon as the previous head is their own review.

Fix. A review is refused to the author of the artifact, and to the completer of the head only when that head is the artifact or a fix. A previous review does not disqualify its author from the next round. A fix of a reviewed artifact is refused to anyone who has completed a review of it, not only when the current head happens to be a review, so a reviewer never takes the rebase or the revise. The author, or an agent who has not reviewed, folds.

### Minor

#### m1. The conflict rebase does not say which sha, and it is not on the release wake

§5.6 posts that rebase "on SHA" in the turn where the review was released. This path does not run reviews.py, so SHA is the field from a line that was not printed. The sha that failed is the one in the release note.

The procedure is also not attached to task-finished release. The base handler for a release is still "nothing required." tick reports a message before a tombstone, so the note usually arrives first. If the release is visible and the message is not, the review is open again and this section does not tell the master to post the rebase.

Fix. On task-finished <R> release, when the note is review cannot build on <sha>: conflict and the artifact is live, post the rebase on that sha, then reject the review. The same turn handles the message if it arrives first.

#### m2. The opening still says a model slip cannot skip the rounds

§5.5 says a post with no --kind is a legal unreviewed task, and only the plan step marks artifacts. That is a slip. The profile floor applies only after --kind is present.

Fix. Say that in §5's opening. The enforced floor is on tasks the master marks, not on every task it posts.

### Nit

#### n1. "§6 step 4" is not this document

The re-check works only if step 1 is the master skill's OUTCOME= block, which stops on the review branch. This document's §6 is the implementation order and has no review block. The skill's later pass path fast-forwards before accept. Running that path as step 1 puts the sha on integration before the re-check.

---

## 10. Dispositions (revision 3, 2026-09-26)

Each finding was checked against revision 2's text before it was folded.

| Finding | Disposition | Where |
|---|---|---|
| C1. A clean intermediate round excludes both workers | Accepted, with a stricter rule than the proposed fix. Both lockouts are real. The proposed fix still leaves the second one open: it refuses a fix only to an agent that *completed* a review, so a worker who *released* a review that could not build may take the rebase and then be refused the next review, while the author is refused too. The rule now splits agents into roles and never looks at the head. **Authors** of `A` completed `A` or a fix of `A`. **Reviewers** of `A` hold, or have held, a claim on a review of `A` (live claim or any tombstone). A review is refused to an author, and a fix is refused to a reviewer. One reviewer may do every round, and with two workers the author folds and the other reviews. | §5.3 Independence, §5.5 `claim`, §5.9 |
| m1. The conflict rebase does not say which sha, and it is not on the release wake | Accepted. The release handler gains the case. The rebase builds on the sha in the release note. Whichever of the release and the message arrives first posts the rebase, unless an open fix of `A` exists, and then rejects the review. The second arrival does nothing. | §5.6, §5.9 |
| m2. The opening still says a model slip cannot skip the rounds | Accepted. The opening says the floor binds the tasks the master marks, and that a task posted without `--kind` is not an artifact. | §5 opening |
| n1. "§6 step 4" is not this document | Accepted. Step 1 is `/swarm-master` §6 step 4's `OUTCOME=` block, which stops on the review branch. The acceptance check runs there. The re-check runs before step 9's fast-forward command, and the chain accepts replace *Passes* items 2 and 3. | §5.6 `merge` |

---

## 11. Third review of §5 (2026-09-27) — `515ed0a`

**Reviewed tip:** `515ed0a` on `feat/min-reviews`. §7 and §9 were not re-opened. (The operator pasted this review in chat; it is recorded here as given.)

Revision 3 needs another pass. The role split, the release-note rebase, and the profile-floor wording hold. The merge arm still fast-forwards a review branch this section never creates.

What holds: an author is refused every review, and a reviewer is refused every fix, including a reviewer who only released. One reviewer can take the next round, so two clean rounds and the rebase after a review that could not build both have a legal claimant. The rebase uses the sha from the release note, once, whichever of the release and the message arrives first. A task posted without --kind is not an artifact. Step 1 of merge is the skill's OUTCOME= block and stops on the review branch. The re-check is before the fast-forward, and a refusal after that is reported rather than reset.

### Major

#### M1. The pass path fast-forwards rip-swarm/review-<wake task>

merge is entered from wake task-finished <R> complete, and R is the clean head, not A. Step 1 runs the OUTCOME= block with T=<A>, so the branch it creates is rip-swarm/review-<A>. That block is one shell. The skill says those variables do not survive, and the next command copies TIP and SHA from the OUTCOME= line, not the branch name.

Step 3 runs Passes item 1. That command builds rip-swarm/review-$T from T. Filled in as the skill defines T, from the wake, that is rip-swarm/review-<R>, which was never created. merge --ff-only fails, the command prints FAILED, and the skill says to report and stop. Nothing accepts A, and nothing posts a wake that would try again.

The shortfall delete and "not worth pursuing" use that same T. Falls short runs git branch -D on rip-swarm/review-<R> after switching to integration, so the delete fails and rip-swarm/review-<A> is left behind. "Not worth pursuing" then rejects the wake's task. That is the head review. reviews.py posts another round, and A stays. The re-check's "delete the review branch" does not name rip-swarm/review-<A> either, and an unmerged branch needs -D.

The conflict path is fine: the OUTCOME= block deletes $REVIEW inside the shell where T=<A>.

Fix. After the block, the branch is the REVIEW value from the OUTCOME= line, rip-swarm/review-<A>. The fast-forward, the shortfall delete, and the re-check delete all use that name. An unmerged branch is removed with git branch -D only after the worktree is back on rip-swarm/integration. "Not worth pursuing" rejects A and runs the artifact cascade.

### Nit

#### n1. The message that usually arrives first does not name the review

The release wake carries <R>. The message is only the note, review cannot build on <sha>: conflict, and tick reports the message first. The procedure then says to reject R. The release wake still has the id, so a master who cannot find R from the note is saved by the second arrival. The arm that usually runs first should not have to infer the task.

Fix. The message names the review task as well as the sha.

---

## 12. Dispositions (revision 4, 2026-09-27)

| Finding | Disposition | Where |
|---|---|---|
| M1. The pass path fast-forwards `rip-swarm/review-<wake task>` | Accepted. The `merge` arm says that in every skill command it runs, `<T>` is `<A>`, not the woken review. The branch is `rip-swarm/review-<A>`, the `REVIEW=` value on the `OUTCOME=` line. The pass path runs *Passes* item 1 with `T=<A>`. The re-check failure and the shortfall run the *Falls short* command with `T=<A>`, which switches to integration before `git branch -D`. "Not worth pursuing" rejects `A`, and its reject handler cascades over the chain. The conflict path is unchanged. A rehearsal case covers the three paths entered from a review's wake. | §5.6 `merge`, §5.9 |
| n1. The message that usually arrives first does not name the review | Accepted. The note and the message are `review <R> cannot build on <sha>: conflict`. | §5.6, §5.7 |

---

## 13. Grok review of §5 (2026-09-27) — `1e84398`

**Reviewer:** Grok Build (`grok-4.7`, low effort), run through the grok-build bridge on the diff `1e84398~1..1e84398`. Recorded as given, without its one-line preamble.

Revision 4 needs another pass. The merge-arm branch name and the release note that names the review hold. Rewriting the conflict step dropped the SHA the worker must merge.

What holds: `merge` is entered from `wake task-finished <R> complete`, and every skill command in that arm fills `<T>` with `<A>`. *Passes* item 1 then fast-forwards `rip-swarm/review-<A>` and deletes it with `-d` after the fast-forward. A failed re-check and a shortfall run the *Falls short* command with `T=<A>`, which switches to `rip-swarm/integration` before `git branch -D`. "Not worth pursuing" rejects `A`, and the artifact reject handler cascades over the chain. The release note and the message are both `review <R> cannot build on <sha>: conflict`, so the arrival `tick` reports first no longer has to infer the id.

### Minor

#### m1. The conflict rebase no longer names the sha

The previous conflict arm said to follow today's steps with `--fixes <A>`. Those steps are `/swarm-master` §6 step 5: the post body is `Merge <SHA> onto rip-swarm/integration and resolve the conflict`, and `<SHA>` is copied from the `OUTCOME=` line.

Step 4 now says only that the block has deleted `rip-swarm/review-<A>` and to post the rebase with `--fixes <A>`. Nothing in that step names `<SHA>`. The shortfall arm still says the follow-up builds on `SHA`, and the cannot-build arm still says the body names the sha from the note. This arm does not. A worker posted from step 4 alone has no commit to merge.

The disposition says the conflict path is unchanged. The text no longer points at the skill post that carries the sha.

Fix: state that the conflict post is the skill's rebase command with `T=<A>`, and that its body names the `SHA` from the `OUTCOME=` line.

---

## 14. Dispositions (revision 5, 2026-09-27)

| Finding | Disposition | Where |
|---|---|---|
| m1. The conflict rebase no longer names the sha | Accepted. Step 4 posts the skill's own rebase command with `T=<A>`: `--fixes <A>`, today's title, and today's body `Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.`, with `<SHA>` from the `OUTCOME=` line. | §5.6 `merge` step 4 |

---

## 15. Fourth review of §5 (2026-09-27) — `1e84398`

**Reviewed tip:** `1e84398` on `feat/min-reviews`. §7, §9, and §11 were not re-opened. (The operator pasted this review in chat; it is recorded here as given. It arrived after revision 5 had already restored the sha, and adds the worker message and the shortfall gap.)

Revision 4 needs another pass. The branch name is fixed, and the release note now carries the review's id. Rewriting the conflict step dropped the sha the worker has to merge.

What holds: in the merge arm every skill command fills <T> with <A>, so the pass path fast-forwards rip-swarm/review-<A> and the re-check and shortfall paths delete that branch with git branch -D after switching back to rip-swarm/integration. "Not worth pursuing" rejects A, and that reject cascades over the chain. The release note and the message are review <R> cannot build on <sha>: conflict.

### Minor

#### m1. The conflict rebase no longer names the sha

§11 left the conflict path on today's steps. Those steps post a rebase whose body is Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work, with SHA taken from the OUTCOME= line, and then message the worker. Revision 4 replaces that with "Post the rebase with --fixes <A>."

A fixes task builds on the sha its body names. With no sha, BUILD_ON is empty and the worker resets onto rip-swarm/integration instead of merging the head. The rebase completes without the chain's commits, and the next review reads that tree. The shortfall step kept its sha ("building on SHA") and dropped the rest of today's sentence, the gap to close.

Fix. The conflict body stays today's sentence, with --fixes <A> and SHA from the OUTCOME= line, and the worker is still messaged. The shortfall body names that same sha and the gap.

---

## 16. Dispositions (revision 6, 2026-09-27)

| Finding | Disposition | Where |
|---|---|---|
| m1. The conflict rebase no longer names the sha | Accepted. Revision 5 had restored today's rebase body with `<SHA>` from the `OUTCOME=` line. Revision 6 adds the rest. The conflict step messages a worker again: the author of `A`, not the head's completer. In a chain the head is a review, and its completer is a reviewer, who is refused every fix (§5.3). The shortfall body names the same `SHA` and the gap to close, as today's *Falls short* step 2 does. | §5.6 `merge` steps 4 and 5 |


---

## 17. Grok review of §5 (2026-09-27) — `8c44ed2`

**Reviewer:** Grok Build (`grok-4.7`, high effort), a read-only run through the grok-build bridge over all of §5 at `8c44ed2`, checked against the roles spec, the skills and the helpers. Recorded as given, without its preamble.

Revision 6 needs another pass. Two paths never accept the artifact: the pass re-check compares two different `SHA` strings and takes the failure arm on every clean chain, and a review that cannot build stays open after a new master joins, so `reviews.py` waits forever.

What holds: With two workers, an author is refused every review and a reviewer (including one who only released) is refused every fix, so N=1 and N=2, findings and clean, each have a legal claimant, and one reviewer can take every round. `reviews.py` posts only while no chain task is open, so a repeated complete does not post a second round. A thin review rejected in the complete handler is resumed by its reject tombstone, and a takeover is resumed by the artifact's bare complete. Rejecting the artifact cascades the chain, stays quiet while a review is claimed, and the derived wake rejects that review after `complete` (`NEXT=done`). The merge arm fills `<T>` with the artifact, so the fast-forward and `git branch -D` name `rip-swarm/review-<A>`; the conflict body carries the `OUTCOME=` sha and messages the author; the shortfall body names that sha and the gap. `--kind` cannot go below the profile, the goal phrase is not read from the message log, and `complete --verdict` is specified as exit 1.

### Critical

#### C1. The pass re-check compares two different SHA strings and never fast-forwards

§5.6 step 2 requires the second `reviews.py` line to be `NEXT=merge` with the same `SHA`, and on any other result deletes `rip-swarm/review-<A>` and acts on the new `NEXT`. The skill tells every later command to take `SHA` from the `OUTCOME=` line (`skills/swarm-master/SKILL.md` lines 119–122). That value is `git rev-parse SHORT^{commit}`, the full id. `reviews.py` prints the short id stored in `result_ref` (§5.3, §5.4; the worker writes it with `rev-parse --short`, `skills/swarm-worker/SKILL.md` line 110).

N=1, two workers. The author completes the spec. The reviewer completes `clean`. `reviews.py` prints `NEXT=merge SHA=a1b2c3d`. The block prints `SHA=` plus the full id of that commit. The check passes, the second `reviews.py` prints `SHA=a1b2c3d` again, and a literal compare against the `OUTCOME` value fails. The arm deletes the review branch. The new `NEXT` is still `merge`, so the arm runs again and fails the same compare. The artifact stays completed and unaccepted. A takeover marks that complete seen on the first pass and stops the same way, so `all-complete` never fires.

Fix. Compare the second `reviews.py` line to the `reviews.py` line that entered the arm (both short ids), or compare `git rev-parse` of each. State that `OUTCOME`'s `SHA=` is the full id of that same commit and is not the string to compare. On a match, fast-forward. On a real head change, delete the branch and act on the new `NEXT`.

#### C2. A takeover leaves a review that cannot build open, and the chain waits forever

§5.6 runs the cannot-build procedure only from the release wake and from the message. §7.5 seeds both away for a new master: every `release` tombstone is seen (`skills/rip-swarm/rip_swarm/state.py` lines 48–53) and `messages_cursor` starts at the newest message. `reviews.py` then returns `wait` whenever that review is open (§5.4). A release with no complete and no reject is open (`board.py` lines 72–78).

N=1 or N=2. Worker 2 claims the review, the build does not merge, and worker 2 releases with `review <R> cannot build on <sha>: conflict` and messages the master. The baton has expired, or the seated master dies without handling the wake. The next master joins. Its first chain wake is the artifact's bare complete. `reviews.py` sees `R` open and prints `wait`. §5.6 says `wait` is nothing, so no rebase is posted and `R` is not rejected. Worker 1 is offered `R` and refused as the author; that offer is marked seen. Worker 2 is not re-offered a task it released (§7.3). `R` stays open, later completes stay on `wait`, and `idle-board` only reports `R`. `all-complete` never fires. §5.8's "a takeover is woken" does not cover this wake.

Fix. Drive the procedure from the board, not from those two wakes. On every chain wake, including the artifact's bare complete and `idle-board` for that review: if `A` is live, `R` is unrejected, and a release tombstone of `R` has that note, post the rebase unless a fix of `A` is already neither completed nor rejected, then reject `R`. A later master then does this from the artifact's complete wake.

### Major

#### M1. Rejecting a chain fix always runs the orphan handler on the artifact

§5.6 skips replacement for a chain task (step 2) and then runs steps 1–4 before `reviews.py`. The roles spec's step 3 runs exactly when step 2 posted no replacement (`2026-09-26-roles-and-install.md` §9; `skills/swarm-master/SKILL.md` line 168): if `T` fixes `A` and no other fix of `A` is open, claimed, blocked, or awaiting acceptance, review `A` now or reject it and cascade. Chain fixes stay unaccepted until the final merge, so only a later completed fix suppresses this. The first revise has no such sibling.

N=2, findings. Worker 2's review is completed. Worker 1 claims the revise and rejects it. Step 2 posts nothing, so step 3 runs. Rejecting `A` there cascades the chain, and the next revise that `reviews.py` would post is never posted. Reviewing `A` there nests the complete handler inside the reject handler, then the outer `reviews.py` runs as well.

Fix. For a chain task of a live artifact, skip step 3 as well as step 2. After the cascade-note check, the only action is `reviews.py`.

#### M2. §2 stops the master on the skip that §5.9 says reaches all-complete

§2: after `--cascade`, a printed skip means stop and report those ids. §5.6: a skipped live claim on a rejected artifact is not a failure, and the derived wake returns when the claim ends. §5.9's rehearsal is that the claimed review completes, the derived wake rejects it, and `all-complete` is reached. §5.5 keeps that wake from firing while the claim is live, so waiting does not spin. Stopping does: nobody is seated when the review completes.

Two workers. The artifact is rejected while one review is claimed. `--cascade` prints `skipped <R> held by <agent>`. The master that follows §2 stops. The worker completes the review. That complete stays unaccepted until some later master joins; the seated goal never reaches `all-complete`.

Fix. In the artifact reject handler, a skip of a chain task is not the §2 stop; go back to wait. Keep §2's stop for an `after` dependent that is still claimed, because that wake repeats every tick. Have the skip line identify a chain task so the two are not the same rule.

### Minor

#### m1. A replacement either copies `min_reviews` or recomputes it from the profile

§5.1: an artifact a later master posts uses the profile. §5.6: a replacement copies the rejected artifact's `kind` and `min_reviews`. The goal phrase is not on the board (§5.1). Profile `spec: 2`, goal `reviews: spec=3`, the posted spec has `min_reviews` 3. A takeover that still wants the work and follows §5.1 posts the replacement at 2. `inbox-add` allows it, because 2 is not below the profile. One required round is gone.

Fix. §5.1: a replacement copies `kind` and `min_reviews` from the rejected task. The profile is the floor only for an artifact that is not a copy.

#### m2. The review complete line drops `--result-ref` and the worker treats that as a lost lease

§5.7 step 5 is `complete --verdict clean|findings`. `complete` without `--result-ref` raises `ClaimDenied` (`claim.py` lines 246–247) and the CLI exits 2 (`cli.py` lines 52–54). The worker skill treats exit 2 from `complete` as a lost lease and does not complete (`skills/swarm-worker/SKILL.md` lines 115–117). §5.5's exit 1 applies to a bad `--verdict`, not to a missing `--result-ref`. Nothing is written, the claim is still held, and the worker stops working on it until the lease expires.

Fix. The review complete line is the existing `complete` command plus `--verdict clean|findings`.

#### m3. The merge arm has no `badsha`, `dirty`, or `error` outcome

§5.6 step 1 runs the `OUTCOME=` block and then specifies pass, conflict, falls short, and not worth pursuing. The block also prints `badsha`, `dirty`, and `error` (`skills/swarm-master/SKILL.md` lines 81–117). Those arms are not in §5.6. On `badsha` the head commit is missing, the complete is now seen, and neither this master nor a takeover is told to reject `<A>` or report. The chain stays completed and unaccepted. Skill step 6 would reject `<T>`, and this arm sets `<T>` to `<A>`, so a missing review commit would reject the artifact if that step ran.

Fix. Name the three outcomes in the merge arm, with `<T>` as `<A>`: `dirty` as skill step 7, `error` as skill step 8, and `badsha` as reject `<A>` with the missing-commit note, then the artifact cascade.

---

## 18. Dispositions (revision 7, 2026-09-27)

Each finding was checked against §5, the roles spec and the code before it was folded.

| Finding | Disposition | Where |
|---|---|---|
| C1. The pass re-check compares two different SHA strings and never fast-forwards | Accepted. The `OUTCOME=` line's `SHA=` is `rev-parse` output, the full id, while `reviews.py` prints the short sha from `result_ref`. The re-check now compares `HEAD=` and `SHA=` with the `reviews.py` line that entered the arm, and says the `OUTCOME=` value is not compared. | §5.6 `merge` step 2, §5.9 |
| C2. A takeover leaves a review that cannot build open, and the chain waits forever | Accepted, with a helper rather than a handler rule. `seed_state` marks `release` tombstones seen and the message cursor starts at the newest message, so neither announcement reaches a new master. `reviews.py` now finds such a review from its `release` tombstone. It returns `post-rebase` (with `REVIEW=` and the note's sha) when no fix was posted since, and `reject-review` when the rebase was posted before a crash. The release wake, the message, an `idle-board` wake and a takeover's first chain wake all go through it. | §5.4, §5.6, §5.9 |
| M1. Rejecting a chain fix always runs the orphan handler on the artifact | Accepted. For a chain task of a live artifact, the reject handler runs step 1, skips steps 2 and 3, and runs step 4 and then `reviews.py`. | §5.6 reject handler, §5.9 |
| M2. §2 stops the master on the skip that §5.9 says reaches all-complete | Accepted. `--cascade` marks a skipped chain task `(chain of <A>)`. That skip is not §2's stop, and the master goes back to wait. §2's stop stays for a skipped `after` dependent. | §5.5, §5.6, §5.9 |
| m1. A replacement either copies `min_reviews` or recomputes it from the profile | Accepted. A replacement copies `kind` and `min_reviews`. The profile applies to any other artifact a later master posts. | §5.1, §5.6 |
| m2. The review complete line drops `--result-ref` | Accepted. The review uses the worker's usual `complete` command with `--verdict` added. | §5.7 |
| m3. The merge arm has no `badsha`, `dirty`, or `error` outcome | Accepted in part. `dirty` and `error` follow the skill's §6 steps 7 and 8 with `T=<A>`. Declined for `badsha`: rejecting `<A>`. At `merge` the head is always a review, so a missing commit means that review is bad, not the artifact. The master rejects the head review, and its reject wake runs `reviews.py` to post the round again. Rejecting `A` would drop a reviewed artifact for one reviewer's bad `result_ref`. | §5.6 `merge` steps 7 and 8, §5.9 |

---

## 19. Grok review of §5 (2026-09-27) — `2c044c7`

**Reviewer:** Grok Build (`grok-4.7`, high effort), a fresh read-only run through the grok-build bridge over all of §5 at `2c044c7`, with the same brief as §17. Recorded as given, without its preamble.

Revision 7 needs another pass. The helper rows for a review that cannot build are the right board test, but the `complete` procedure that actually runs on a takeover's first wake has no arm for either new value, so that wake does not post the rebase.

What holds: With two workers, an author is refused every review and a reviewer, including one who only released, is refused every fix, so N=1 and N=2, findings and clean, each have a legal claimant and one reviewer can take every round. `post-review` and `post-revise` still appear only while no chain task is open, so a repeated complete does not post a second round. A thin review rejected in the complete handler is resumed by its reject tombstone. Rejecting the artifact cascades the chain, a `(chain of <A>)` skip goes back to wait, and the derived wake rejects a review that completes afterward. The merge arm fills `<T>` with the artifact, compares the two `reviews.py` lines rather than the full id on `OUTCOME=`, and the conflict body carries that full id and messages the author. A rejected revise skips the orphan step. `complete --verdict` stays exit 1, on the worker's usual `complete` line. `--kind` cannot go below the profile that the posting command loads, and the goal phrase is not read from the message log.

### Major

#### M1. The artifact's complete wake does not act on `post-rebase` or `reject-review`

§5.4 returns those values from a `release` tombstone, and §5.9 says a takeover posts the rebase from the artifact's unaccepted `complete`. The procedure on that wake is the case list at §5.6: `done`, `wait`, `post-review`, `post-revise`, and `merge`. `post-rebase` and `reject-review` are not in it. Their actions sit under the message, the `release` wake, and `idle-board` (§5.6). The takeover sentence there says the new master gets the `NEXT`; it does not add those arms to the `complete` switch. A takeover seeds every `release` and starts the message cursor at the newest message (`state.py` lines 40–54), so those two triggers are already gone. `tick` reports the bare `complete` first (`waiter.py` lines 106–110).

N=1, two workers. Worker 2 releases `R` with `review <R> cannot build on <sha>: conflict` and the seated master dies before handling it. The next master's first wake is `task-finished <A> complete`. `reviews.py` prints `NEXT=post-rebase REVIEW=<R> SHA=<sha>`. Nothing in the `complete` switch posts the rebase or rejects `R`. Worker 1 is offered `R` and refused as the author; that offer is marked seen. Worker 2 is not re-offered a task it released. The same switch drops `reject-review` when the rebase was posted before the crash and has even completed: the row is ahead of the head rows, so the rebase's own `complete` does not post the next round either.

The only trigger still ahead is `idle-board`, once, after `idle_board_after` (`waiter.py` lines 126–135). That wake does run the arms. A master that stops on an unmatched `NEXT`, as it stops on other helper results it cannot place, never reaches it. If it waits, posts from `idle-board`, and dies after that wake is marked seen but before the post, a state-kept restart has the `complete` already seen and `idle-board` already reported, and neither worker can take `R`.

Fix. On the `complete` wake, `post-rebase` and `reject-review` run the same steps as the release wake: post the rebase from `SHA` unless one is already posted, then reject `REVIEW`. Do this before "read the head", so an earlier clean review is not rejected on a wake whose `NEXT` is about the open release.

### Minor

#### m1. The profile floor refuses the replacement copy it is supposed to keep

§5.1 and §5.6 say a replacement copies `kind` and `min_reviews`, whatever the profile says now. §5.5 refuses `--kind K` with `--min-reviews` below the profile the command loads, and that profile is `RIP_SWARM_PROFILE` or `default` (`profile.py` lines 43–56). The original profile name is not on the board.

`profiles/default.yaml` has `spec: 2`. The operator starts the posting master with `RIP_SWARM_PROFILE=quick` and `quick` has `spec: 0`, which §5.1 gives as the way to run fewer reviews. The spec is stored with `min_reviews` 0. It is later rejected and the work is still wanted. A takeover started without that variable copies `--kind spec --min-reviews 0`. `inbox-add` exits 1, `min_reviews for spec is at least 2 (profile)`. The reject handler stops on a failed post (master skill line 171), before the cascade. Every later master on the default profile hits the same refusal, so the replacement is never posted.

Fix. In §5.5, a post that copies an existing artifact's `kind` and `min_reviews` is not subject to the floor. The floor applies only to a number the master chose for a new artifact.

### Nit

#### n1. The re-check says `HEAD=` is a short sha

§5.6 merge step 2: the second `reviews.py` line must match the entering line's `HEAD=` and `SHA=`, then "Both are the head's short sha." `HEAD=` is the task id (§5.4). `SHA=` is the short sha. The field compare itself matches on a clean chain. The sentence describes `HEAD=` as a sha, which is the confusion the re-check was rewritten to avoid.

Fix. Say both lines' `SHA=` values are the head's short sha, and `HEAD=` is the head's task id.

---

## 20. Dispositions (revision 8, 2026-09-27)

| Finding | Disposition | Where |
|---|---|---|
| M1. The artifact's complete wake does not act on `post-rebase` or `reject-review` | Accepted. The `complete` wake's arms now include `post-rebase` and `reject-review`, checked before the head is read. Every chain wake (complete, reject, release, message, `idle-board`) uses the same arms. | §5.6, §5.9 |
| m1. The profile floor refuses the replacement copy it is supposed to keep | Accepted, narrowly. The floor exempts only an exact copy. The post's title ends in ` (replaces <X>)`, `X` is rejected, and `X` has the same `kind` and exactly that `min_reviews`. The helper checks all of it on the board, so a slip still cannot post a new artifact below the profile. | §5.5, §5.6, §5.9 |
| n1. The re-check says `HEAD=` is a short sha | Accepted. `HEAD=` is the head's task id, and `SHA=` its short sha. | §5.6 `merge` step 2 |
