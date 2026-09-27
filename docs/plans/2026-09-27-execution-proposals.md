# Execution proposals: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** implement the four execution proposals: a one-publish reject cascade, a heartbeat loop that follows the profile, no completion message, and minimum independent review rounds per artifact kind that the helpers enforce.

**Architecture:** every mechanism is a deterministic helper plus the skill text that drives it. The helpers stay stdlib-only and read everything from the board (inbox files, claim tombstones, acceptance records). One new module, `rip_swarm/reviews.py`, decides a reviewed artifact's next step; `claim`, `complete`, `accept`, `reject --cascade`, the derived wake and `status` consult the same chain model. The role skills (`/swarm-master`, `/swarm-worker`) get the new arms, and the model-free rehearsal proves them line for line.

**Tech Stack:** Python 3.10+ stdlib only; git ≥ 2.31; `unittest`; real git against temporary bare remotes in tests.

**Spec:** `docs/specs/2026-09-26-execution-proposals.md` at revision 9 (`a8c5138`). Read §1–§6. §7–§22 are the review history; the dispositions explain why each rule exists. The roles spec `docs/specs/2026-09-26-roles-and-install.md` is the base the proposals change.

## Global Constraints

- Python 3.10+, standard library only. No new dependencies.
- Test command: `PYTHONPATH=skills/rip-swarm python3 -m unittest discover -s tests`. The full suite takes about 2.5 minutes; run it with a 600-second timeout. It must pass at the end of every task. During a task, run only the test files you touch.
- TDD: write the failing test, run it and see it fail, implement, run it and see it pass, commit.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Work on branch `feat/min-reviews`. Never push, never force.
- Exit codes: `0` ok, `1` failure or usage error, `2` refusal (`ClaimDenied`), `3` a `wait` (or, new, a heartbeat loop) is already running for this agent.
- Hive writes go through `publish` with an allowlist. Never `git add -A` in the hive.
- Inbox fields are create-only. New fields: `kind`, `min_reviews`, `reviews` (spec §5.2). New tombstone field: `verdict` on a review's `complete` tombstone.
- Exact strings (spec §2, §5.4, §5.5, §5.7). Copy them verbatim:

  | Where | Text |
  |---|---|
  | cascade note | `dependency <T> rejected` |
  | cascade skip | `skipped <id> held by <agent> until <expires_at>` |
  | cascade chain skip | `skipped <id> (chain of <A>) held by <agent> until <expires_at>` |
  | `reviews.py` line | `NEXT=<post-review\|post-revise\|post-rebase\|reject-review\|merge\|wait\|done> ARTIFACT=<A> ROUNDS=<k>/<N> HEAD=<id> SHA=<sha> CHAIN=<id,id,…> [REVIEW=<id>]` |
  | floor refusal | `min_reviews for <K> is at least <n> (profile)` |
  | author refused a review | `<agent> wrote part of <A>; its review must come from another agent` |
  | reviewer refused a fix | `<agent> reviewed <A>; its fixes must come from another agent` |
  | chain of a rejected artifact | `<A> is rejected` |
  | accept not ready | `<A> needs <N> review rounds ending clean, has <k>` |
  | cannot-build note | `review <id> cannot build on <sha>: conflict` |
  | status artifact | `(spec, reviews 1/2)` |
  | status review task | `(reviews <A>)` |

- The profile key is `min_reviews`, a map of kind to integer ≥ 0. `DEFAULT_PROFILE` and the template ship `spec: 0`, `plan: 0`, `implementation: 0`. `reviews_required_per_plan` stays advisory and unread.
- With every `min_reviews` at 0 or absent, every path is exactly today's (spec §5.8).

## Rulings made while writing this plan

The spec is silent on these. Each is the smallest choice that keeps its rules implementable.

1. **`publish` takes a callable allowlist.** The cascade learns which tasks it tombstones only inside the op, after the fetch. `publish(allow=callable)` calls it after the op, before the commit. A list still works as before.
2. **`reject --cascade` refuses `--note`** (exit 1). The cascade writes its own note, and a second note would be silently dropped.
3. **A corrupt claim in the cascade walk refuses the whole cascade** (exit 2), before anything is written, as `master_reject` refuses one today.
4. **The heartbeat loop keeps a pid file and has `--stop`.** Skills must stop the loop "on every path" (spec §3), across fresh shells and compaction, on any harness. `claim.py heartbeat --task X --agent A --stop` stops the loop for `X` in this clone. A second `--loop` for the same task exits 3. A failure other than `ClaimDenied` (a fetch or push error) is printed and retried after 30 seconds, so a network blip does not end the loop while the lease is alive.
5. **A master's reject does not make it a reviewer.** Reviewers are the agents that claimed a review (spec §5.3). A master-written reject tombstone (it has `"action": "reject"`) is not a claim, so it is skipped when collecting reviewers.
6. **`--verdict` is validated by the library, not argparse**, so a bad value exits 1, never argparse's exit 2, which the worker skill reads as a lost lease.
7. **No head prints `HEAD=none SHA=none`.**
8. **"A fix posted since that release"** means a fix whose `created_at` is at or after the release tombstone's stamp.
9. **The master reads the profile number through the helper.** It posts `--kind K --min-reviews <phrase N>`. A refusal (`min_reviews for K is at least n (profile)`) means the profile's number is larger, and it posts again with `--kind K` alone, which takes the profile's number. That is the "larger of" rule without the model parsing YAML.
10. **No version bump.** The spec does not ask for one; `0.3.0` stays.

## Review Focus

These are the inputs most likely to break for a real user that no spec test names. Each has a test in the task listed.

1. **A worker types `--verdict Clean`** (capital C) or forgets `--verdict`. It exits 1 with a message naming `clean` and `findings`, nothing is written, and the claim is still held, so the worker can run `complete` again. Test in Task 5.
2. **Two `release` tombstones of one review in the same second** (`…release.<stamp>.json` and `…release.<stamp>-2.json`). The "latest" release is the `-2` one, although it sorts first by name. Test in Task 5.
3. **A heartbeat loop started twice**, for example after compaction. The second exits 3 and one loop keeps running; `--stop` stops it. Test in Task 2.
4. **`reject --cascade` on a task nothing waits on.** Exit 0, it prints `nothing waits on <T>`, and no hive commit is made. Test in Task 1.
5. **A profile with `min_reviews: {spec: two}`.** `inbox-add --kind spec` exits 1 with a message naming the key, not a traceback, and `inbox-add` without `--kind` still works. Test in Task 4.

## File map

| Path | Responsibility | Tasks |
|---|---|---|
| `skills/rip-swarm/rip_swarm/gitops.py` | `publish` accepts a callable allowlist | 1 |
| `skills/rip-swarm/rip_swarm/acceptance.py` | `cascade_reject`; `accept` readiness | 1, 6, 7 |
| `skills/rip-swarm/rip_swarm/board.py` | `downstream` walk; `TaskView` fields `kind`, `min_reviews`, `reviews`; `artifact_of`, `chain`, `posting_order` | 1, 4, 7 |
| `skills/rip-swarm/rip_swarm/lease.py` (new) | heartbeat loop, its pid file, `--stop` | 2 |
| `skills/rip-swarm/rip_swarm/profile.py` | `min_reviews` default and `min_reviews_floors` | 4 |
| `skills/rip-swarm/templates/_swarm/profiles/default.yaml` | `min_reviews` zeros | 4 |
| `skills/rip-swarm/rip_swarm/inbox.py` | `--kind`, `--min-reviews`, `--reviews` rules and the floor | 4 |
| `skills/rip-swarm/rip_swarm/reviews.py` (new) | chain state, head, rounds, readiness, next step, authors, reviewers | 5 |
| `skills/rip-swarm/scripts/reviews.py` (new) | script entry for `reviews` | 5 |
| `skills/rip-swarm/rip_swarm/claim.py` | `complete --verdict`; independence and rejected-artifact refusals in `try_claim` | 5, 6 |
| `skills/rip-swarm/rip_swarm/waiter.py` | derived reject wake for a rejected artifact's chain | 7 |
| `skills/rip-swarm/rip_swarm/status.py` | `(spec, reviews 1/2)` and `(reviews <A>)` | 7 |
| `skills/rip-swarm/rip_swarm/cli.py` | flags `--cascade`, `--loop`, `--stop`, `--kind`, `--min-reviews`, `--reviews`, `--verdict`; `reviews` command | 1, 2, 4, 5 |
| `skills/swarm-master/SKILL.md` | cascade command, heartbeat loop, review rounds, chain arms | 3, 8 |
| `skills/swarm-worker/SKILL.md` | heartbeat loop, no result message, review tasks | 3, 8 |
| `skills/rip-swarm/SKILL.md` | reference for the new flags and `reviews.py` | 3, 8 |
| `tests/test_cascade.py` (new) | cascade unit and publish tests | 1, 7 |
| `tests/test_lease.py` (new) | heartbeat loop | 2 |
| `tests/test_reviews.py` (new) | verdicts, `reviews.py` table, claim and accept guards, chain cascade, wake, status | 5, 6, 7 |
| `tests/test_inbox.py`, `tests/test_profile.py` | field rules and the profile | 4 |
| `tests/test_packaging.py` | skill needles | 3, 8 |
| `tests/test_rehearsal.py` | model-free rehearsal of the chain | 3, 9, 10 |
| `docs/specs/2026-09-26-roles-and-install.md`, `README.md`, `templates/_swarm/PROTOCOL.md`, the execution-proposals spec status | docs | 11 |

---
### Task 1: `reject --cascade` over `after`, in one publish (spec §2)

**Files:**
- Modify: `skills/rip-swarm/rip_swarm/gitops.py` (`publish`, `publish_or_apply`)
- Modify: `skills/rip-swarm/rip_swarm/board.py` (add `downstream`)
- Modify: `skills/rip-swarm/rip_swarm/acceptance.py` (split `master_reject`, add `cascade_reject`)
- Modify: `skills/rip-swarm/rip_swarm/cli.py` (`reject --cascade`)
- Create: `tests/test_cascade.py`
- Modify: `tests/test_gitops.py` (one test)

**Interfaces:**
- Produces: `board.downstream(board: dict[str, TaskView], root: str) -> list[tuple[str, str | None]]`: every task a reject of `root` cascades to, level by level, sorted within a level; the second item is the artifact whose chain the task is in (always `None` until Task 7).
- Produces: `acceptance.cascade_reject(hive, *, agent: str, task_id: str, now: datetime) -> dict` returning `{"root": T, "rejected": [ids], "already": [ids], "skipped": [{"task_id", "agent", "expires_at", "chain_of"}]}`. Raises `ClaimDenied` when the caller does not hold the baton, when `T` is not rejected, or when a walked task has a corrupt claim.
- Produces: `gitops.publish(..., allow=callable)`: a callable allowlist is called after the op.
- CLI: `claim.py reject --hive H --task T --agent A --cascade` prints one line per task: `rejected <id>`, `already rejected <id>`, `skipped <id> held by <agent> until <expires_at>`, or `nothing waits on <T>` when there is none.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cascade.py`:

```python
# tests/test_cascade.py — the one-publish reject cascade (execution proposals §2, §5.5)
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hivekit import T0, git, local_hive, make_project, remote_files
from rip_swarm.acceptance import cascade_reject, master_reject
from rip_swarm.board import list_tombstones, read_board
from rip_swarm.claim import ClaimDenied, _create_claim
from rip_swarm.cli import main
from rip_swarm.inbox import create_task
from rip_swarm.join import join
from rip_swarm.orchestrator import promote
from rip_swarm.timeutil import add_seconds


def cli(*argv, at=T0):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), \
            mock.patch("rip_swarm.cli.now_utc", return_value=at):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue().strip(), err.getvalue().strip()


class TestCascadeReject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))
        promote(self.hive, agent="alice", harness="claude-code", now=T0, lease_seconds=1800,
                reason="master", allow_self_promote=False, operators=["op"], by="op")

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, title, **kw):
        return create_task(self.hive, title=title, created_by="alice", now=T0, **kw)["id"]

    def _actions(self, tid):
        return sorted(s.action for s in list_tombstones(self.hive) if s.task_id == tid)

    def _notes(self, tid):
        return [s.doc().get("note") for s in list_tombstones(self.hive)
                if s.task_id == tid and s.action == "reject"]

    def test_walks_after_to_the_end_in_one_call(self):
        t = self._task("t")
        d = self._task("d", after=[t])
        e = self._task("e", after=[d])
        f = self._task("f", after=[t, e])          # waits on a task already in the walk
        u = self._task("u")                        # unrelated
        master_reject(self.hive, agent="alice", task_id=t, note="drop", now=T0)
        doc = cascade_reject(self.hive, agent="alice", task_id=t, now=T0)
        self.assertEqual(doc["root"], t)
        self.assertEqual(sorted(doc["rejected"]), sorted([d, e, f]))
        self.assertEqual((doc["already"], doc["skipped"]), ([], []))
        for tid in (d, e, f):
            self.assertEqual(self._notes(tid), [f"dependency {t} rejected"])
        self.assertFalse(read_board(self.hive, T0)[u].rejected)

    def test_a_second_run_writes_nothing(self):
        t = self._task("t")
        d = self._task("d", after=[t])
        e = self._task("e", after=[d])
        master_reject(self.hive, agent="alice", task_id=t, note="drop", now=T0)
        cascade_reject(self.hive, agent="alice", task_id=t, now=T0)
        before = len(list_tombstones(self.hive))
        again = cascade_reject(self.hive, agent="alice", task_id=t, now=T0)
        self.assertEqual(again["rejected"], [])
        self.assertEqual(sorted(again["already"]), sorted([d, e]))
        self.assertEqual(len(list_tombstones(self.hive)), before)

    def test_a_live_claim_is_skipped_and_an_expired_one_tombstoned_first(self):
        t = self._task("t")
        d = self._task("d", after=[t])
        e = self._task("e", after=[t])
        # A dependent of a rejected task cannot be claimed through `claim` (it is
        # blocked), so the claims are planted below the refusal checks.
        _create_claim(self.hive, d, "bob", "grok", T0, 900)                     # live
        _create_claim(self.hive, e, "bob", "grok", add_seconds(T0, -1000), 900)  # expired
        master_reject(self.hive, agent="alice", task_id=t, note="drop", now=T0)
        doc = cascade_reject(self.hive, agent="alice", task_id=t, now=T0)
        self.assertEqual(doc["rejected"], [e])
        self.assertEqual(doc["skipped"], [{"task_id": d, "agent": "bob",
                                           "expires_at": "2026-09-26T10:15:00Z",
                                           "chain_of": None}])
        self.assertEqual(self._actions(e), ["expired", "reject"])
        self.assertEqual(self._actions(d), [])

    def test_refused_to_a_worker_and_for_a_live_root(self):
        t = self._task("t")
        self._task("d", after=[t])
        with self.assertRaisesRegex(ClaimDenied, "does not hold a live orchestrator baton"):
            cascade_reject(self.hive, agent="bob", task_id=t, now=T0)
        with self.assertRaisesRegex(ClaimDenied, f"{t} is not rejected"):
            cascade_reject(self.hive, agent="alice", task_id=t, now=T0)

    def test_a_corrupt_claim_refuses_before_anything_is_written(self):
        t = self._task("t")
        d = self._task("d", after=[t])
        e = self._task("e", after=[t])
        (self.hive / "claims" / f"{d}.json").write_text("{", encoding="utf-8")
        master_reject(self.hive, agent="alice", task_id=t, note="drop", now=T0)
        with self.assertRaisesRegex(ClaimDenied, f"{d} has a corrupt claim file"):
            cascade_reject(self.hive, agent="alice", task_id=t, now=T0)
        self.assertEqual(self._actions(e), [])


class TestCascadeCli(unittest.TestCase):
    """The cascade on a real hive clone: one commit, widened allowlist."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin, repo = make_project(Path(self.tmp.name))
        self.m = join(repo, role="master", harness="grok", now=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, title, *extra):
        rc, out, err = cli("inbox-add", "--hive", self.m.hive, "--created-by", self.m.agent,
                           "--title", title, *extra)
        self.assertEqual(rc, 0, err)
        return out.split()[1].rstrip(":")

    def reject(self, task, *extra):
        return cli("reject", "--hive", self.m.hive, "--task", task, "--agent", self.m.agent, *extra)

    def commits(self):
        return int(git(self.origin, "rev-list", "--count", "swarm"))

    def test_the_cascade_is_one_hive_commit(self):
        t = self.post("T")
        d = self.post("D", "--after", t)
        e = self.post("E", "--after", d)
        self.assertEqual(self.reject(t, "--note", "drop")[0], 0)
        before = self.commits()
        rc, out, err = self.reject(t, "--cascade")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(out.splitlines()), sorted([f"rejected {d}", f"rejected {e}"]))
        self.assertEqual(self.commits(), before + 1)
        files = remote_files(self.origin)
        for tid in (d, e):
            self.assertTrue(any(f.startswith(f"claims/{tid}.reject.") for f in files), tid)
        rc, out, _ = self.reject(t, "--cascade")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(out.splitlines()),
                         sorted([f"already rejected {d}", f"already rejected {e}"]))
        self.assertEqual(self.commits(), before + 1)

    def test_nothing_waits_on_the_task(self):
        t = self.post("T")
        self.assertEqual(self.reject(t, "--note", "drop")[0], 0)
        before = self.commits()
        self.assertEqual(self.reject(t, "--cascade")[:2], (0, f"nothing waits on {t}"))
        self.assertEqual(self.commits(), before)

    def test_refusals(self):
        t = self.post("T")
        self.post("D", "--after", t)
        rc, _, err = self.reject(t, "--cascade")
        self.assertEqual(rc, 2)
        self.assertIn(f"{t} is not rejected", err)
        rc, _, err = self.reject(t, "--cascade", "--note", "x")
        self.assertEqual(rc, 1)
        self.assertIn("--cascade writes its own note", err)
```

In `tests/test_gitops.py`, add this test to `TestGitops`, right after `test_op_writing_outside_allowlist_is_refused_and_nothing_is_pushed`:

```python
    def test_a_callable_allowlist_is_read_after_the_op(self):
        from rip_swarm.gitops import GitopsError

        written = []

        def op():
            name = "lookback/late.md"
            (self.ha / "lookback").mkdir(exist_ok=True)
            (self.ha / name).write_text("x\n", encoding="utf-8")
            written.append(name)
            return {}

        publish(self.ha, task_id="__none__", op=op, message="late", agent="alice",
                now=T0, allow=lambda: list(written))
        _git(self.ha, "fetch")
        self.assertIn("lookback/late.md", self._remote_tree(self.ha))

        def leak():
            (self.ha / "lookback" / "other.md").write_text("y\n", encoding="utf-8")
            return {}

        with self.assertRaises(GitopsError):
            publish(self.ha, task_id="__none__", op=leak, message="leak", agent="alice",
                    now=T0, allow=lambda: ["lookback/late.md"])
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_cascade tests.test_gitops -v 2>&1 | tail -15`
Expected: `ImportError: cannot import name 'cascade_reject'`, and the gitops test fails because `tuple(allow)` of a function raises `TypeError`.

- [ ] **Step 3: Make `publish` accept a callable allowlist**

In `skills/rip-swarm/rip_swarm/gitops.py`, change the `publish` signature line `allow: Iterable[str] | None = None,` to:

```python
    allow: Iterable[str] | Callable[[], Iterable[str]] | None = None,
```

Add to its docstring, after the sentence that ends "raises GitopsError: see `_commit_op`.":

```text
    A callable `allow` is called after the op, for an op that learns its own
    write set only once it has read the fetched tree (the reject cascade).
```

Replace the line `patterns = tuple(default_allow(task_id, agent) if allow is None else allow)` with:

```python
    fixed = None if callable(allow) else tuple(
        default_allow(task_id, agent) if allow is None else allow
    )
```

and, in the `try:` block after the op, replace `_commit_op(hive, message, patterns)` with:

```python
        _commit_op(hive, message, fixed if fixed is not None else tuple(allow()))
```

Change `publish_or_apply`'s `allow: Iterable[str] | None = None,` the same way. `Callable` is already imported in `gitops.py` (it types `op`); check the import line and add it if it is missing.

- [ ] **Step 4: Add the walk**

In `skills/rip-swarm/rip_swarm/board.py`, after `fixers`:

```python
def downstream(board: dict[str, TaskView], root: str) -> list[tuple[str, str | None]]:
    """Every task a reject of `root` cascades to: the tasks that wait on it
    through `after`, directly or further down, including a task that waits on
    several tasks of the walk (execution proposals §2). Level by level, sorted
    within a level. The second item names the artifact whose review chain the
    task is in; it is None for an `after` dependent."""
    seen, out, frontier = {root}, [], [root]
    while frontier:
        level = sorted(tid for tid, view in board.items()
                       if tid not in seen and any(dep in seen for dep in view.after))
        out.extend((tid, None) for tid in level)
        seen.update(level)
        frontier = level
    return out
```

- [ ] **Step 5: Split `master_reject` and add `cascade_reject`**

In `skills/rip-swarm/rip_swarm/acceptance.py`, change the board import to `from rip_swarm.board import downstream, is_accepted, read_board`, then replace `master_reject` with:

```python
def master_reject(
    hive: Path, *, agent: str, task_id: str, note: str | None, now: datetime
) -> dict:
    """The baton holder drops a task that has no live claim (spec §7.4)."""
    _require_task(hive, task_id)
    rec = _require_master(hive, agent, now)
    view = read_board(hive, now)[task_id]
    if view.rejected:
        return {"task_id": task_id, "already": True}
    if view.accepted:
        raise ClaimDenied(f"{task_id} is already accepted; it cannot be rejected")
    held = active_holder(hive, task_id, now)
    if isinstance(held, Holder):
        raise ClaimDenied(
            f"{task_id} is held by {held.agent} until {format_z(held.expires_at)}; "
            "the holder must release it or it must expire first"
        )
    if isinstance(held, Corrupt):
        raise ClaimDenied(f"{task_id} has a corrupt claim file: {held.error}")
    return _write_reject(hive, rec=rec, agent=agent, task_id=task_id, note=note, now=now,
                         expired=isinstance(held, Expired))


def _write_reject(
    hive: Path, *, rec: dict, agent: str, task_id: str, note: str | None,
    now: datetime, expired: bool,
) -> dict:
    """One master reject tombstone for a task nobody holds. An expired claim is
    tombstoned `expired` first."""
    path = HivePaths(hive).claim(task_id)
    if expired:
        old = read_json(path)
        tombstone_claim(path, "expired", now)
        append_claim_audit(hive, action="expired", claim_doc=old, now=now)
    body = {
        "task_id": task_id,
        "claim_id": new_claim_id(now),
        "agent": agent,
        "harness": rec["harness"],
        "action": "reject",
        "note": note,
        "at": format_z(now),
        "expires_at": format_z(now),
    }
    for dest in _tombstone_candidates(path, "reject", now):
        if write_json_to_new_path(dest, body):
            break
    append_claim_audit(hive, action="reject", claim_doc=body, now=now)
    return body


def cascade_reject(hive: Path, *, agent: str, task_id: str, now: datetime) -> dict:
    """Reject, in one call, every task a reject of `task_id` cascades to
    (execution proposals §2): each gets the note `dependency <T> rejected`.
    A task with a live claim is skipped, not failed; an accepted one is left
    alone; one already rejected is reported as such. Every walked task is
    checked before anything is written, so a corrupt claim refuses the whole
    cascade."""
    _require_task(hive, task_id)
    rec = _require_master(hive, agent, now)
    board = read_board(hive, now)
    root = board.get(task_id)
    if root is None:
        raise ClaimDenied(f"inbox task {task_id} is unreadable")
    if not root.rejected:
        raise ClaimDenied(f"{task_id} is not rejected; reject it before --cascade")
    todo: list[tuple[str, bool]] = []
    already: list[str] = []
    skipped: list[dict] = []
    for tid, chain_of in downstream(board, task_id):
        view = board[tid]
        if view.rejected:
            already.append(tid)
            continue
        if view.accepted:
            continue
        held = active_holder(hive, tid, now)
        if isinstance(held, Holder):
            skipped.append({"task_id": tid, "agent": held.agent,
                            "expires_at": format_z(held.expires_at), "chain_of": chain_of})
            continue
        if isinstance(held, Corrupt):
            raise ClaimDenied(f"{tid} has a corrupt claim file: {held.error}")
        todo.append((tid, isinstance(held, Expired)))
    note = f"dependency {task_id} rejected"
    for tid, expired in todo:
        _write_reject(hive, rec=rec, agent=agent, task_id=tid, note=note, now=now,
                      expired=expired)
    return {"root": task_id, "rejected": [tid for tid, _ in todo],
            "already": already, "skipped": skipped}
```

- [ ] **Step 6: Wire `--cascade` into the CLI**

In `skills/rip-swarm/rip_swarm/cli.py`:

1. Import: `from rip_swarm.acceptance import accept_task, cascade_reject, holds_baton, master_reject`.
2. In `_parser`, the `release`/`reject` loop builds both parsers. After the loop add:

```python
    sub.choices["reject"].add_argument(
        "--cascade", action="store_true",
        help="baton holder: reject every task that waits on rejected --task, in one publish",
    )
```

3. In `_reject`, right after `_resolve_harness(hive, agent, args.harness)`, add:

```python
    if args.cascade:
        return _reject_cascade(args, hive, now, task_id, agent)
```

4. Add after `_reject`:

```python
def _reject_cascade(
    args: argparse.Namespace, hive: Path, now: datetime, task_id: str, agent: str
) -> str:
    """Execution proposals §2: one publish for the whole cascade. The op learns
    which tasks it tombstones only after the fetch, so the allowlist is a
    callable over what it wrote: each tombstoned task's claim paths, plus the
    audit log."""
    if args.note is not None:
        raise ValueError("--cascade writes its own note (dependency <T> rejected); drop --note")
    written: list[str] = []

    def op() -> dict:
        doc = cascade_reject(hive, agent=agent, task_id=task_id, now=now)
        written[:] = doc["rejected"]
        return doc

    def allow() -> list[str]:
        out = ["store/claims.jsonl"]
        for tid in written:
            out += [f"claims/{tid}.json", f"claims/{tid}.*.json"]
        return out

    doc = _run_op(
        hive, local=args.local, task_id="__none__", message=f"reject --cascade {task_id}",
        op=op, agent=agent, now=now, allow=allow,
    )
    return _cascade_lines(doc)


def _cascade_lines(doc: dict) -> str:
    lines = [f"rejected {tid}" for tid in doc["rejected"]]
    lines += [f"already rejected {tid}" for tid in doc["already"]]
    for skip in doc["skipped"]:
        chain = f" (chain of {skip['chain_of']})" if skip.get("chain_of") else ""
        lines.append(
            f"skipped {skip['task_id']}{chain} held by {skip['agent']} until {skip['expires_at']}"
        )
    return "\n".join(lines) or f"nothing waits on {doc['root']}"
```

5. Change `_run_op`'s `allow: list[str] | None = None,` to `allow: list[str] | Callable[[], list[str]] | None = None,` (`Callable` is already imported from `collections.abc`).

- [ ] **Step 7: Run the tests to see them pass**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_cascade tests.test_gitops tests.test_acceptance tests.test_cli -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 8: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/rip-swarm/rip_swarm/gitops.py skills/rip-swarm/rip_swarm/board.py \
  skills/rip-swarm/rip_swarm/acceptance.py skills/rip-swarm/rip_swarm/cli.py \
  tests/test_cascade.py tests/test_gitops.py
git commit -m "feat: reject --cascade rejects the dependent chain in one publish

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `heartbeat --loop` and `--stop` (spec §3)

**Files:**
- Create: `skills/rip-swarm/rip_swarm/lease.py`
- Modify: `skills/rip-swarm/rip_swarm/cli.py`
- Create: `tests/test_lease.py`

**Interfaces:**
- Produces: `lease.lease_ttl(profile: dict, task_id: str) -> int` (`orchestrator_lease_ttl` for `orchestrator`, else `worker_lease_ttl`, in seconds).
- Produces: `lease.heartbeat_loop(hive, task_id, agent, *, ttl, beat, clock, sleep, out, err, max_beats=None) -> None`, where `beat(now) -> str` heartbeats once and returns the line to print. `ClaimDenied` propagates.
- Produces: `lease.acquire_loop_lock(hive, task_id) -> Path`, `lease.release_loop_lock(path)`, `lease.stop_loop(hive, task_id) -> str`, `lease.LoopRunning` (exit 3).
- CLI: `claim.py heartbeat --hive H --task X --agent A --loop` runs until killed or stopped; `claim.py heartbeat --hive H --task X --agent A --stop` stops it and prints `stopped heartbeat loop <pid> for <X>` or `no heartbeat loop running for <X>`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lease.py`:

```python
# tests/test_lease.py — the heartbeat loop (execution proposals §3)
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, local_hive
from rip_swarm.claim import ClaimDenied, heartbeat, try_claim
from rip_swarm.cli import main
from rip_swarm.gitops import GitopsError
from rip_swarm.inbox import create_task
from rip_swarm.lease import (
    LoopRunning, acquire_loop_lock, heartbeat_loop, lease_ttl, release_loop_lock, stop_loop,
)


class Clock:
    def __init__(self, start):
        self.now, self.slept = start, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class TestHeartbeatLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))
        self.tid = create_task(self.hive, title="t", created_by="op", now=T0)["id"]
        try_claim(self.hive, self.tid, "bob", "grok", T0, 1800)
        self.beats, self.lines, self.errors = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def beat(self, now):
        self.beats.append(now)
        doc = heartbeat(self.hive, self.tid, "bob", now, 1800)
        return f"heartbeat {self.tid} until {doc['expires_at']}"

    def loop(self, clock, beat=None, max_beats=2):
        heartbeat_loop(self.hive, self.tid, "bob", ttl=1800, beat=beat or self.beat,
                       clock=clock, sleep=clock.sleep, out=self.lines.append,
                       err=self.errors.append, max_beats=max_beats)

    def test_lease_ttl_follows_the_profile(self):
        profile = {"orchestrator_lease_ttl": "10m", "worker_lease_ttl": "4m"}
        self.assertEqual(lease_ttl(profile, "orchestrator"), 600)
        self.assertEqual(lease_ttl(profile, self.tid), 240)

    def test_sleeps_until_half_the_lease_is_gone(self):
        clock = Clock(T0)
        self.loop(clock)
        self.assertEqual(clock.slept, [900, 900])
        self.assertEqual(self.beats, [T0 + timedelta(seconds=900), T0 + timedelta(seconds=1800)])
        self.assertEqual(len(self.lines), 2)

    def test_under_half_left_beats_at_once(self):
        clock = Clock(T0 + timedelta(seconds=1000))
        self.loop(clock)
        self.assertEqual(self.beats[0], T0 + timedelta(seconds=1000))
        self.assertEqual(clock.slept, [900])

    def test_a_lost_lease_ends_the_loop(self):
        clock = Clock(T0 + timedelta(seconds=1801))
        with self.assertRaisesRegex(ClaimDenied, "claim expired"):
            self.loop(clock)

    def test_other_failures_are_retried_after_30_seconds(self):
        clock = Clock(T0 + timedelta(seconds=1000))
        failures = [GitopsError("push rejected")]

        def flaky(now):
            if failures:
                raise failures.pop()
            return self.beat(now)

        self.loop(clock, beat=flaky, max_beats=1)
        self.assertEqual(clock.slept, [30])
        self.assertEqual(self.beats, [T0 + timedelta(seconds=1030)])
        self.assertIn("push rejected", self.errors[0])

    def test_a_second_loop_is_refused(self):
        lock = acquire_loop_lock(self.hive, self.tid)
        try:
            with self.assertRaises(LoopRunning):
                acquire_loop_lock(self.hive, self.tid)
        finally:
            release_loop_lock(lock)
        release_loop_lock(acquire_loop_lock(self.hive, self.tid))   # free again

    def test_stop_ends_a_running_loop(self):
        # A stand-in process whose command line carries --loop, like the real one.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--loop"])
        try:
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"stopped heartbeat loop {proc.pid} for {self.tid}")
            self.assertNotEqual(proc.wait(timeout=10), 0)
            self.assertFalse(path.exists())
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"no heartbeat loop running for {self.tid}")
        finally:
            proc.kill()
            proc.wait()

    def test_stop_never_kills_a_process_that_is_not_a_loop(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")                 # a stale, reused pid
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"no heartbeat loop running for {self.tid}")
            self.assertIsNone(proc.poll())
        finally:
            proc.kill()
            proc.wait()


class TestHeartbeatLoopCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))
        self.tid = create_task(self.hive, title="t", created_by="op", now=T0)["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch("rip_swarm.cli.now_utc", return_value=T0), \
                mock.patch("rip_swarm.cli.signal.signal"):
            rc = main([str(a) for a in argv])
        return rc, out.getvalue().strip(), err.getvalue().strip()

    def test_loop_on_a_task_not_held_exits_2_at_once(self):
        rc, _, err = self.run_cli("heartbeat", "--hive", self.hive, "--task", self.tid,
                                  "--agent", "bob", "--loop", "--local")
        self.assertEqual(rc, 2)
        self.assertIn(f"no active claim for {self.tid}", err)
        self.assertEqual(stop_loop(self.hive, self.tid),
                         f"no heartbeat loop running for {self.tid}")   # the pid file is gone

    def test_stop_with_nothing_running(self):
        rc, out, _ = self.run_cli("heartbeat", "--hive", self.hive, "--task", self.tid,
                                  "--agent", "bob", "--stop")
        self.assertEqual((rc, out), (0, f"no heartbeat loop running for {self.tid}"))
```

The CLI test passes `--local` on a `--no-git` hive; `_gate_local_on_publishable` only gates publishable hives, so no opt-in is needed.

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_lease -v 2>&1 | tail -5`
Expected: `ModuleNotFoundError: No module named 'rip_swarm.lease'`.

- [ ] **Step 3: Write `lease.py`**

Create `skills/rip-swarm/rip_swarm/lease.py`:

```python
# rip_swarm/lease.py — keep one lease alive while the agent works outside `wait`
# (execution proposals §3)
from __future__ import annotations

import os
import signal
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied
from rip_swarm.fold import Holder, active_holder
from rip_swarm.state import _alive, _read_pid, state_dir
from rip_swarm.timeutil import parse_duration

RETRY_SECONDS = 30


class LoopRunning(Exception):
    def __init__(self, task_id: str, pid: int):
        super().__init__(f"heartbeat loop already running for {task_id} (pid {pid})")
        self.pid = pid


def lease_ttl(profile: dict, task_id: str) -> int:
    key = "orchestrator_lease_ttl" if task_id == "orchestrator" else "worker_lease_ttl"
    return parse_duration(profile[key])


def heartbeat_loop(
    hive: Path,
    task_id: str,
    agent: str,
    *,
    ttl: int,
    beat: Callable[[datetime], str],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    out: Callable[[str], None],
    err: Callable[[str], None],
    max_beats: int | None = None,
) -> None:
    """Heartbeat `task_id` each time half of its lease is gone, until killed.

    The lease is re-read before every sleep, so a heartbeat from another helper
    in between only postpones the next one. `ClaimDenied` (the lease is gone)
    ends the loop. Any other failure, such as a push that did not go through,
    is printed and retried after RETRY_SECONDS while the lease is still alive.
    `max_beats` bounds the loop for tests only."""
    beats = 0
    while max_beats is None or beats < max_beats:
        now = clock()
        rec = active_holder(hive, task_id, now)
        if isinstance(rec, Holder) and rec.agent == agent:
            wait = (rec.expires_at - now).total_seconds() - ttl / 2
            if wait > 0:
                sleep(wait)
                continue
        try:
            line = beat(clock())
        except ClaimDenied:
            raise
        except Exception as e:
            err(f"heartbeat {task_id} failed, retrying in {RETRY_SECONDS}s: {e}")
            sleep(RETRY_SECONDS)
            continue
        beats += 1
        out(line)


def _pid_path(hive: Path, task_id: str) -> Path:
    return state_dir(hive) / f"rip-swarm-heartbeat-{task_id}.pid"


def _is_loop(pid: int) -> bool:
    """A stale pid file may name a pid the system has since reused. Where
    /proc exists, only a process started with --loop is ours to stop."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return b"--loop" in cmdline.split(b"\0")


def acquire_loop_lock(hive: Path, task_id: str) -> Path:
    path = _pid_path(hive, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid = _read_pid(path)
            if pid is not None and _alive(pid) and (pid == os.getpid() or _is_loop(pid)):
                raise LoopRunning(task_id, pid) from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return path
    raise LoopRunning(task_id, _read_pid(path) or 0)


def release_loop_lock(path: Path) -> None:
    if _read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)


def stop_loop(hive: Path, task_id: str) -> str:
    path = _pid_path(hive, task_id)
    pid = _read_pid(path)
    if pid is None or not _alive(pid) or not _is_loop(pid):
        path.unlink(missing_ok=True)
        return f"no heartbeat loop running for {task_id}"
    os.kill(pid, signal.SIGTERM)
    path.unlink(missing_ok=True)
    return f"stopped heartbeat loop {pid} for {task_id}"
```

`acquire_loop_lock` counts the current process as a loop even without `--loop` on its command line: a second acquire from the same process is still a second loop.

- [ ] **Step 4: Wire the flags into the CLI**

In `skills/rip-swarm/rip_swarm/cli.py`:

1. Add `import signal` and `import time` to the stdlib imports, and `from rip_swarm.lease import LoopRunning, acquire_loop_lock, heartbeat_loop, lease_ttl, release_loop_lock, stop_loop`.
2. In `main`, next to `except WaitRunning as e:`, add the same handler for `LoopRunning` (print to stderr, return 3).
3. In `_parser`, after `hb_p.add_argument("--task", required=True)`:

```python
    hb_p.add_argument("--loop", action="store_true",
                      help="heartbeat each time half the lease is gone, until stopped")
    hb_p.add_argument("--stop", action="store_true",
                      help="stop the running --loop for --task in this hive clone")
```

4. In `_dispatch`, replace `return _heartbeat(args, hive, now, profile)` with:

```python
        if args.stop:
            return stop_loop(hive, _require(args.task, "--task"))
        if args.loop:
            return _heartbeat_loop(args, hive, profile)
        return _heartbeat(args, hive, now, profile)
```

5. Add after `_heartbeat`:

```python
def _heartbeat_loop(args: argparse.Namespace, hive: Path, profile: dict) -> None:
    """Execution proposals §3: the lease length comes from the profile, never
    from the skill's prose. SIGTERM (from --stop) ends the loop with exit 0
    and removes its pid file; a lost lease ends it with exit 2."""
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")
    _resolve_harness(hive, agent, args.harness)
    lock = acquire_loop_lock(hive, task_id)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        heartbeat_loop(
            hive, task_id, agent, ttl=lease_ttl(profile, task_id),
            beat=lambda at: _summary(args, _heartbeat(args, hive, at, profile)),
            clock=now_utc, sleep=time.sleep,
            out=lambda line: print(line, flush=True),
            err=lambda line: print(line, file=sys.stderr, flush=True),
        )
    finally:
        release_loop_lock(lock)
```

`_summary` reads `args.command` (`heartbeat`), `args.task` and `args.agent`, so the printed line is the one-shot heartbeat's.

- [ ] **Step 5: Run the tests to see them pass**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_lease tests.test_cli -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 6: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/rip-swarm/rip_swarm/lease.py skills/rip-swarm/rip_swarm/cli.py tests/test_lease.py
git commit -m "feat: heartbeat --loop follows the profile's lease; --stop ends it

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 3: Skills for §2, §3 and §4

**Files:**
- Modify: `skills/swarm-master/SKILL.md`
- Modify: `skills/swarm-worker/SKILL.md`
- Modify: `skills/rip-swarm/SKILL.md`
- Modify: `tests/test_packaging.py`
- Modify: `tests/test_rehearsal.py` (`Master.handle_reject` step 4)

**Interfaces:**
- Consumes: `claim.py reject … --cascade` and its output lines (Task 1); `claim.py heartbeat … --loop` / `--stop` (Task 2).
- Produces: skill text that Task 8 extends. Task 8 adds the review chain to the same sections, so keep the headings named here.

- [ ] **Step 1: Write the failing packaging tests**

In `tests/test_packaging.py`, in `test_master_reject_handler_is_guarded_by_the_board`, replace the needle `'--note "dependency <T> rejected"',` with these two:

```python
                       '--task <T> --agent "$AGENT" --cascade',
                       "`skipped <id> held by <agent> until <expires_at>`",
```

and replace the last assertion's `handler.index('--note "dependency <T> rejected"')` with `handler.index("--cascade")`.

Add these tests to `TestPackaging` (after `test_master_reject_handler_is_guarded_by_the_board`):

```python
    def test_heartbeat_loop_replaces_the_fifteen_minutes(self):
        # Execution proposals §3: the lease comes from the profile, not the prose.
        for name, task in (("swarm-master", "orchestrator"), ("swarm-worker", "<id>")):
            text = self._text(name)
            self.assertNotIn("15 minutes", text, name)
            self.assertIn(f'heartbeat --hive "$HIVE" --task {task} --agent "$AGENT" --loop', text, name)
            self.assertIn("`--stop` in place of `--loop`", text, name)
            self.assertIn("Its end is not a wake.", text, name)
        master = self._text("swarm-master")
        self.assertIn("Stop the heartbeat loop", master)
        self.assertLess(master.index("start the heartbeat loop"), master.index("OUTCOME=$OUTCOME"))

    def test_complete_is_the_handoff(self):
        # Execution proposals §4: no result message after complete.
        worker = self._text("swarm-worker")
        self.assertNotIn("--type result", worker)
        self.assertIn("`complete` is the handoff", worker)

    def test_reference_names_the_new_flags(self):
        ref = (SKILLS / "rip-swarm" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("--cascade", "--loop", "--stop"):
            self.assertIn(needle, ref)
```

`self._text(name)` and `ROLES` already exist in the class (used by the other skill tests); check their definitions before relying on them.

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_packaging -v 2>&1 | tail -8`
Expected: the four tests above FAIL.

- [ ] **Step 2: Master skill — the heartbeat loop**

In `skills/swarm-master/SKILL.md` section 2, replace the paragraph that begins `Your heartbeat is` (it ends `(15 minutes).`) with:

````markdown
Your heartbeat is `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat --hive "$HIVE" --task orchestrator --agent "$AGENT"`. Run it before every merge, acceptance check and write.

**The heartbeat loop** keeps the baton alive through a step that can outlast half the lease: a review's merge and acceptance check. Start it as a background command, the way section 5 starts `wait`:

```bash
RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat --hive "$HIVE" --task orchestrator --agent "$AGENT" --loop
```

It reads `orchestrator_lease_ttl` from the profile and heartbeats each time half of it is gone. Stop it with the same command, `--stop` in place of `--loop`. While it runs it is your heartbeat: do not run the one-shot heartbeat, and run no hive write until you have stopped it. Its end is not a wake. Exit 3 means a loop is already running for the baton; keep that one. If it ended with exit 2 before you stopped it, the baton is gone: stop and report, as below.
````

- [ ] **Step 3: Master skill — the review uses the loop**

In section 6, `wake task-finished <T> complete`:

1. Step 4 begins `Review it **off** the integration branch. Heartbeat first.` Change `Heartbeat first.` to `Heartbeat first, then start the heartbeat loop (section 2).`
2. After the sentence `Later commands take \`TIP\` and \`SHA\` from this \`OUTCOME=\` line. They are not set in any new shell.`, add:

```markdown
   **Stop the heartbeat loop** as soon as `WORKTREE` is back on `rip-swarm/integration`, and in every case before the next hive write: right after an `OUTCOME=` line other than `OUTCOME=merged`; on `OUTCOME=merged`, right after the *Passes* item 1 command prints `NEW_TIP=` or `FAILED:`, or after the *Falls short* command.
```

3. Step 9 begins `**\`OUTCOME=merged\`.** \`WORKTREE\` is on \`rip-swarm/review-<T>\` with the result merged. Heartbeat, then run the task's acceptance check`. Change `Heartbeat, then run` to `The heartbeat loop is running; run`.

- [ ] **Step 4: Master skill — the reject handler cascades in one command**

In `### \`wake task-finished <T> reject\``, replace step 4 (it begins `4. **Reject the dependents.**`) with:

```markdown
4. **Reject the dependents**, all in one publish: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" reject --hive "$HIVE" --task <T> --agent "$AGENT" --cascade`. It rejects every task that is not rejected yet and waits on `<T>`, directly or further down the `after` chain, with the note `dependency <T> rejected`. It prints one line per task: `rejected <id>`, `already rejected <id>`, or `skipped <id> held by <agent> until <expires_at>`, and `nothing waits on <T>` when there is none. The tasks posted in step 2 never wait on `<T>`, so this does not reach them. A `skipped` line means someone still holds a dependent: report those ids to the operator and stop. The wake comes back when that holder releases or its claim expires.
```

- [ ] **Step 5: Worker skill — the loop, and `complete` is the handoff**

In `skills/swarm-worker/SKILL.md` section 5:

1. Replace step 3 (it begins `3. Heartbeat before each long step.`) with:

````markdown
3. Heartbeat before each long step: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat --hive "$HIVE" --task <id> --agent "$AGENT"`. Around a long edit or test run, start the heartbeat loop in the background instead, the way section 3 starts `wait`:
   ```bash
   RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat --hive "$HIVE" --task <id> --agent "$AGENT" --loop
   ```
   It reads `worker_lease_ttl` from the profile and heartbeats each time half of it is gone. Stop it with the same command, `--stop` in place of `--loop`, as soon as that step ends, and always before step 4. Its end is not a wake. If it ended with exit 2 before you stopped it, you no longer hold the claim: handle it as an exit 2 from `heartbeat` (below).
````

2. Delete step 6 (it begins `6. \`RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/message.py"` and sends `--type result`). Renumber `7. Go back to waiting.` to `6. Go back to waiting.`
3. At the end of step 5 (the `complete` block), add the line `   \`complete\` is the handoff: the master is woken by its tombstone and reads the result from \`result_ref\`. Do not send a result message.`

- [ ] **Step 6: The reference**

In `skills/rip-swarm/SKILL.md`, in the `## Tasks, claims, acceptance` code block, after the `heartbeat` line add:

```bash
python3 "$SKILL_DIR/scripts/claim.py" heartbeat --hive "$HIVE" --task ID --agent "$AGENT" --loop|--stop
```

and after the `release|reject` line:

```bash
python3 "$SKILL_DIR/scripts/claim.py" reject --hive "$HIVE" --task ID --agent "$AGENT" --cascade
```

In the rules list below it, replace `- Heartbeat at or before half the lease.` with:

```markdown
- Heartbeat at or before half the lease. `heartbeat --loop` does it for you while you work outside `wait`: it reads the lease from the profile, heartbeats each time half of it is gone, and runs until `heartbeat --stop` for the same task. A second loop for the same task exits 3. The loop exits 2 when the lease is gone.
```

and after the bullet that begins `` - `reject` by the baton holder`` add:

```markdown
- `reject --cascade` (baton holder, once `ID` is rejected) rejects every task that waits on `ID` through `after`, directly or further down, in one publish, with the note `dependency ID rejected`. It prints `rejected <id>`, `already rejected <id>` or `skipped <id> held by <agent> until <time>` per task. It refuses `--note`.
```

- [ ] **Step 7: The rehearsal's reject handler uses the cascade**

In `tests/test_rehearsal.py`, `Master.handle_reject`, replace the step 4 loop:

```python
        for tid in chain:                                                          # step 4
            if not board[tid].rejected:
                self.reject(tid, f"dependency {task} rejected")
                done.append(f"reject {tid}")
        return done
```

with:

```python
        rc, out = cli("reject", "--hive", self.hive, "--task", task,               # step 4
                      "--agent", self.agent, "--cascade", at=self.now)
        assert rc == 0, out
        done += [f"reject {line.split()[1]}" for line in out.splitlines()
                 if line.startswith("rejected ")]
        return done
```

`chain` is still used by step 2 (the `dead` set), so keep `chain = self._downstream(board, task)`.

- [ ] **Step 8: Run the tests**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_packaging tests.test_rehearsal -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 9: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/swarm-master/SKILL.md skills/swarm-worker/SKILL.md skills/rip-swarm/SKILL.md \
  tests/test_packaging.py tests/test_rehearsal.py
git commit -m "docs(skills): one-command cascade, the heartbeat loop, complete is the handoff

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 4: `min_reviews` in the profile; `kind`, `min_reviews`, `reviews` on the board (spec §5.1, §5.2, §5.5 `inbox-add`)

**Files:**
- Modify: `skills/rip-swarm/rip_swarm/profile.py`
- Modify: `skills/rip-swarm/templates/_swarm/profiles/default.yaml`
- Modify: `skills/rip-swarm/rip_swarm/inbox.py`
- Modify: `skills/rip-swarm/rip_swarm/board.py`
- Modify: `skills/rip-swarm/rip_swarm/cli.py` (`inbox-add` flags)
- Modify: `tests/test_inbox.py`, `tests/test_profile.py`, `tests/test_board.py`

**Interfaces:**
- Produces: `profile.min_reviews_floors(profile: dict) -> dict[str, int]`; raises `ValueError` naming the bad key.
- Produces: `inbox.create_task(..., kind: str | None = None, min_reviews: int | None = None, reviews: str | None = None, floors: dict[str, int] | None = None)`.
- Produces: `TaskView.kind: str | None`, `TaskView.min_reviews: int` (0 when absent), `TaskView.reviews: str | None`, all with defaults so existing constructors keep working.
- Produces: `board.posting_order(view) -> tuple[str, str]` (`created_at`, then task id); `board.artifact_of(board, task_id) -> str | None`; `board.chain(board, artifact_id) -> list[TaskView]` (reviews and fixes of the artifact, in posting order, the artifact excluded).
- CLI: `inbox-add … [--kind spec|plan|implementation] [--min-reviews N] [--reviews A]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox.py` (before the `if __name__` block):

```python
from datetime import datetime, timezone

from rip_swarm.acceptance import master_reject
from rip_swarm.orchestrator import promote

T0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
FLOORS = {"spec": 2, "plan": 0, "implementation": 1}


class TestReviewFields(unittest.TestCase):
    """Execution proposals §5.2 and §5.5: the inbox-add rules."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        (self.hive / "agents").mkdir()
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: op\n  harness: human\n  role: operator\n"
            "- id: alice\n  harness: claude-code\n  role: worker\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, title="A", **kw):
        return create_task(self.hive, title=title, created_by="op", now=T0, floors=FLOORS, **kw)

    def test_kind_defaults_to_the_profile_number(self):
        doc = self.post(kind="spec")
        self.assertEqual((doc["kind"], doc["min_reviews"]), ("spec", 2))
        self.assertEqual(self.post(kind="plan")["min_reviews"], 0)

    def test_the_profile_is_a_floor(self):
        self.assertEqual(self.post(kind="spec", min_reviews=3)["min_reviews"], 3)
        with self.assertRaisesRegex(InboxError, r"min_reviews for spec is at least 2 \(profile\)"):
            self.post(kind="spec", min_reviews=1)

    def test_min_reviews_without_kind_is_allowed(self):
        self.assertEqual(self.post(min_reviews=1)["min_reviews"], 1)
        self.assertNotIn("kind", self.post(min_reviews=1))

    def test_unknown_kind_and_bad_numbers_are_refused(self):
        with self.assertRaisesRegex(InboxError, "unknown kind 'poem'"):
            self.post(kind="poem")
        for bad in (-1, "2", True, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(InboxError, "must be an integer >= 0"):
                    self.post(min_reviews=bad)

    def test_reviews_needs_a_reviewed_artifact(self):
        a = self.post(kind="spec")["id"]
        plain = self.post(title="plain")["id"]
        r = self.post(title="Review 1", reviews=a)
        self.assertEqual(r["reviews"], a)
        self.assertNotIn("min_reviews", r)
        with self.assertRaisesRegex(InboxError, f"{plain} is not a reviewed artifact"):
            self.post(reviews=plain)
        with self.assertRaisesRegex(InboxError, "unknown task"):
            self.post(reviews="task_01J00000000000000000000000")

    def test_reviews_combines_with_nothing_else(self):
        a = self.post(kind="spec")["id"]
        for extra in ({"fixes": a}, {"min_reviews": 1}, {"kind": "spec"}):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(InboxError, "--reviews cannot be combined"):
                    self.post(reviews=a, **extra)

    def test_a_replacement_may_copy_a_lower_number(self):
        promote(self.hive, agent="alice", harness="claude-code", now=T0, lease_seconds=1800,
                reason="m", allow_self_promote=False, operators=["op"], by="op")
        x = create_task(self.hive, title="Spec", created_by="op", now=T0,
                        floors={"spec": 1, "plan": 0}, kind="spec")["id"]     # a lighter profile
        title = f"Spec (replaces {x})"
        with self.assertRaisesRegex(InboxError, "at least 2"):
            self.post(title=title, kind="spec", min_reviews=1)            # X is not rejected
        master_reject(self.hive, agent="alice", task_id=x, note="drop", now=T0)
        doc = self.post(title=title, kind="spec", min_reviews=1)          # an exact copy
        self.assertEqual(doc["min_reviews"], 1)
        with self.assertRaisesRegex(InboxError, "at least 2"):
            self.post(title=title, kind="spec", min_reviews=0)            # not the same number
        with self.assertRaisesRegex(InboxError, "at least 1"):
            self.post(title=title, kind="implementation", min_reviews=0)  # not the same kind
        with self.assertRaisesRegex(InboxError, "at least 2"):
            self.post(title="Spec", kind="spec", min_reviews=1)           # not titled as a copy
```

Append to `tests/test_profile.py`, inside `TestProfile`:

```python
    def test_min_reviews_defaults_to_zero_per_kind(self):
        from rip_swarm.profile import min_reviews_floors
        prof = load_profile(self.hive, "default")
        self.assertEqual(min_reviews_floors(prof), {"spec": 0, "plan": 0, "implementation": 0})
        self._write_site("strict", "min_reviews:\n  spec: 2\n")
        strict = load_profile(self.hive, "strict")
        self.assertEqual(min_reviews_floors(strict), {"spec": 2, "plan": 0, "implementation": 0})

    def test_min_reviews_must_be_whole_numbers(self):
        from rip_swarm.profile import min_reviews_floors
        for text in ("min_reviews:\n  spec: two\n", "min_reviews:\n  spec: -1\n",
                     "min_reviews: 3\n"):
            with self.subTest(text=text):
                self._write_site("bad", text)
                with self.assertRaisesRegex(ValueError, "min_reviews"):
                    min_reviews_floors(load_profile(self.hive, "bad"))

    def test_the_template_ships_zero_reviews(self):
        from rip_swarm.init_hive import _DEFAULT_TEMPLATE
        from rip_swarm.simpleyaml import load_yaml
        doc = load_yaml((_DEFAULT_TEMPLATE / "profiles" / "default.yaml").read_text(encoding="utf-8"))
        self.assertEqual(doc["min_reviews"], {"spec": 0, "plan": 0, "implementation": 0})
```

Append to `tests/test_board.py`, inside its test class (it has `self.hive`, `self._task`, `MISSING` and imports `main` and `read_board`):

```python
    def test_review_fields_and_the_chain(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from rip_swarm.board import artifact_of, chain

        def add(*argv):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(["inbox-add", "--hive", str(self.hive), "--local",
                           "--created-by", "op", *argv])
            return rc, out.getvalue().split()[1].rstrip(":") if rc == 0 else err.getvalue()

        (self.hive / "profiles").mkdir(exist_ok=True)
        (self.hive / "profiles" / "default.yaml").write_text(
            "min_reviews:\n  spec: 1\n", encoding="utf-8")
        rc, a = add("--title", "Spec", "--kind", "spec")
        self.assertEqual(rc, 0)
        _, r1 = add("--title", "Review 1 of Spec", "--reviews", a)
        _, f1 = add("--title", "Revise Spec after review 1", "--fixes", a)
        _, plain = add("--title", "Other")
        board = read_board(self.hive, T0)
        self.assertEqual((board[a].kind, board[a].min_reviews, board[a].reviews), ("spec", 1, None))
        self.assertEqual((board[r1].reviews, board[r1].min_reviews), (a, 0))
        self.assertEqual({artifact_of(board, t) for t in (a, r1, f1)}, {a})
        self.assertIsNone(artifact_of(board, plain))
        self.assertEqual([v.task_id for v in chain(board, a)], sorted([r1, f1]))
        rc, err = add("--title", "x", "--kind", "spec", "--min-reviews", "0")
        self.assertEqual(rc, 1)
        self.assertIn("min_reviews for spec is at least 1 (profile)", err)
        rc, err = add("--title", "x", "--min-reviews", "two")
        self.assertEqual(rc, 1)
        self.assertIn("must be an integer >= 0", err)
        (self.hive / "profiles" / "default.yaml").write_text(
            "min_reviews:\n  spec: two\n", encoding="utf-8")
        rc, err = add("--title", "x", "--kind", "spec")                   # Review Focus 5
        self.assertEqual(rc, 1)
        self.assertIn("min_reviews", err)
        self.assertEqual(add("--title", "no kind")[0], 0)                 # still posts
```

The two chain tasks share `created_at` (one second), so `chain` orders them by id; `sorted([r1, f1])` states exactly that.

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_inbox tests.test_profile tests.test_board -v 2>&1 | tail -8`
Expected: FAIL / ERROR (`unexpected keyword argument 'floors'`, missing `min_reviews_floors`, missing `artifact_of`).

- [ ] **Step 3: The profile**

In `skills/rip-swarm/rip_swarm/profile.py`, add to `DEFAULT_PROFILE` after `"reviews_required_per_plan": 1,`:

```python
    "min_reviews": {"spec": 0, "plan": 0, "implementation": 0},
```

and add after `deep_merge`:

```python
def min_reviews_floors(profile: dict) -> dict[str, int]:
    """The profile's `min_reviews` map: artifact kind -> the minimum number of
    independent review rounds, a floor the goal can only raise (execution
    proposals §5.1). A bad value is an error naming its key, never a silent 0."""
    raw = profile.get("min_reviews") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile min_reviews must map each kind to a number, got {raw!r}")
    out: dict[str, int] = {}
    for kind, n in raw.items():
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"profile min_reviews[{kind}] must be an integer >= 0, got {n!r}")
        out[str(kind)] = n
    return out
```

In `skills/rip-swarm/templates/_swarm/profiles/default.yaml`, after `reviews_required_per_plan: 1` add:

```yaml
min_reviews:
  spec: 0
  plan: 0
  implementation: 0
```

- [ ] **Step 4: The inbox rules**

In `skills/rip-swarm/rip_swarm/inbox.py`, replace `create_task` with:

```python
_REPLACES = re.compile(r" \(replaces (task_[0-9A-HJKMNP-TV-Z]{26})\)$")


def _whole(n: object) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


def create_task(
    hive: Path,
    *,
    title: str,
    created_by: str,
    body: str | None = None,
    task_id: str | None = None,
    now: datetime | None = None,
    after: list[str] | None = None,
    fixes: str | None = None,
    kind: str | None = None,
    min_reviews: int | None = None,
    reviews: str | None = None,
    floors: dict[str, int] | None = None,
) -> dict:
    """Post one task. `kind`, `min_reviews` and `reviews` are the review-round
    fields of execution proposals §5.2; `floors` is the profile's `min_reviews`
    map, the floor a posted number may not go below (§5.5)."""
    title = title.strip()
    if not title:
        raise InboxError("title is required")
    if not created_by.strip():
        raise InboxError("created_by is required")
    if reviews is not None and (fixes or kind is not None or min_reviews is not None):
        raise InboxError("--reviews cannot be combined with --fixes, --min-reviews or --kind")
    if min_reviews is not None and not _whole(min_reviews):
        raise InboxError(f"--min-reviews must be an integer >= 0, got {min_reviews!r}")
    deps = list(dict.fromkeys(after or []))
    for ref in [*deps, *([fixes] if fixes else []), *([reviews] if reviews else [])]:
        validate_task_id(ref)
        if not HivePaths(hive).inbox_task(ref).is_file():
            raise InboxError(f"unknown task {ref}: post it before tasks that refer to it")
    if reviews is not None and not _whole_at_least_one(read_json(HivePaths(hive).inbox_task(reviews))):
        raise InboxError(f"{reviews} is not a reviewed artifact (min_reviews >= 1)")
    if kind is not None:
        known = floors or {}
        if kind not in known:
            names = ", ".join(sorted(known)) or "none"
            raise InboxError(f"unknown kind {kind!r}: the profile's min_reviews names {names}")
        if min_reviews is None:
            min_reviews = known[kind]
        elif min_reviews < known[kind] and not _exact_copy(hive, title, kind, min_reviews):
            raise InboxError(f"min_reviews for {kind} is at least {known[kind]} (profile)")
    ts = now or now_utc()
    tid = validate_task_id(task_id) if task_id is not None else new_task_id(ts)
    doc = {
        "id": tid,
        "title": title,
        "created_at": format_z(ts),
        "created_by": created_by,
    }
    if body is not None:
        doc["body"] = body
    if deps:
        doc["after"] = deps
    if fixes:
        doc["fixes"] = fixes
    if kind is not None:
        doc["kind"] = kind
    if min_reviews is not None:
        doc["min_reviews"] = min_reviews
    if reviews is not None:
        doc["reviews"] = reviews
    excl_create_json(HivePaths(hive).inbox_task(tid), doc)
    return doc


def _whole_at_least_one(doc: dict) -> bool:
    return _whole(doc.get("min_reviews")) and doc["min_reviews"] >= 1


def _exact_copy(hive: Path, title: str, kind: str, n: int) -> bool:
    """A replacement's copy may keep its original's number below today's
    profile (§5.5): the title names `X`, `X` is rejected, and `X` has exactly
    this kind and number. Checked on the board, so nothing else passes."""
    match = _REPLACES.search(title)
    if not match:
        return False
    original = match.group(1)
    try:
        doc = read_json(HivePaths(hive).inbox_task(original))
    except (OSError, ValueError):
        return False
    rejected = any(HivePaths(hive).claims.glob(f"{original}.reject.*.json"))
    return rejected and doc.get("kind") == kind and doc.get("min_reviews") == n and _whole(n)
```

`_exact_copy` globs the reject tombstone directly because `inbox.py` cannot import `board.py` (board imports inbox).

- [ ] **Step 5: The board**

In `skills/rip-swarm/rip_swarm/board.py`, add three fields at the end of `TaskView` (after `blocked_by`), with defaults:

```python
    kind: str | None = None
    min_reviews: int = 0
    reviews: str | None = None
```

In `read_board`, add to the `TaskView(...)` call:

```python
            kind=doc.get("kind") if isinstance(doc.get("kind"), str) else None,
            min_reviews=_reviews_needed(doc.get("min_reviews")),
            reviews=doc.get("reviews") if isinstance(doc.get("reviews"), str) else None,
```

and add these helpers after `fixers`:

```python
def _reviews_needed(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def posting_order(view: TaskView) -> tuple[str, str]:
    """`created_at` has one-second resolution, so the task id breaks ties
    (execution proposals §5.3)."""
    return (view.created_at, view.task_id)


def artifact_of(board: dict[str, TaskView], task_id: str) -> str | None:
    """The reviewed artifact (min_reviews >= 1) whose chain `task_id` is in:
    itself, the task it `reviews`, or the task it `fixes`. None otherwise."""
    view = board.get(task_id)
    if view is None:
        return None
    if view.min_reviews >= 1:
        return task_id
    for ref in (view.reviews, view.fixes):
        if ref and ref in board and board[ref].min_reviews >= 1:
            return ref
    return None


def chain(board: dict[str, TaskView], artifact_id: str) -> list[TaskView]:
    """Every review and every fix of `artifact_id`, in posting order; the
    artifact itself is not included."""
    return sorted(
        (v for v in board.values() if v.reviews == artifact_id or v.fixes == artifact_id),
        key=posting_order,
    )
```

- [ ] **Step 6: The CLI flags**

In `skills/rip-swarm/rip_swarm/cli.py`:

1. Import `min_reviews_floors` next to `load_profile`.
2. In `_parser`, after the `--fixes` argument of `inbox_p`:

```python
    inbox_p.add_argument("--kind", help="artifact kind: a key of the profile's min_reviews")
    inbox_p.add_argument("--min-reviews", dest="min_reviews",
                         help="review rounds this artifact needs (>= the profile's number)")
    inbox_p.add_argument("--reviews", help="task id of the artifact this review task reviews")
```

`--min-reviews` has no `type=int` on purpose: argparse exits 2 on a bad type, and §5.5 makes every `inbox-add` refusal exit 1.

3. Replace `_inbox_add`'s `op` with:

```python
    min_reviews = _whole_number(args.min_reviews, "--min-reviews")

    def op() -> dict:
        # Read the profile inside the op, after publish fast-forwarded the hive.
        floors = min_reviews_floors(load_profile(hive, args.profile)) if args.kind else None
        return create_task(
            hive,
            title=args.title,
            created_by=args.created_by,
            body=args.body,
            now=now,
            after=args.after,
            fixes=args.fixes,
            kind=args.kind,
            min_reviews=min_reviews,
            reviews=args.reviews,
            floors=floors,
        )
```

and add near `_require`:

```python
def _whole_number(value: str | None, flag: str) -> int | None:
    if value is None:
        return None
    if not str(value).isdigit():
        raise ValueError(f"{flag} must be an integer >= 0, got {value!r}")
    return int(value)
```

The profile is loaded only with `--kind`, so a broken `min_reviews` in the profile never blocks an ordinary post (Review Focus 5).

- [ ] **Step 7: Run the tests to see them pass**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_inbox tests.test_profile tests.test_board tests.test_init_hive tests.test_cli -v 2>&1 | tail -5`
Expected: `OK`. If a template test compares `profiles/default.yaml` with a fixed text, add the `min_reviews` block there too.

- [ ] **Step 8: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/rip-swarm/rip_swarm/profile.py skills/rip-swarm/templates/_swarm/profiles/default.yaml \
  skills/rip-swarm/rip_swarm/inbox.py skills/rip-swarm/rip_swarm/board.py skills/rip-swarm/rip_swarm/cli.py \
  tests/test_inbox.py tests/test_profile.py tests/test_board.py
git commit -m "feat: min_reviews profile floor and the kind, min_reviews, reviews inbox fields

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 5: `complete --verdict` and `reviews.py` (spec §5.3, §5.4, §5.5 `complete`)

**Files:**
- Modify: `skills/rip-swarm/rip_swarm/claim.py` (`complete`, `check_verdict`)
- Create: `skills/rip-swarm/rip_swarm/reviews.py`
- Create: `skills/rip-swarm/scripts/reviews.py`
- Modify: `skills/rip-swarm/rip_swarm/cli.py` (`complete --verdict`, `reviews` command)
- Create: `tests/test_reviews.py`

**Interfaces:**
- Consumes: `TaskView.kind/min_reviews/reviews`, `board.artifact_of`, `board.chain` (Task 4).
- Produces: `claim.complete(hive, task_id, agent, now, result_ref, note=None, verdict=None)`; the `complete` tombstone carries `"verdict"` when given. `claim.check_verdict(hive, task_id, verdict)` raises `ValueError`.
- Produces in `rip_swarm/reviews.py`: `ReviewsError(ValueError)`; `ChainState` (fields `artifact`, `tasks`, `settled`, `head`, `head_sha`, `head_verdict`, `rounds`; property `ready`); `chain_state(hive, board, artifact_id, stones=None) -> ChainState`; `Step` (fields `next`, `artifact`, `rounds`, `needed`, `head`, `sha`, `chain`, `review`; method `line()`); `next_step(hive, board, task_id) -> Step`; `tombstones_by_task(hive) -> dict[str, list[Tombstone]]` (each list in write order).
- CLI: `reviews.py --hive H --task T` prints the `NEXT=` line and exits 0, or exits 1 with a message.
- Test base: `tests/test_reviews.py` defines `ChainCase`, which Tasks 6 and 7 extend.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reviews.py`:

```python
# tests/test_reviews.py — review rounds per artifact kind (execution proposals §5)
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, local_hive
from rip_swarm.acceptance import accept_task, master_reject
from rip_swarm.board import list_tombstones, read_board
from rip_swarm.claim import ClaimDenied, complete, release, try_claim
from rip_swarm.cli import main
from rip_swarm.inbox import create_task
from rip_swarm.orchestrator import promote
from rip_swarm.reviews import ReviewsError, next_step

REG = (
    "- id: op\n  harness: human\n  role: operator\n"
    "- id: master\n  harness: grok\n  role: worker\n"
    "- id: alice\n  harness: claude-code\n  role: worker\n"
    "- id: bob\n  harness: grok\n  role: worker\n"
    "- id: carol\n  harness: claude-code\n  role: worker\n"
)
HARNESS = {"master": "grok", "alice": "claude-code", "bob": "grok", "carol": "claude-code"}
FLOORS = {"spec": 0, "plan": 0, "implementation": 0}
S = timedelta(seconds=1)


def cli(*argv, at=T0):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), \
            mock.patch("rip_swarm.cli.now_utc", return_value=at):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue().strip(), err.getvalue().strip()


class ChainCase(unittest.TestCase):
    """A local board: `master` holds the baton, `alice` writes, `bob` reviews.
    Every post and complete moves the clock one second, so posting order is
    creation order unless a test pins the time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name), REG)
        promote(self.hive, agent="master", harness="grok", now=T0, lease_seconds=86400,
                reason="master", allow_self_promote=False, operators=["op"], by="op")
        self.clock = T0
        self.shas = (f"{n:07x}" for n in range(0xa000001, 0xa0fffff))

    def tearDown(self):
        self.tmp.cleanup()

    def later(self):
        self.clock += S
        return self.clock

    def post(self, title, **kw):
        now = kw.pop("now", None) or self.later()
        return create_task(self.hive, title=title, created_by="master", now=now,
                           floors=FLOORS, **kw)["id"]

    def artifact(self, n=2):
        return self.post("Spec", kind="spec", min_reviews=n)

    def done(self, tid, agent, verdict=None):
        now = self.later()
        try_claim(self.hive, tid, agent, HARNESS[agent], now, 3600)
        sha = next(self.shas)
        complete(self.hive, tid, agent, now, f"rip-swarm/{agent}@{sha}", verdict=verdict)
        return sha

    def review(self, a, agent="bob", verdict="clean"):
        r = self.post("Review", reviews=a)
        return r, self.done(r, agent, verdict)

    def fix(self, a, agent="alice"):
        f = self.post("Revise", fixes=a)
        return f, self.done(f, agent)

    def step(self, tid):
        return next_step(self.hive, read_board(self.hive, self.clock), tid)

    def claim_file(self, tid):
        return self.hive / "claims" / f"{tid}.json"


class TestVerdict(ChainCase):
    def test_a_review_completes_with_a_verdict(self):
        a = self.artifact()
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        try_claim(self.hive, r, "bob", "grok", self.later(), 3600)
        for bad in (None, "Clean", "ok"):                                   # Review Focus 1
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "--verdict clean or findings"):
                    complete(self.hive, r, "bob", self.clock, "rip-swarm/bob@abc1234", verdict=bad)
                self.assertTrue(self.claim_file(r).is_file())             # still held, nothing written
        complete(self.hive, r, "bob", self.clock, "rip-swarm/bob@abc1234", verdict="findings")
        [stone] = [s for s in list_tombstones(self.hive) if s.task_id == r]
        self.assertEqual(stone.doc()["verdict"], "findings")

    def test_other_tasks_refuse_a_verdict(self):
        t = self.post("t")
        try_claim(self.hive, t, "alice", "claude-code", self.later(), 3600)
        with self.assertRaisesRegex(ValueError, "not a review task"):
            complete(self.hive, t, "alice", self.clock, "rip-swarm/alice@abc1234", verdict="clean")
        self.assertTrue(self.claim_file(t).is_file())

    def test_cli_verdict_errors_exit_1(self):
        t = self.post("t")
        try_claim(self.hive, t, "alice", "claude-code", self.later(), 3600)
        rc, _, err = cli("complete", "--hive", self.hive, "--task", t, "--agent", "alice",
                         "--result-ref", "rip-swarm/alice@abc1234", "--verdict", "clean",
                         "--local", at=self.clock)
        self.assertEqual(rc, 1)
        self.assertIn("not a review task", err)


class TestNextStep(ChainCase):
    def test_nothing_completed_waits(self):
        a = self.artifact()
        self.assertEqual(self.step(a).line(),
                         f"NEXT=wait ARTIFACT={a} ROUNDS=0/2 HEAD=none SHA=none CHAIN=")

    def test_the_chain_moves_round_by_round(self):
        a = self.artifact(2)
        sa = self.done(a, "alice")
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds, s.chain), ("post-review", a, sa, 0, ()))
        r1 = self.post("Review 1", reviews=a)
        self.assertEqual(self.step(a).next, "wait")                         # a review is open
        sr1 = self.done(r1, "bob", "findings")
        s = self.step(r1)                                                   # any chain task resolves A
        self.assertEqual((s.next, s.artifact, s.head, s.sha, s.rounds), ("post-revise", a, r1, sr1, 1))
        f1, sf1 = self.fix(a)
        s = self.step(f1)
        self.assertEqual((s.next, s.head, s.sha, s.rounds, s.chain), ("post-review", f1, sf1, 1, (r1, f1)))
        r2, sr2 = self.review(a, verdict="clean")
        self.assertEqual(self.step(a).line(),
                         f"NEXT=merge ARTIFACT={a} ROUNDS=2/2 HEAD={r2} SHA={sr2} CHAIN={r1},{f1},{r2}")

    def test_clean_but_short_asks_for_another_round(self):
        a = self.artifact(2)
        self.done(a, "alice")
        r1, sr1 = self.review(a)
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds), ("post-review", r1, sr1, 1))

    def test_a_fix_after_a_clean_review_needs_another_round(self):
        a = self.artifact(1)
        self.done(a, "alice")
        self.review(a)
        f, sf = self.fix(a)
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds), ("post-review", f, sf, 1))

    def test_a_rejected_review_is_not_a_round(self):
        a = self.artifact(1)
        sa = self.done(a, "alice")
        r1, _ = self.review(a)
        self.assertEqual(self.step(a).next, "merge")
        master_reject(self.hive, agent="master", task_id=r1, note="review rejected: thin", now=self.later())
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds, s.chain), ("post-review", a, sa, 0, ()))

    def test_done_once_the_artifact_is_settled(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r1, _ = self.review(a)
        accept_task(self.hive, agent="master", task_id=r1, integration_sha="0123abc", now=self.later())
        accept_task(self.hive, agent="master", task_id=a, integration_sha="0123abc", now=self.later())
        self.assertEqual(self.step(r1).next, "done")
        b = self.artifact(1)
        master_reject(self.hive, agent="master", task_id=b, note="drop", now=self.later())
        self.assertEqual(self.step(b).next, "done")

    def test_posting_order_breaks_ties_by_task_id(self):
        low, high = "task_01J00000000000000000000001", "task_01J00000000000000000000002"
        for review_id, fix_id, expected in ((high, low, "merge"), (low, high, "post-review")):
            with self.subTest(review=review_id):
                self.tearDown()
                self.setUp()
                a = self.artifact(1)
                self.done(a, "alice")
                same = self.later()
                self.post("Review", reviews=a, task_id=review_id, now=same)
                self.post("Revise", fixes=a, task_id=fix_id, now=same)
                self.done(fix_id, "alice")
                self.done(review_id, "bob", "clean")
                s = self.step(a)
                self.assertEqual(s.next, expected)
                self.assertEqual(s.chain, tuple(sorted([low, high])))

    def test_a_review_that_cannot_build_asks_for_a_rebase_once(self):
        a = self.artifact(1)
        sa = self.done(a, "alice")
        r1 = self.post("Review", reviews=a)
        try_claim(self.hive, r1, "bob", "grok", self.later(), 3600)
        release(self.hive, r1, "bob", self.clock, note=f"review {r1} cannot build on {sa}: conflict")
        s = self.step(a)
        self.assertEqual((s.next, s.review, s.sha), ("post-rebase", r1, sa))
        self.assertTrue(s.line().endswith(f" REVIEW={r1}"))
        self.post("Rebase Spec onto rip-swarm/integration", fixes=a)
        s = self.step(a)
        self.assertEqual((s.next, s.review), ("reject-review", r1))
        master_reject(self.hive, agent="master", task_id=r1, note="superseded", now=self.later())
        self.assertEqual(self.step(a).next, "wait")                         # the rebase is open

    def test_the_latest_release_of_one_second_decides(self):
        # Review Focus 2: the second release of a second is `<stamp>-2.json`,
        # which sorts before `<stamp>.json` by name.
        a = self.artifact(1)
        sa = self.done(a, "alice")
        conflict = "review {} cannot build on " + sa + ": conflict"
        for first, second, expected in ((conflict, "gave up", "wait"),
                                        ("gave up", conflict, "post-rebase")):
            with self.subTest(second=second):
                r = self.post("Review", reviews=a)
                now = self.later()
                for note in (first, second):
                    try_claim(self.hive, r, "bob", "grok", now, 3600)
                    release(self.hive, r, "bob", now, note=note.format(r))
                self.assertEqual(self.step(a).next, expected)
                master_reject(self.hive, agent="master", task_id=r, note="x", now=self.later())

    def test_exit_1_cases(self):
        plain = self.post("plain")
        with self.assertRaisesRegex(ReviewsError, "is not a reviewed artifact or in the chain of one"):
            self.step(plain)
        a = self.artifact(1)
        self.done(a, "alice")
        r1, _ = self.review(a)
        [stone] = [s for s in list_tombstones(self.hive) if s.task_id == r1]
        doc = stone.doc()
        del doc["verdict"]                                                  # edited by hand
        stone.path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(ReviewsError, "has no verdict"):
            self.step(a)

    def test_cli(self):
        a = self.artifact(2)
        rc, out, _ = cli("reviews", "--hive", self.hive, "--task", a, at=self.clock)
        self.assertEqual((rc, out), (0, f"NEXT=wait ARTIFACT={a} ROUNDS=0/2 HEAD=none SHA=none CHAIN="))
        plain = self.post("plain")
        rc, out, err = cli("reviews", "--hive", self.hive, "--task", plain, at=self.clock)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("is not a reviewed artifact", err)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_reviews -v 2>&1 | tail -5`
Expected: `ModuleNotFoundError: No module named 'rip_swarm.reviews'`.

- [ ] **Step 3: `complete --verdict`**

In `skills/rip-swarm/rip_swarm/claim.py`, add before `complete`:

```python
def _task_doc(hive: Path, task_id: str) -> dict:
    try:
        doc = read_json(HivePaths(hive).inbox_task(task_id))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def check_verdict(hive: Path, task_id: str, verdict: str | None) -> None:
    """Execution proposals §5.5: a review task (`reviews` set) completes with
    `clean` or `findings`, and no other task takes a verdict. This is a usage
    error, ValueError (exit 1), never ClaimDenied: the worker skill reads exit 2
    from `complete` as a lost lease."""
    reviews = _task_doc(hive, task_id).get("reviews")
    if reviews:
        if verdict not in ("clean", "findings"):
            got = "" if verdict is None else f", got {verdict!r}"
            raise ValueError(
                f"{task_id} reviews {reviews}: complete needs --verdict clean or findings{got}"
            )
    elif verdict is not None:
        raise ValueError(
            f"{task_id} is not a review task: --verdict is only for tasks posted with --reviews"
        )
```

Change `complete`'s signature to add `verdict: str | None = None` after `note`, call `check_verdict(hive, task_id, verdict)` right after the `result_ref` check (before `_require_holder`, so nothing is read or written on a usage error), and after `doc["result_ref"] = …` add:

```python
    if verdict is not None:
        doc["verdict"] = verdict
```

- [ ] **Step 4: `reviews.py`**

Create `skills/rip-swarm/rip_swarm/reviews.py`:

```python
# rip_swarm/reviews.py — a reviewed artifact's chain and its next step
# (execution proposals §5.3, §5.4). Read-only: everything comes from the board.
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rip_swarm.board import TaskView, Tombstone, artifact_of, chain, list_tombstones

VERDICTS = ("clean", "findings")
_CANNOT_BUILD = re.compile(
    r"review (?P<review>task_[0-9A-HJKMNP-TV-Z]{26}) cannot build on "
    r"(?P<sha>[0-9a-f]{7,64}): conflict"
)
_SUFFIX = re.compile(r"-(\d+)\.json$")


class ReviewsError(ValueError):
    """The board gives no next step for this task (exit 1, no NEXT line)."""


def _write_order(stone: Tombstone) -> tuple[str, int]:
    """Tombstones written in one second are `<stamp>.json`, `<stamp>-2.json`, …
    By name `-2` sorts first, so order by the stamp, then by that number."""
    match = _SUFFIX.search(stone.name)
    return (stone.stamp, int(match.group(1)) if match else 1)


def _stamp_z(stamp: str) -> str:
    """`20260926T100000Z` -> `2026-09-26T10:00:00Z`, comparable with `created_at`."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"


def tombstones_by_task(hive: Path) -> dict[str, list[Tombstone]]:
    out: dict[str, list[Tombstone]] = {}
    for stone in list_tombstones(hive):
        out.setdefault(stone.task_id, []).append(stone)
    for stones in out.values():
        stones.sort(key=_write_order)
    return out


def _complete_doc(stones: dict[str, list[Tombstone]], task_id: str) -> dict:
    for stone in stones.get(task_id, []):
        if stone.action == "complete":
            return stone.doc() or {}
    return {}


def _short_sha(doc: dict) -> str | None:
    ref = doc.get("result_ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    return ref.rsplit("@", 1)[-1].strip()


@dataclass(frozen=True)
class ChainState:
    artifact: TaskView
    tasks: tuple[TaskView, ...]      # its reviews and fixes, in posting order
    settled: tuple[TaskView, ...]    # of those, the completed and not rejected
    head: TaskView | None
    head_sha: str | None
    head_verdict: str | None
    rounds: int

    @property
    def ready(self) -> bool:
        """§5.3: at least `min_reviews` rounds, and the head is a clean review."""
        head = self.head
        return (
            head is not None
            and head.reviews == self.artifact.task_id
            and self.head_verdict == "clean"
            and self.rounds >= self.artifact.min_reviews
        )


def chain_state(
    hive: Path,
    board: dict[str, TaskView],
    artifact_id: str,
    stones: dict[str, list[Tombstone]] | None = None,
) -> ChainState:
    """The head is the chain task latest in posting order among those that
    are completed and not rejected, `A` itself first. A round is a review in
    the chain that is completed and not rejected."""
    stones = tombstones_by_task(hive) if stones is None else stones
    art = board[artifact_id]
    tasks = tuple(chain(board, artifact_id))
    settled = tuple(v for v in tasks if v.completed and not v.rejected)
    heads = ([art] if art.completed and not art.rejected else []) + list(settled)
    head = heads[-1] if heads else None
    doc = _complete_doc(stones, head.task_id) if head is not None else {}
    return ChainState(
        artifact=art,
        tasks=tasks,
        settled=settled,
        head=head,
        head_sha=_short_sha(doc) if head is not None else None,
        head_verdict=doc.get("verdict") if head is not None else None,
        rounds=sum(1 for v in settled if v.reviews == artifact_id),
    )


@dataclass(frozen=True)
class Step:
    next: str
    artifact: str
    rounds: int
    needed: int
    head: str | None
    sha: str | None
    chain: tuple[str, ...]
    review: str | None = None

    def line(self) -> str:
        text = (
            f"NEXT={self.next} ARTIFACT={self.artifact} ROUNDS={self.rounds}/{self.needed} "
            f"HEAD={self.head or 'none'} SHA={self.sha or 'none'} CHAIN={','.join(self.chain)}"
        )
        return f"{text} REVIEW={self.review}" if self.review else text


def next_step(hive: Path, board: dict[str, TaskView], task_id: str) -> Step:
    """§5.4's table, row by row. `task_id` is the artifact or any task in its chain."""
    artifact_id = artifact_of(board, task_id)
    if artifact_id is None:
        raise ReviewsError(f"{task_id} is not a reviewed artifact or in the chain of one")
    stones = tombstones_by_task(hive)
    st = chain_state(hive, board, artifact_id, stones)
    art = st.artifact

    def step(name: str, sha: str | None = None, review: str | None = None) -> Step:
        return Step(
            next=name,
            artifact=artifact_id,
            rounds=st.rounds,
            needed=art.min_reviews,
            head=st.head.task_id if st.head is not None else None,
            sha=sha if sha is not None else st.head_sha,
            chain=tuple(v.task_id for v in st.settled),
            review=review,
        )

    if art.accepted or art.rejected:
        return step("done")
    # An open review whose latest release says it could not build. Read from
    # the board, so a takeover master that never saw the wake or the message
    # still gets it (§5.4).
    for view in st.tasks:
        if view.reviews != artifact_id or not view.is_open:
            continue
        released = [s for s in stones.get(view.task_id, []) if s.action == "release"]
        if not released:
            continue
        note = (released[-1].doc() or {}).get("note") or ""
        match = _CANNOT_BUILD.fullmatch(note)
        if match is None or match["review"] != view.task_id:
            continue
        since = _stamp_z(released[-1].stamp)
        rebased = any(v.fixes == artifact_id and v.created_at >= since for v in st.tasks)
        return step("reject-review" if rebased else "post-rebase", sha=match["sha"],
                    review=view.task_id)
    if not art.completed or any(not v.completed and not v.rejected for v in st.tasks):
        return step("wait")
    head = st.head
    if head.reviews == artifact_id:
        if st.head_verdict not in VERDICTS:
            raise ReviewsError(
                f"review {head.task_id} has no verdict clean or findings on its complete "
                "tombstone; the board was edited by hand"
            )
        if st.head_verdict == "findings":
            return step("post-revise")
        return step("merge" if st.rounds >= art.min_reviews else "post-review")
    return step("post-review")
```

Create `skills/rip-swarm/scripts/reviews.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rip_swarm.cli import main

raise SystemExit(main(["reviews", *sys.argv[1:]]))
```

- [ ] **Step 5: The CLI**

In `skills/rip-swarm/rip_swarm/cli.py`:

1. Import `from rip_swarm.reviews import next_step`.
2. After `complete_p.add_argument("--note")`:

```python
    complete_p.add_argument("--verdict", help="review tasks only: clean or findings")
```

(No `choices=`: argparse would exit 2, which the worker skill reads as a lost lease.)

3. After the `status` parser:

```python
    rev_p = sub.add_parser(
        "reviews", parents=[base],
        help="read-only: the next step of a reviewed artifact's review chain",
    )
    rev_p.add_argument("--task", required=True)
```

4. In `_dispatch`, after the `status` branch:

```python
    if args.command == "reviews":
        return next_step(hive, read_board(hive, now), args.task).line()
```

5. In `_complete`, pass `verdict=args.verdict` to `complete(...)` (as a keyword, after `args.note`).

- [ ] **Step 6: Run the tests to see them pass**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_reviews tests.test_claim tests.test_cli -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 7: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`. If `tests/test_packaging.py` lists the scripts, add `reviews.py` there.

```bash
git add skills/rip-swarm/rip_swarm/claim.py skills/rip-swarm/rip_swarm/reviews.py \
  skills/rip-swarm/scripts/reviews.py skills/rip-swarm/rip_swarm/cli.py tests/test_reviews.py
git commit -m "feat: complete --verdict and reviews.py, the next step of a review chain

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 6: The `claim` and `accept` guards (spec §5.3 independence, §5.5 `claim` and `accept`)

**Files:**
- Modify: `skills/rip-swarm/rip_swarm/reviews.py` (`authors`, `reviewers`)
- Modify: `skills/rip-swarm/rip_swarm/claim.py` (`try_claim`)
- Modify: `skills/rip-swarm/rip_swarm/acceptance.py` (`accept_task`)
- Modify: `tests/test_reviews.py`

**Interfaces:**
- Consumes: `reviews.chain_state`, `reviews.tombstones_by_task`, `ChainState.ready` (Task 5); `board.artifact_of` (Task 4).
- Produces: `reviews.authors(hive, board, artifact_id, stones=None) -> set[str]`; `reviews.reviewers(hive, board, artifact_id, now, stones=None) -> set[str]`.
- Behaviour: `try_claim` refuses (exit 2) a chain task of a rejected artifact, a review to an author, and a fix to a reviewer. `accept_task` refuses (exit 2) an artifact with `min_reviews >= 1` that is not ready, `--via` or not.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reviews.py`, before the `if __name__` block:

```python
class TestIndependence(ChainCase):
    def claim(self, tid, agent):
        return try_claim(self.hive, tid, agent, HARNESS[agent], self.later(), 3600)

    def test_an_author_is_refused_a_review(self):
        a = self.artifact(2)
        self.done(a, "alice")
        r1 = self.post("Review 1", reviews=a)
        with self.assertRaisesRegex(ClaimDenied, f"alice wrote part of {a}; "
                                                 "its review must come from another agent"):
            self.claim(r1, "alice")
        self.done(r1, "bob", "findings")
        self.fix(a, agent="carol")                                          # a third agent folds
        r2 = self.post("Review 2", reviews=a)
        with self.assertRaisesRegex(ClaimDenied, "carol wrote part of"):
            self.claim(r2, "carol")                                         # and is an author now
        self.claim(r2, "bob")

    def test_a_reviewer_is_refused_a_fix_however_it_reviewed(self):
        for how in ("completed", "released", "holds"):
            with self.subTest(how=how):
                self.tearDown()
                self.setUp()
                a = self.artifact(2)
                self.done(a, "alice")
                r = self.post("Review", reviews=a)
                if how == "completed":
                    self.done(r, "bob", "findings")
                else:
                    self.claim(r, "bob")
                    if how == "released":
                        release(self.hive, r, "bob", self.clock, note="cannot build")
                f = self.post("Revise", fixes=a)
                with self.assertRaisesRegex(ClaimDenied, f"bob reviewed {a}; "
                                                         "its fixes must come from another agent"):
                    self.claim(f, "bob")

    def test_one_reviewer_may_do_every_round(self):
        a = self.artifact(2)
        self.done(a, "alice")
        self.review(a, agent="bob", verdict="clean")
        r2 = self.post("Review 2", reviews=a)
        self.claim(r2, "bob")                                               # after its own clean review

    def test_a_masters_reject_is_not_a_claim(self):
        from rip_swarm.reviews import reviewers
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        master_reject(self.hive, agent="master", task_id=r, note="superseded", now=self.later())
        self.assertEqual(reviewers(self.hive, read_board(self.hive, self.clock), a, self.clock), set())

    def test_the_chain_of_a_rejected_artifact_is_closed(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        master_reject(self.hive, agent="master", task_id=a, note="drop", now=self.later())
        with self.assertRaisesRegex(ClaimDenied, f"{a} is rejected"):
            self.claim(r, "bob")

    def test_cli_refusal_exits_2(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        rc, _, err = cli("claim", "--hive", self.hive, "--task", r, "--agent", "alice",
                         "--local", at=self.later())
        self.assertEqual(rc, 2)
        self.assertIn("alice wrote part of", err)


class TestAcceptReadiness(ChainCase):
    def accept(self, tid, *via):
        return accept_task(self.hive, agent="master", task_id=tid, integration_sha="0123abc",
                           via=via, now=self.later())

    def refused(self, a, n, k):
        with self.assertRaisesRegex(ClaimDenied, f"{a} needs {n} review rounds ending clean, has {k}"):
            self.accept(a)

    def test_findings_last(self):
        a = self.artifact(1)
        self.done(a, "alice")
        self.review(a, verdict="findings")
        self.refused(a, 1, 1)

    def test_clean_but_short(self):
        a = self.artifact(2)
        self.done(a, "alice")
        self.review(a)
        self.refused(a, 2, 1)

    def test_clean_and_met(self):
        a = self.artifact(2)
        self.done(a, "alice")
        r1, _ = self.review(a, verdict="findings")
        f1, _ = self.fix(a)
        r2, _ = self.review(a)
        for tid in (r1, f1, r2):                                            # chain tasks: today's rules
            self.accept(tid)
        self.assertEqual(self.accept(a)["task_id"], a)

    def test_a_fix_after_the_clean_review(self):
        a = self.artifact(1)
        self.done(a, "alice")
        self.review(a)
        self.fix(a)
        self.refused(a, 1, 1)

    def test_a_rejected_review_does_not_count(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r1, _ = self.review(a)
        master_reject(self.hive, agent="master", task_id=r1, note="review rejected: thin",
                      now=self.later())
        self.refused(a, 1, 0)

    def test_via_does_not_bypass_the_rounds(self):
        a = self.artifact(1)
        self.done(a, "alice")
        f, _ = self.fix(a)
        self.accept(f)
        with self.assertRaisesRegex(ClaimDenied, f"{a} needs 1 review rounds ending clean, has 0"):
            self.accept(a, f)

    def test_zero_rounds_is_todays_path(self):
        a = self.artifact(0)
        self.done(a, "alice")
        self.assertEqual(self.accept(a)["task_id"], a)
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_reviews -v 2>&1 | tail -8`
Expected: the new `TestIndependence` and `TestAcceptReadiness` tests FAIL (no refusal raised).

- [ ] **Step 3: Authors and reviewers**

In `skills/rip-swarm/rip_swarm/reviews.py`, change the imports to:

```python
from datetime import datetime

from rip_swarm.board import TaskView, Tombstone, artifact_of, chain, list_tombstones
from rip_swarm.fold import Expired, Holder, active_holder
```

and add at the end:

```python
def authors(
    hive: Path,
    board: dict[str, TaskView],
    artifact_id: str,
    stones: dict[str, list[Tombstone]] | None = None,
) -> set[str]:
    """§5.3: every agent that completed `A` or a fix of `A`."""
    stones = tombstones_by_task(hive) if stones is None else stones
    ids = [artifact_id, *(v.task_id for v in board.values() if v.fixes == artifact_id)]
    out: set[str] = set()
    for tid in ids:
        for stone in stones.get(tid, []):
            agent = (stone.doc() or {}).get("agent") if stone.action == "complete" else None
            if isinstance(agent, str):
                out.add(agent)
    return out


def reviewers(
    hive: Path,
    board: dict[str, TaskView],
    artifact_id: str,
    now: datetime,
    stones: dict[str, list[Tombstone]] | None = None,
) -> set[str]:
    """§5.3: every agent that has claimed a review of `A`: it holds a claim on
    one (live, or expired and not yet stolen) or has a tombstone on one. A
    master's reject is not a claim: its tombstone carries `"action": "reject"`
    and is skipped."""
    stones = tombstones_by_task(hive) if stones is None else stones
    out: set[str] = set()
    for view in board.values():
        if view.reviews != artifact_id:
            continue
        for stone in stones.get(view.task_id, []):
            doc = stone.doc() or {}
            if doc.get("action") == "reject":
                continue
            if isinstance(doc.get("agent"), str):
                out.add(doc["agent"])
        rec = active_holder(hive, view.task_id, now)
        if isinstance(rec, (Holder, Expired)):
            out.add(rec.agent)
    return out
```

- [ ] **Step 4: The `claim` refusals**

In `skills/rip-swarm/rip_swarm/claim.py`, change the board import to `from rip_swarm.board import artifact_of, blocked_by, finished_reason, read_board` and add `from rip_swarm.reviews import authors, reviewers`. In `try_claim`, after the `blocked by` check and before `return _create_claim(...)`, add `_check_chain(hive, task_id, agent, now)`, and define it after `try_claim`:

```python
def _check_chain(hive: Path, task_id: str, agent: str, now: datetime) -> None:
    """Execution proposals §5.5: no work starts on the chain of a rejected
    artifact, no agent reviews text it wrote, and a reviewer never fixes.
    Checked here, so every claim path is covered. An artifact itself and a
    task outside every chain pass untouched."""
    board = read_board(hive, now)
    artifact_id = artifact_of(board, task_id)
    if artifact_id is None or artifact_id == task_id:
        return
    if board[artifact_id].rejected:
        raise ClaimDenied(f"{artifact_id} is rejected")
    view = board[task_id]
    if view.reviews == artifact_id and agent in authors(hive, board, artifact_id):
        raise ClaimDenied(
            f"{agent} wrote part of {artifact_id}; its review must come from another agent"
        )
    if view.fixes == artifact_id and agent in reviewers(hive, board, artifact_id, now):
        raise ClaimDenied(
            f"{agent} reviewed {artifact_id}; its fixes must come from another agent"
        )
```

- [ ] **Step 5: The `accept` readiness rule**

In `skills/rip-swarm/rip_swarm/acceptance.py`, import `from rip_swarm.reviews import chain_state`. In `accept_task`, replace `view = read_board(hive, now).get(task_id)` with:

```python
    board = read_board(hive, now)
    view = board.get(task_id)
```

and after the `if view.rejected:` refusal add:

```python
    if view.min_reviews >= 1:
        # Execution proposals §5.5: `--via` does not bypass the rounds; the
        # fixes walk ends at this refusal as it ends at a rejected task.
        state = chain_state(hive, board, task_id)
        if not state.ready:
            raise ClaimDenied(
                f"{task_id} needs {view.min_reviews} review rounds ending clean, has {state.rounds}"
            )
```

Update `accept_task`'s docstring: add the sentence `A reviewed artifact (min_reviews >= 1) is refused until it is ready (execution proposals §5.3).`

- [ ] **Step 6: Run the tests to see them pass**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_reviews tests.test_claim tests.test_acceptance tests.test_profile -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 7: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/rip-swarm/rip_swarm/reviews.py skills/rip-swarm/rip_swarm/claim.py \
  skills/rip-swarm/rip_swarm/acceptance.py tests/test_reviews.py
git commit -m "feat: claim enforces reviewer independence; accept enforces review rounds

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 7: The cascade walks the chain; the derived wake; `status` (spec §5.5)

**Files:**
- Modify: `skills/rip-swarm/rip_swarm/board.py` (`downstream`)
- Modify: `skills/rip-swarm/rip_swarm/waiter.py` (`_blocking_reject`)
- Modify: `skills/rip-swarm/rip_swarm/status.py`
- Modify: `tests/test_reviews.py`, `tests/test_cascade.py`

**Interfaces:**
- Consumes: `board.chain` (Task 4); `reviews.chain_state` (Task 5); `acceptance.cascade_reject` and `_cascade_lines` (Task 1), which already carry `chain_of`.
- Produces: `downstream` also yields each walked reviewed artifact's chain tasks, with `chain_of` set to that artifact. `status_report` gains `"reviews": {tid: artifact}` and `"artifacts": {tid: {"kind", "rounds", "needed"}}`; `format_status` prints `(spec, reviews 1/2)`, `(reviews <A>)` and `(fixes <X>)` through one helper.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reviews.py`, before the `if __name__` block:

```python
class TestChainCascade(ChainCase):
    def test_the_cascade_rejects_the_chain_and_skips_a_live_claim(self):
        from rip_swarm.acceptance import cascade_reject
        a = self.artifact(2)
        self.done(a, "alice")
        r1, _ = self.review(a, verdict="findings")
        f1, _ = self.fix(a)
        r2 = self.post("Review 2", reviews=a)
        try_claim(self.hive, r2, "bob", "grok", self.later(), 3600)          # held at the reject
        d = self.post("Plan", after=[a])
        master_reject(self.hive, agent="master", task_id=a, note="not worth pursuing",
                      now=self.later())
        doc = cascade_reject(self.hive, agent="master", task_id=a, now=self.clock)
        self.assertEqual(sorted(doc["rejected"]), sorted([r1, f1, d]))
        [skip] = doc["skipped"]
        self.assertEqual((skip["task_id"], skip["agent"], skip["chain_of"]), (r2, "bob", a))
        from rip_swarm.cli import _cascade_lines
        self.assertIn(f"skipped {r2} (chain of {a}) held by bob until ", _cascade_lines(doc))

    def test_the_derived_wake_waits_for_a_live_claim(self):
        from rip_swarm.acceptance import cascade_reject
        from rip_swarm.state import seed_state
        from rip_swarm.waiter import Wake, tick
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        try_claim(self.hive, r, "bob", "grok", self.later(), 3600)
        state = seed_state(self.hive, "master")                             # A's complete is unseen
        wake = lambda: tick(self.hive, "master", state, self.clock, idle_after=600)
        master_reject(self.hive, agent="master", task_id=a, note="drop", now=self.later())
        self.assertEqual(wake(), Wake("task-finished", f"{a} complete"))    # its bare complete
        self.assertEqual(wake(), Wake("task-finished", f"{a} reject"))      # the tombstone
        self.assertIsNone(wake())                                           # silent: r is held
        complete(self.hive, r, "bob", self.later(), "rip-swarm/bob@abc1234", verdict="clean")
        self.assertEqual(wake(), Wake("task-finished", f"{r} complete"))
        self.assertEqual(wake(), Wake("task-finished", f"{a} reject"))      # derived, recurs
        self.assertEqual(wake(), Wake("task-finished", f"{a} reject"))
        cascade_reject(self.hive, agent="master", task_id=a, now=self.clock)
        self.assertEqual(wake(), Wake("task-finished", f"{r} reject"))
        self.assertEqual(wake(), Wake("all-complete"))

    def test_status_shows_kind_and_rounds(self):
        from rip_swarm.status import format_status, status_report
        a = self.artifact(2)
        self.done(a, "alice")
        r1, _ = self.review(a, verdict="clean")
        r2 = self.post("Review 2", reviews=a)
        plain = self.post("Plain", kind="plan")                             # a kind, zero rounds
        text = format_status(status_report(self.hive, self.clock))
        self.assertIn(f"{a} Spec (spec, reviews 1/2)", text)
        self.assertIn(f"{r2} Review 2 (reviews {a})", text)
        self.assertIn(f"{plain} Plain (plan)", text)
```

In `tests/test_cascade.py`, `TestCascadeCli`, add a publish test over a chain, with a second joined worker:

```python
    def test_a_chain_cascade_is_one_commit_under_the_widened_allowlist(self):
        w = join(self.repo, role="worker", harness="claude-code", now=T0)
        a = self.post("Spec", "--kind", "spec", "--min-reviews", "1")

        def as_worker(*argv):
            rc, out, err = cli(*argv[:1], "--hive", w.hive, *argv[1:], "--agent", w.agent)
            self.assertEqual(rc, 0, err)
            return out

        as_worker("claim", "--task", a)
        as_worker("complete", "--task", a, "--result-ref", f"rip-swarm/{w.agent}@abc1234")
        r = self.post("Review 1 of Spec", "--reviews", a)
        f = self.post("Revise Spec", "--fixes", a)
        d = self.post("Plan", "--after", a)
        self.assertEqual(self.reject(a, "--note", "not worth pursuing")[0], 0)
        before = self.commits()
        rc, out, err = self.reject(a, "--cascade")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(out.splitlines()),
                         sorted([f"rejected {r}", f"rejected {f}", f"rejected {d}"]))
        self.assertEqual(self.commits(), before + 1)
```

For that test, `setUp` must keep the project path: change `self.origin, repo = make_project(...)` to `self.origin, self.repo = make_project(...)` and join with `self.repo`. The `min_reviews` floor needs no profile: the template ships zeros and `--min-reviews 1` is above them.

- [ ] **Step 2: Run the tests to see them fail**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_reviews tests.test_cascade -v 2>&1 | tail -8`
Expected: the chain cascade tests reject only `d`; the wake test gets `None` where it expects the derived wake; the status test finds no annotation.

- [ ] **Step 3: The walk takes the chain**

In `skills/rip-swarm/rip_swarm/board.py`, replace `downstream` (it must come after `chain`, which Task 4 added; move it below if needed):

```python
def downstream(board: dict[str, TaskView], root: str) -> list[tuple[str, str | None]]:
    """Every task a reject of `root` cascades to: the tasks that wait on it
    through `after`, directly or further down, including a task that waits on
    several tasks of the walk (execution proposals §2), and the review chain of
    every reviewed artifact on the way (§5.5). Level by level, sorted within a
    level. The second item names the artifact whose chain the task is in; it is
    None for an `after` dependent."""
    seen, out, frontier = {root}, [], [root]
    while frontier:
        level: dict[str, str | None] = {}
        for tid in frontier:
            if tid in board and board[tid].min_reviews >= 1:
                for view in chain(board, tid):
                    if view.task_id not in seen:
                        level.setdefault(view.task_id, tid)
        for tid, view in board.items():
            if tid not in seen and any(dep in seen for dep in view.after):
                level.setdefault(tid, None)
        ordered = sorted(level)
        out.extend((tid, level[tid]) for tid in ordered)
        seen.update(ordered)
        frontier = ordered
    return out
```

- [ ] **Step 4: The derived wake**

In `skills/rip-swarm/rip_swarm/waiter.py`, change the board import to `from rip_swarm.board import TaskView, chain, list_tombstones, read_board`, and replace `_blocking_reject` with:

```python
def _blocking_reject(board: dict[str, TaskView]) -> str | None:
    """The first rejected task (sorted) that the board says is not done
    cascading: an unsettled task lists it in `after`, or it is a reviewed
    artifact with a chain task that is neither accepted nor rejected and has no
    live claim (execution proposals §5.5). A chain task claimed when the
    artifact was rejected is left alone until its claim ends, so a live claim
    never makes the wake spin."""
    waited_on = {
        dep
        for view in board.values()
        if not view.settled
        for dep in view.after
        if dep in board and board[dep].rejected
    }
    for tid, view in board.items():
        if view.rejected and view.min_reviews >= 1 and any(
            not task.settled and task.claim != "live" for task in chain(board, tid)
        ):
            waited_on.add(tid)
    return min(waited_on) if waited_on else None
```

- [ ] **Step 5: `status` annotations**

In `skills/rip-swarm/rip_swarm/status.py`:

1. Import `from rip_swarm.reviews import chain_state`.
2. In `status_report`, after `"fixes": {…},` add:

```python
        "reviews": {tid: view.reviews for tid, view in sorted(board.items()) if view.reviews},
        "artifacts": {
            tid: {"kind": view.kind, "needed": view.min_reviews,
                  "rounds": chain_state(hive, board, tid).rounds if view.min_reviews else 0}
            for tid, view in sorted(board.items())
            if view.kind or view.min_reviews
        },
```

3. Add a helper and use it for every `(fixes …)` suffix in `format_status` (the active claims, `inbox_without_claim`, `blocked` and `awaiting_acceptance` lines). Each place that builds `… + (f" (fixes {fixes[tid]})" if tid in fixes else "")` becomes `… + _notes(report, tid)`; the active-claims branch `if tid in fixes: line += f" (fixes {fixes[tid]})"` becomes `line += _notes(report, tid)`. The helper:

```python
def _notes(report: dict, tid: str) -> str:
    """` (spec, reviews 1/2)`, ` (reviews <A>)`, ` (fixes <X>)`: what a task is
    in a review chain or a follow-up (execution proposals §5.5)."""
    out = ""
    art = (report.get("artifacts") or {}).get(tid)
    if art:
        parts = [art["kind"]] if art["kind"] else []
        if art["needed"]:
            parts.append(f"reviews {art['rounds']}/{art['needed']}")
        out += f" ({', '.join(parts)})"
    reviewed = (report.get("reviews") or {}).get(tid)
    if reviewed:
        out += f" (reviews {reviewed})"
    fixed = (report.get("fixes") or {}).get(tid)
    if fixed:
        out += f" (fixes {fixed})"
    return out
```

The artifact line in `test_status_shows_kind_and_rounds` is under `awaiting_acceptance` (`<id> Spec (spec, reviews 1/2)`), the open review under `inbox_without_claim`, and the kinded plan too.

- [ ] **Step 6: Run the tests to see them pass**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_reviews tests.test_cascade tests.test_wait tests.test_status -v 2>&1 | tail -5`
Expected: `OK`.

- [ ] **Step 7: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/rip-swarm/rip_swarm/board.py skills/rip-swarm/rip_swarm/waiter.py \
  skills/rip-swarm/rip_swarm/status.py tests/test_reviews.py tests/test_cascade.py
git commit -m "feat: the cascade and the derived wake cover a rejected artifact's chain; status shows rounds

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 8: Skills for review rounds (spec §5.6, §5.7)

**Files:**
- Modify: `skills/swarm-master/SKILL.md`
- Modify: `skills/swarm-worker/SKILL.md`
- Modify: `skills/rip-swarm/SKILL.md`
- Modify: `tests/test_packaging.py`

**Interfaces:**
- Consumes: every helper surface of Tasks 1–7: `inbox.py --kind/--min-reviews/--reviews`, `claim.py complete --verdict`, `reviews.py`, `reject --cascade` with `(chain of <A>)` skips, the refusal strings in Global Constraints.
- Produces: the master's *Review chains* subsection and the worker's *Review tasks* paragraph, which the rehearsal in Tasks 9 and 10 follows line for line. Keep the headings and the arm names exactly as written here.

Every inline command in a role skill is a fresh shell: it must assign each of `RS`, `HIVE`, `AGENT`, `WORKTREE` it reads (`test_inline_commands_assign_what_they_read`). Every line that names a git verb (`switch`, `merge`, `branch`, `rev-parse`, …) after the word `git` must contain `git -C "$WORKTREE"` (`test_role_skill_git_commands_are_pinned_to_the_worktree`), so prose says `branch -D`, never `git branch -D`. The text below already does both; keep it that way when you adjust wording.

- [ ] **Step 1: Write the failing packaging tests**

Add to `TestPackaging` in `tests/test_packaging.py`:

```python
    def _section(self, text, start, end):
        return text[text.index(start):text.index(end)]

    def test_master_marks_artifacts_with_the_larger_number(self):
        text = self._text("swarm-master")
        plan = self._section(text, "## 4. Plan and post", "## 5.")
        for needle in ("reviews: <kind>=<N>", "the larger of the phrase's number and the profile's",
                       "--kind <kind>", "--min-reviews <N>",
                       "`min_reviews for <kind> is at least <n> (profile)`",
                       "without `--kind`"):
            self.assertIn(needle, plan)

    def test_master_runs_reviews_py_on_every_chain_wake(self):
        text = self._text("swarm-master")
        complete = self._section(text, "### `wake task-finished <T> complete`", "### Review chains")
        self.assertIn("**Review chains first.**", complete)
        self.assertLess(complete.index("**Review chains first.**"), complete.index("1. If `<HIVE>/accepted/<T>.json`"))
        chains = self._section(text, "### Review chains", "### `wake task-finished <T> release`")
        for needle in ('RS=<RS>; HIVE=<HIVE>; python3 "$RS/scripts/reviews.py" --hive "$HIVE" --task <T>',
                       "NEXT=<post-review|post-revise|post-rebase|reject-review|merge|wait|done>",
                       "**`post-rebase` or `reject-review`** first",
                       "read the head first when it is a review",
                       '--note "review rejected: <why>"',
                       "--reviews <A>", "Revise <title> after review <k>",
                       "In every skill command below, `<T>` is `<A>`",
                       "again **before** the fast-forward",
                       "same `HEAD=` and `SHA=`",
                       "accept each id of `CHAIN`, in order, then `A`",
                       "message the author of `A`",
                       "post the replacement first",
                       "reject **`A`**, not the woken review",
                       "reject that review, not `A`",
                       "Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.",
                       '--note "superseded by rebase <id>"'):
            self.assertIn(needle, chains)
        release = self._section(text, "### `wake task-finished <T> release`", "### `wake task-finished <T> reject`")
        self.assertIn("*Review chains*", release)
        reject = self._section(text, "### `wake task-finished <T> reject`", "### Other wakes")
        for needle in ("**`<T>` is a chain task**", "skip steps 2 and 3", "**`<T>` is a reviewed artifact**",
                       "`skipped <id> (chain of <T>) held by", "Skip step 2 when `<T>` has a chain",
                       "its `--kind` and `--min-reviews`"):
            self.assertIn(needle, reject)
        other = self._section(text, "### Other wakes", "## 7.")
        self.assertIn("when `NEXT` is `wait` or `done`", other)

    def test_worker_reviews_with_a_verdict_and_never_resolves(self):
        text = self._text("swarm-worker")
        review = self._section(text, "**Review tasks.**", "Never push a project branch")
        for needle in ("--verdict clean", "--verdict findings",
                       'WORKTREE=<WORKTREE>; if git -C "$WORKTREE" rev-parse -q --verify MERGE_HEAD >/dev/null; then git -C "$WORKTREE" merge --abort; fi',
                       '--note "review <id> cannot build on <sha>: conflict"',
                       "never `HEAD`", "`SYNC=error`, `BUILD=conflict` or `BUILD=error`",
                       "Edit nothing else.", "`<agent> wrote part of <A>`",
                       "`<agent> reviewed <A>`", "`<A> is rejected`"):
            self.assertIn(needle, review)

    def test_reference_names_the_review_surface(self):
        ref = (SKILLS / "rip-swarm" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("--kind", "--min-reviews", "--reviews", "--verdict clean|findings",
                       'scripts/reviews.py" --hive "$HIVE" --task ID', "(chain of <A>)"):
            self.assertIn(needle, ref)
```

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_packaging -v 2>&1 | tail -8`
Expected: the four new tests FAIL.

- [ ] **Step 2: Master — the baton paragraph**

In `skills/swarm-master/SKILL.md` section 2, the paragraph `**Exit 2 from the heartbeat or from \`accept.py\`**` ends with `…in the \`fixes\` walk (section 6), which only ends the walk.` Append:

```markdown
`<A> needs <N> review rounds ending clean, has <k>` in that walk also only ends it: a reviewed artifact is accepted by its own chain (*Review chains*, section 6).
```

- [ ] **Step 3: Master — plan and post**

In section 4:

1. After the first paragraph (it ends `do not re-post it.`) add:

```markdown
A master that takes over trusts the `kind` and `min_reviews` already on the inbox tasks; `status.py` shows them, for example `(spec, reviews 1/2)`. An artifact it posts itself takes the profile's number (`--kind <kind>` alone).
```

2. Change the command in the code block to end with `[--after <task-id>]… [--kind <kind> [--min-reviews <N>]]`.
3. After the code block's line `Each call prints \`task <id>: <title>\`.` sentence (keep the broadcast sentence after it), insert before the broadcast sentence:

```markdown
**Review rounds.** The operator can require independent reviews of the spec, the plan and the implementation. The profile's `min_reviews` map gives each kind a number (the template ships zeros), and the goal may raise it with the fixed phrase `reviews: <kind>=<N> …`, for example `reviews: spec=3 implementation=2`. The phrase can raise a number, never lower it. For each kind, N is the larger of the phrase's number and the profile's:

1. Mark one task per kind as the artifact: the spec, the plan, the implementation. Mark more than one of a kind only when the goal names more.
2. Post each artifact with `--kind <kind>`, which takes the profile's number. If the goal's phrase names that kind, add `--min-reviews <N>` with the phrase's number. A refusal `min_reviews for <kind> is at least <n> (profile)` means the profile's number is larger: post it again with `--kind <kind>` alone.
3. Post the tasks that build on an artifact, and an implementation's subtasks, `--after` the artifact and without `--kind`, so they stay blocked until it is accepted and grow no review chain of their own.

A task posted without `--kind` is not an artifact. With every number at 0 nothing changes: no review task is ever posted.
```

- [ ] **Step 4: Master — the `complete` handler checks for a chain first**

In `### \`wake task-finished <T> complete\``, insert before step 1:

```markdown
0. **Review chains first.** If `<HIVE>/inbox/<T>.json` has `"min_reviews"` of 1 or more, or has `"reviews"`, or has `"fixes": "<A>"` where `<HIVE>/inbox/<A>.json` has `"min_reviews"` of 1 or more, `T` is in a review chain: run *Review chains* (below) with `<T>`, and skip steps 1–9. Its `merge` arm runs steps 4–9 for the artifact.
```

- [ ] **Step 5: Master — the *Review chains* subsection**

Insert this subsection right before `### \`wake task-finished <T> release\` or \`expired\``:

````markdown
### Review chains

A reviewed artifact `A` is a task whose inbox file has `"min_reviews"` of 1 or more. Its chain is `A`, every task with `"fixes": "<A>"` and every task with `"reviews": "<A>"`. Post every revise, rebase and follow-up of a reviewed artifact with `--fixes <A>`, never with `--fixes` of another chain task, so the chain stays flat. A helper reads the board and decides the next step. Every chain wake runs it: a chain task's `complete` (step 0 above), `reject` and `release`, a message about a review that cannot build, and `idle-board` for a chain task.

```bash
RS=<RS>; HIVE=<HIVE>; python3 "$RS/scripts/reviews.py" --hive "$HIVE" --task <T>
```

It prints one line:

```text
NEXT=<post-review|post-revise|post-rebase|reject-review|merge|wait|done> ARTIFACT=<A> ROUNDS=<k>/<N> HEAD=<id> SHA=<sha> CHAIN=<id,id,…> [REVIEW=<id>]
```

Exit 1 prints no `NEXT=` line: report its message to the operator and stop. Otherwise act on `NEXT`, checking the arms in this order. `<title>` is `A`'s title; `SHA`, `HEAD`, `CHAIN`, `REVIEW`, `k` and `N` come from the line.

1. **`post-rebase` or `reject-review`** first: see *A review that cannot build* below. They are about an open review that was released, and `HEAD` may be an earlier review that is fine.
2. For `post-review`, `post-revise` and `merge`, read the head first when it is a review (its inbox file has `"reviews"`): its text at `SHA` (`WORKTREE=<WORKTREE>; git -C "$WORKTREE" show <SHA>`) and the `verdict` in `<HIVE>/claims/<HEAD>.complete.*.json`. If the review is too thin, or its verdict disagrees with its text (for example `clean` over listed findings), reject it: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" reject --hive "$HIVE" --task <HEAD> --agent "$AGENT" --note "review rejected: <why>"`. Then run the helper again and act on the new line. A rejected review is not a round.
3. **`done` or `wait`**: nothing.
4. **`post-review`**: post round `k+1`: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/inbox.py" --hive "$HIVE" --created-by "$AGENT" --reviews <A> --title "Review <k+1> of <title>" --body "Build on <SHA> and review <title> there. Append the review to <doc> as a numbered section, ## <n>. Review <k+1> (<your agent id>, <your harness>); for code, write docs/reviews/<A>-r<k+1>.md instead. Edit nothing else. Complete with --verdict clean only when the review has no finding that needs a change, otherwise --verdict findings."`, with `<doc>` the reviewed document.
5. **`post-revise`**: post `--fixes <A>` titled `Revise <title> after review <k>`. Its body says: build on `<SHA>`; fold each finding of review `<k>` (name them); add a dispositions table after the review section (finding, disposition, where).
6. **`merge`.** This arm is entered from the wake of the clean head, but everything it does is about `A`. In every skill command below, `<T>` is `<A>`, not the woken task. The review branch is `rip-swarm/review-<A>`.
   1. Run step 4 (the `OUTCOME=` block) with `T=<A>` and `SHORT=<SHA>`. On `OUTCOME=merged`, run `A`'s acceptance check in `WORKTREE`, as step 9 does, and leave `WORKTREE` clean.
   2. **Passes:** run the helper with `--task <A>` again **before** the fast-forward. It must print `NEXT=merge` with the same `HEAD=` and `SHA=` as the line that entered this arm (`HEAD=` is the head's task id, `SHA=` its short sha; the `OUTCOME=` line's `SHA=` is the full id and is not compared). Otherwise run the *Falls short* command with `T=<A>`: it switches to `rip-swarm/integration` first, then force-deletes `rip-swarm/review-<A>` (`branch -D`), because the branch is unmerged. Stop the heartbeat loop and act on the new line.
   3. Run the *Passes* item 1 command with `T=<A>` and the `TIP` of the `OUTCOME=` line, and stop the heartbeat loop. Instead of *Passes* items 2 and 3, accept each id of `CHAIN`, in order, then `A`, each with `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/accept.py" --hive "$HIVE" --agent "$AGENT" --task <id> --integration-sha <NEW_TIP>`. If `accept.py` refuses after the fast-forward, report it to the operator and stop. Do not reset integration: this skill never forces, and a worker may already have merged the new tip.
   4. **`OUTCOME=conflict`**: the block has already deleted `rip-swarm/review-<A>`. Post the rebase with step 5's command and `T=<A>`: `--fixes <A>`, titled `Rebase <title> onto rip-swarm/integration`, body `Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.`, with `<SHA>` from the `OUTCOME=` line. Then message the author of `A` (the agent in `<HIVE>/claims/<A>.complete.*.json`), not the head's worker: a reviewer is refused the rebase.
   5. **Falls short**: run the *Falls short* command with `T=<A>`, stop the heartbeat loop, and post the follow-up with `--fixes <A>`. Its body names the `SHA` of the `OUTCOME=` line to build on, and the gap to close.
   6. **Not worth pursuing**: run the *Falls short* command with `T=<A>` and stop the heartbeat loop. Decide now whether the work is still wanted. If it is, post the replacement first: titled `<title> (replaces <A>)`, with `A`'s body and with `--kind` and `--min-reviews` copied from `<HIVE>/inbox/<A>.json`, unless the grep of the reject handler's step 2 finds one already. Then reject **`A`**, not the woken review: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" reject --hive "$HIVE" --task <A> --agent "$AGENT" --note "<why>"`. Its reject handler cascades over the chain and posts no replacement.
   7. **`OUTCOME=dirty`** or **`OUTCOME=error`**: as steps 7 and 8, with `T=<A>`.
   8. **`OUTCOME=badsha`**: the head's `result_ref` is not a commit. At `merge` the head is always a review, so reject that review, not `A`: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" reject --hive "$HIVE" --task <HEAD> --agent "$AGENT" --note "result_ref <result_ref> is not a commit"`, and message its worker. Its reject wake runs the helper, and the head falls back to the task before it.

**A review that cannot build.** A worker that cannot build a review on its sha releases it with the note `review <R> cannot build on <sha>: conflict` and messages the same text. The `release` wake of `<R>`, that message, and an `idle-board` wake for a chain review each run the helper with `--task <R>` and act on `NEXT` with the arms above; no handler parses the note. A master that takes over gets the same `NEXT` from its first chain wake, usually `A`'s unaccepted `complete`.

- `post-rebase`: post a rebase `--fixes <A>`, titled `Rebase <title> onto rip-swarm/integration`, with the body `Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.` (`SHA` from the line, which is the sha from the note). Then reject `REVIEW`: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" reject --hive "$HIVE" --task <REVIEW> --agent "$AGENT" --note "superseded by rebase <id>"`. The rebase is a new head and costs another round.
- `reject-review`: the rebase was posted before a crash. Reject `REVIEW` only, with the same command.

The rebase is posted first, so the reject's own wake finds the chain busy and posts nothing.
````

- [ ] **Step 6: Master — release, reject and other wakes**

1. `### \`wake task-finished <T> release\` or \`expired\``: append the paragraph `If \`<T>\` is in a review chain (as in step 0 of the \`complete\` handler), run *Review chains* with \`<T>\`.`
2. In `### \`wake task-finished <T> reject\``, insert after the first paragraph (`Run every step, in order, …`) and before step 1:

```markdown
Review chains change this handler. First look at `<T>`:

- **`<T>` is a chain task** (its inbox has `"reviews": "<A>"`, or `"fixes": "<A>"` where `<A>` has `"min_reviews"` of 1 or more), and `<A>` is not rejected: run step 1, skip steps 2 and 3, and run step 4. Then run *Review chains* with `<T>`. Step 2 must not replace a chain task, and step 3 would treat `<A>` as orphaned while it waits for its next round. This also resumes a master that died between rejecting a review and posting the next round.
- **`<T>` is a reviewed artifact** (`"min_reviews"` of 1 or more): step 4's `--cascade` also rejects its chain. A line `skipped <id> (chain of <T>) held by <agent> until <expires_at>` is not a stop: go back to wait, and the wake returns when that claim ends. A `skipped` line without `(chain of …)` still stops, as step 4 says. Skip step 2 when `<T>` has a chain (at least one task with `"reviews": "<T>"` or `"fixes": "<T>"`): the replacement was decided before this reject (*Review chains*, `merge` step 6), and this wake comes back while a claimed chain task finishes. An artifact rejected before any review was posted has no chain and runs step 2 as usual.
- Otherwise run the steps as they are.
```

3. In step 2, change `Each post keeps the old task's body and its \`--fixes\`, if it has one.` to `Each post keeps the old task's body, its \`--fixes\`, and its \`--kind\` and \`--min-reviews\`, if it has them; a replacement copies them whatever the profile says now.`
4. In `### Other wakes`:
   - `wake message` bullet: append `A message \`review <R> cannot build on <sha>: conflict\` is handled under *Review chains*.`
   - Replace the `wake idle-board <T>` bullet with: `` `wake idle-board <T>`: if `<T>` is in a review chain, run *Review chains* with `<T>` first. Either way, and also when `NEXT` is `wait` or `done`, tell the operator nobody is claiming `<T>`: worker briefs may exclude it, or every remaining worker may be refused it (an author may not review, a reviewer may not fix). Keep waiting. ``

- [ ] **Step 7: Worker — review tasks**

In `skills/swarm-worker/SKILL.md` section 5, insert before the paragraph `**Exit 2 from \`heartbeat\` or \`complete\`**`:

````markdown
**Review tasks.** A task whose inbox file (`<HIVE>/inbox/<id>.json`) has `"reviews"` asks you to review another task's result, not to change it. Do it this way:

1. Run step 1 with `BUILD_ON=` the sha the body names, as for a task with `fixes`.
2. If that does not merge cleanly (`SYNC=error`, `BUILD=conflict` or `BUILD=error`), resolve nothing. In all three cases:
   1. abort a merge only if one is in progress: `WORKTREE=<WORKTREE>; if git -C "$WORKTREE" rev-parse -q --verify MERGE_HEAD >/dev/null; then git -C "$WORKTREE" merge --abort; fi`;
   2. release the task: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" release --hive "$HIVE" --task <id> --agent "$AGENT" --note "review <id> cannot build on <sha>: conflict"`. `<id>` is this review task and `<sha>` is the `BUILD_ON` value, the sha the body names: never `HEAD`, which after the abort is the integration tip. A master that takes over has only this note to go on;
   3. message the master with the same text: `RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/message.py" --hive "$HIVE" --from "$AGENT" --to orchestrator --type note --body "review <id> cannot build on <sha>: conflict"`, and go back to waiting.

   A resolution would land inside the review commit and could be stamped clean, so the master posts a rebase instead.
3. Read the artifact at that sha, and write the review where the body says: a numbered section appended to the reviewed document, or the file it names for code.
4. Edit nothing else.
5. Commit it as step 4 does, and complete with step 5's command, `--result-ref` included, plus `--verdict clean` or `--verdict findings`. The verdict is `clean` only when the review has no finding that needs a change. Exit 1 naming `--verdict` means the flag was wrong: the claim is still yours, so fix it and run `complete` again.

`claim` refuses a task of a review chain with exit 2 in three cases: `<agent> wrote part of <A>` (you may not review what you wrote), `<agent> reviewed <A>` (a reviewer may not fix, even after only releasing a review), and `<A> is rejected`. Go back to waiting, as for any exit 2 on `claim`.

````

- [ ] **Step 8: The reference**

In `skills/rip-swarm/SKILL.md`, in the `## Tasks, claims, acceptance` code block:

1. Change the `inbox.py` line to end `[--after ID]… [--fixes ID] [--kind K [--min-reviews N]] [--reviews ID]`.
2. Change the `complete` line to end `--result-ref "rip-swarm/$AGENT@SHA" [--verdict clean|findings]`.
3. Add the line `python3 "$SKILL_DIR/scripts/reviews.py" --hive "$HIVE" --task ID`.

After the rules list, add:

```markdown
Review rounds (a reviewed artifact has `min_reviews` of 1 or more):

- `--kind K` marks an artifact. Its `min_reviews` defaults to the profile's `min_reviews[K]` and may not be lower, except in an exact copy: a post titled `… (replaces <X>)` of a rejected `X` with the same kind and number.
- `--reviews A` posts a review task of artifact `A`. It takes no `--fixes`, `--kind` or `--min-reviews`. Revise, rebase and follow-up tasks of `A` use `--fixes A`.
- A review task completes with `--verdict clean|findings`, and no other task takes one (exit 1 either way; nothing is written).
- `claim` refuses a review of `A` to anyone who completed `A` or a fix of it, a fix of `A` to anyone who claimed a review of it, and any chain task of a rejected `A`.
- `accept` refuses `A` until it has `min_reviews` rounds and its latest chain result is a clean review. `--via` does not bypass this.
- `reviews.py` prints the chain's next step: `NEXT=… ARTIFACT=A ROUNDS=k/N HEAD=… SHA=… CHAIN=… [REVIEW=…]`, or exits 1.
- `reject --cascade` of a reviewed artifact also rejects its chain. A held chain task prints `skipped <id> (chain of <A>) held by <agent> until <time>`.
```

- [ ] **Step 9: Run the tests**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_packaging -v 2>&1 | tail -5`
Expected: `OK`. If a pre-existing needle breaks because a sentence moved, keep the needle's meaning and update its text.

- [ ] **Step 10: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add skills/swarm-master/SKILL.md skills/swarm-worker/SKILL.md skills/rip-swarm/SKILL.md tests/test_packaging.py
git commit -m "docs(skills): review rounds: artifacts, the chain arms, review tasks

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 9: Rehearsal of review chains — rounds, independence, takeover (spec §5.9 rehearsal cases)

**Files:**
- Modify: `tests/test_rehearsal.py`

**Interfaces:**
- Consumes: the skills as Task 8 wrote them. The rehearsal is the skill text, minus the model: each helper method below cites the arm it plays, and must do what that arm says, line for line. Where this code and the skill disagree, the skill is the authority: fix the rehearsal, or report the skill defect.
- Produces: `Session.review`, `Session.fold`, `Session.body_sha`, `Session.complete(task, *extra)`; `Master.chain_wake`, `Master.merge_arm`, `Master.handle_release`, `Master.handle_messages`, `Master.handle_idle`, `Master.reviews`, `Master.in_chain`, `Master.inbox`; `handle_complete(task, check, **arms)` and `handle_reject(task, wanted=(), **arms)` with the chain cases. Task 10 uses all of them.

The model's choices are parameters: `check(wt)` is the master's acceptance check on the merged tree, `judge(head)` returns why a review is rejected (or `None`), `worth` is `False` for *not worth pursuing*, and `wanted` says whether the work is still wanted when it is not.

- [ ] **Step 1: Session helpers for review tasks and folds**

In `tests/test_rehearsal.py`:

1. Change the imports: `from rip_swarm.board import artifact_of, chain, fixers, posting_order, read_board`.
2. Replace `Session.complete` with:

```python
    def complete(self, task, *extra):
        sha = git(self.wt, "rev-parse", "--short", "HEAD")      # what /swarm-worker records
        rc, out = cli("complete", "--hive", self.hive, "--task", task, "--agent", self.agent,
                      "--result-ref", f"rip-swarm/{self.agent}@{sha}", *extra, at=self.now)
        assert rc == 0, out
        return sha
```

3. Add to `Session`:

```python
    def body_sha(self, task):
        """The sha a review, revise, rebase or follow-up body names to build on."""
        sync(self.hive)
        body = json.loads((self.hive / "inbox" / f"{task}.json").read_text(encoding="utf-8"))["body"]
        return re.search(r"\b[0-9a-f]{7,40}\b", body).group(0)

    def review(self, task, verdict, text, *, message=True, result_ref=None):
        """/swarm-worker *Review tasks*, step by step. Returns the short sha, or
        "refused" (claim exit 2), or "released" (the build did not merge cleanly)."""
        if self.claim(task) != 0:
            return "refused"
        sha = self.body_sha(task)
        sync_, build, _ = self.start_task(sha)                                     # step 1
        if sync_ == "error" or build in ("conflict", "error"):                     # step 2
            if run(self.wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
                git(self.wt, "merge", "--abort")
            note = f"review {task} cannot build on {sha}: conflict"                # the body's sha
            rc, out = cli("release", "--hive", self.hive, "--task", task, "--agent", self.agent,
                          "--note", note, at=self.now)
            assert rc == 0, out
            if message:
                rc, out = cli("message", "--hive", self.hive, "--from", self.agent,
                              "--to", "orchestrator", "--type", "note", "--body", note, at=self.now)
                assert rc == 0, out
            return "released"
        doc = self.wt / "spec.md"                                                  # steps 3-4
        doc.write_text(doc.read_text(encoding="utf-8") + text, encoding="utf-8")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", f"{task}: review")
        if result_ref is not None:                                                 # a slip, for badsha
            rc, out = cli("complete", "--hive", self.hive, "--task", task, "--agent", self.agent,
                          "--result-ref", result_ref, "--verdict", verdict, at=self.now)
            assert rc == 0, out
            return result_ref
        return self.complete(task, "--verdict", verdict)                          # step 5

    def fold(self, task, text):
        """A revise, rebase or follow-up of a reviewed artifact: /swarm-worker §5
        with BUILD_ON from the body. A conflict is the work: `text` resolves it."""
        if self.claim(task) != 0:
            return "refused"
        sync_, build, _ = self.start_task(self.body_sha(task))
        assert sync_ in ("merged", "reset", "none") and build in ("merged", "conflict"), (sync_, build)
        (self.wt / "spec.md").write_text(text, encoding="utf-8")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", f"{task}: fold")
        return self.complete(task)
```

- [ ] **Step 2: The master's chain arms**

1. In `Master.review_block`, right after `full = resolved.stdout.strip()`, add `self.last_full = full` (the `OUTCOME=` line's `SHA=`).
2. Change `Master.handle_complete` to take the chain arms and run step 0 first:

```python
    def handle_complete(self, task, check, **arms):
        """`wake task-finished <task> complete` per /swarm-master §6."""
        sync(self.hive)
        if self.in_chain(task):                                                    # step 0
            return self.chain_wake(task, check=check, **arms)
        if (self.hive / "accepted" / f"{task}.json").exists():
            ...  # the rest of the method stays exactly as it is
```

3. Add to `Master`:

```python
    def inbox(self, task):
        return json.loads((self.hive / "inbox" / f"{task}.json").read_text(encoding="utf-8"))

    def in_chain(self, task):
        """Step 0: min_reviews >= 1, or `reviews`, or `fixes` of such an artifact."""
        doc = self.inbox(task)
        if (doc.get("min_reviews") or 0) >= 1 or doc.get("reviews"):
            return True
        return bool(doc.get("fixes")) and (self.inbox(doc["fixes"]).get("min_reviews") or 0) >= 1

    def reviews(self, task):
        rc, out = cli("reviews", "--hive", self.hive, "--task", task, at=self.now)
        assert rc == 0, out
        return dict(field.split("=", 1) for field in out.split())

    def chain_wake(self, task, *, check=None, judge=None, worth=True, wanted=False):
        """/swarm-master *Review chains*: run reviews.py and act on NEXT, arm by arm.
        Returns what it did: `review <id>`, `revise <id>`, `rebase <id>`,
        `reject <id>`, `post <id>`, `accepted`, `short <id>`, `moved`, or an outcome."""
        sync(self.hive)
        line = self.reviews(task)
        nxt, a, head = line["NEXT"], line["ARTIFACT"], line["HEAD"]
        title = self.inbox(a)["title"]
        if nxt in ("post-rebase", "reject-review"):                                # arm 1
            done = []
            if nxt == "post-rebase":
                reb = self.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a,
                                "--body", f"Merge {line['SHA']} onto rip-swarm/integration and "
                                          "resolve the conflict; the resolution is the work.")
                done.append(f"rebase {reb}")
            else:
                board = read_board(self.hive, self.now)
                reb = max((v for v in chain(board, a) if v.fixes == a), key=posting_order).task_id
            self.reject(line["REVIEW"], f"superseded by rebase {reb}")
            return done + [f"reject {line['REVIEW']}"]
        if nxt in ("post-review", "post-revise", "merge") and judge \
                and self.inbox(head).get("reviews"):                               # arm 2
            why = judge(head)
            if why:
                self.reject(head, f"review rejected: {why}")
                return [f"reject {head}", *self.chain_wake(task, check=check, judge=judge,
                                                            worth=worth, wanted=wanted)]
        if nxt in ("done", "wait"):                                                # arm 3
            return []
        k = int(line["ROUNDS"].split("/")[0])
        if nxt == "post-review":                                                   # arm 4
            r = self.post(f"Review {k + 1} of {title}", "--reviews", a, "--body",
                          f"Build on {line['SHA']} and review {title} there. Append the review "
                          f"to spec.md as a numbered section, Review {k + 1}. Edit nothing else. "
                          "Complete with --verdict clean only when the review has no finding "
                          "that needs a change, otherwise --verdict findings.")
            return [f"review {r}"]
        if nxt == "post-revise":                                                   # arm 5
            f = self.post(f"Revise {title} after review {k}", "--fixes", a, "--body",
                          f"Build on {line['SHA']}; fold each finding of review {k} and add "
                          "a dispositions table after the review section.")
            return [f"revise {f}"]
        return self.merge_arm(a, line, check, worth, wanted)                       # arm 6

    def merge_arm(self, a, line, check, worth, wanted):
        """*Review chains* arm 6: everything is about A; `<T>` is A throughout."""
        title = self.inbox(a)["title"]
        outcome = self.review_block(a, line["SHA"])                                # 6.1
        if outcome == "conflict":                                                  # 6.4
            reb = self.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a,
                            "--body", f"Merge {self.last_full} onto rip-swarm/integration and "
                                      "resolve the conflict; the resolution is the work.")
            return [f"rebase {reb}"]
        if outcome == "badsha":                                                    # 6.8
            self.reject(line["HEAD"], f"result_ref rip-swarm/?@{line['SHA']} is not a commit")
            return [f"reject {line['HEAD']}"]
        if outcome != "merged":                                                    # 6.7
            return [outcome]
        review = f"rip-swarm/review-{a}"
        if check(self.wt):
            again = self.reviews(a)                                                # 6.2
            if (again["NEXT"], again["HEAD"], again["SHA"]) != ("merge", line["HEAD"], line["SHA"]):
                assert self._ok("switch", "rip-swarm/integration") and self._ok("branch", "-D", review)
                return ["moved", *self.chain_wake(a, check=check)]
            if git(self.wt, "status", "--porcelain"):                              # 6.3: Passes item 1
                return ["dirty"]
            if git(self.wt, "rev-parse", "rip-swarm/integration") != self.last_tip:
                return ["moved"]
            assert (self._ok("switch", "rip-swarm/integration")
                    and self._ok("merge", "--ff-only", review) and self._ok("branch", "-d", review))
            new_tip = git(self.wt, "rev-parse", "HEAD")
            for tid in [*filter(None, line["CHAIN"].split(",")), a]:
                rc, out = cli("accept", "--hive", self.hive, "--agent", self.agent, "--task", tid,
                              "--integration-sha", new_tip, at=self.now)
                assert rc == 0, out
            return ["accepted"]
        assert self._ok("switch", "rip-swarm/integration") and self._ok("branch", "-D", review)
        if worth:                                                                  # 6.5
            f = self.post(f"Follow up {title}", "--fixes", a, "--body",
                          f"Build on {self.last_full}: close the gap the check found.")
            return [f"short {f}"]
        done = []                                                                  # 6.6
        if wanted and not [r for r in self.replacements(a)
                           if not read_board(self.hive, self.now)[r].rejected]:
            doc = self.inbox(a)
            rep = self.post(f"{title} (replaces {a})", "--kind", doc["kind"],
                            "--min-reviews", str(doc["min_reviews"]), "--body", doc.get("body", ""))
            done.append(f"post {rep}")
        self.reject(a, "not worth pursuing")
        return done + [f"reject {a}"]

    def handle_release(self, task, **arms):
        """`wake task-finished <task> release`: a chain task runs *Review chains*."""
        sync(self.hive)
        return self.chain_wake(task, **arms) if self.in_chain(task) else []

    def handle_messages(self, **arms):
        """`wake message`: a cannot-build message runs *Review chains* for its review."""
        rc, out = cli("messages", "--hive", self.hive, "--to", self.agent, "--new", at=self.now)
        assert rc == 0, out
        done = []
        for review in re.findall(r"review (task_\w+) cannot build on [0-9a-f]+: conflict", out):
            done += self.chain_wake(review, **arms)
        return done

    def handle_idle(self, task, **arms):
        """`wake idle-board <task>`: run the chain arms first; always report."""
        sync(self.hive)
        done = self.chain_wake(task, **arms) if self.in_chain(task) else []
        return done + [f"report {task}"]
```

- [ ] **Step 3: The reject handler's chain cases**

In `Master.handle_reject`, change the signature to `def handle_reject(self, task, wanted=(), **arms):` and, after `root = not re.fullmatch(...)`, add:

```python
        a = artifact_of(board, task)
        chain_task = a is not None and a != task and not board[a].rejected         # chain case 1
        has_chain = board[task].min_reviews >= 1 and bool(chain(board, task))       # chain case 2
```

Guard step 2 with `if root and not chain_task and not has_chain:` (was `if root:`), and step 3 with `and not chain_task` added to its condition. Replace the Task 3 step 4 block with:

```python
        rc, out = cli("reject", "--hive", self.hive, "--task", task,               # step 4
                      "--agent", self.agent, "--cascade", at=self.now)
        assert rc == 0, out
        lines = out.splitlines()
        done += [f"reject {line.split()[1]}" for line in lines if line.startswith("rejected ")]
        if any(line.startswith("skipped ") and "(chain of " not in line for line in lines):
            return done + ["stop"]                                                 # §2's stop
        if chain_task:
            done += self.chain_wake(task, **arms)
        return done
```

- [ ] **Step 4: Write the rehearsal cases**

Add a helper and the tests to `TestRehearsal`:

```python
    def _artifact(self, n):
        return self.m.post("Spec", "--kind", "spec", "--min-reviews", str(n), "--body", "Write spec.md")

    def _write_artifact(self, a, text="# Spec\nv1\n"):
        self.assertEqual(self.w1.claim(a), 0)
        self.w1.start_task()
        self.w1.work("spec.md", text, f"{a}: spec")
        return self.w1.complete(a)

    @staticmethod
    def _ids(done, verb):
        return [item.split()[1] for item in done if item.startswith(f"{verb} ")]

    def _spec(self):
        return git(self.repo, "show", "rip-swarm/integration:spec.md")

    def test_spec_with_two_rounds_findings_then_clean(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.assertEqual(self.w1.review(r1, "clean", "x"), "refused")               # the author
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: say why\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        self.assertEqual(self.w2.fold(f1, "x"), "refused")                          # the reviewer
        self.w1.fold(f1, "# Spec\nv2, says why\n\n## Review 1\nfinding: say why\n\n"
                         "### Dispositions\n| say why | accepted | v2 |\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f1} complete"))
        [r2] = self._ids(self.m.handle_complete(f1, None), "review")
        self.w2.review(r2, "clean", "\n## Review 2\nno findings\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} complete"))
        check = lambda wt: "Dispositions" in (wt / "spec.md").read_text(encoding="utf-8")
        self.assertEqual(self.m.handle_complete(r2, check), ["accepted"])
        for tid in (r1, f1, r2, a):
            self.assertTrue((self.m.hive / "accepted" / f"{tid}.json").exists(), tid)
        spec = self._spec()
        for needle in ("## Review 1", "### Dispositions", "## Review 2"):
            self.assertIn(needle, spec)
        self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{a}").returncode, 0)
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_the_author_is_refused_the_review_and_the_reviewer_the_revise(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        err = io.StringIO()
        rc, _ = cli("claim", "--hive", self.w1.hive, "--task", r1, "--agent", self.w1.agent, err=err)
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.w1.agent} wrote part of {a}", err.getvalue())
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: x\n")
        self.m.tick()
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        err = io.StringIO()
        rc, _ = cli("claim", "--hive", self.w2.hive, "--task", f1, "--agent", self.w2.agent, err=err)
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.w2.agent} reviewed {a}", err.getvalue())

    def test_two_clean_rounds_by_the_same_reviewer(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        [r2] = self._ids(self.m.handle_complete(r1, None), "review")               # 1/2: again
        self.assertNotEqual(self.w2.review(r2, "clean", "\n## Review 2\nno findings\n"), "refused")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(r2, lambda wt: True), ["accepted"])

    def test_a_takeover_mid_chain_posts_nothing_twice(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: x\n")
        self.m.tick()
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        # The master dies here. A new one joins after its baton expired.
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        self.w1.now = self.w2.now = LATER
        posted = len(list((b.hive / "inbox").glob("task_*.json")))
        for task in sorted([a, r1]):                                               # bare completes
            self.assertEqual(b.tick(), Wake("task-finished", f"{task} complete"))
            self.assertEqual(b.handle_complete(task, None), [])                    # NEXT=wait: F1 is open
        self.assertEqual(b.tick(), Wake("idle-board", f1))                         # open since T0
        self.assertEqual(b.handle_idle(f1), [f"report {f1}"])
        self.assertEqual(len(list((b.hive / "inbox").glob("task_*.json"))), posted)
        self.w1.fold(f1, "# Spec\nv2\n\n## Review 1\nfinding: x\n\n### Dispositions\n| x | done |\n")
        self.assertEqual(b.tick(), Wake("task-finished", f"{f1} complete"))
        self.assertEqual(len(self._ids(b.handle_complete(f1, None), "review")), 1)
        self.assertEqual(b.handle_complete(f1, None), [])                          # a repeat posts nothing

    def test_a_thin_or_contradicted_review_is_rejected_and_does_not_count(self):
        for text, why in (("\n## Review 1\nok\n", "too thin"),
                          ("\n## Review 1\nfinding: the spec skips errors\n", "clean over a listed finding")):
            with self.subTest(why=why):
                self.tearDown()
                self.setUp()
                a = self._artifact(1)
                self._write_artifact(a)
                self.m.tick()
                [r1] = self._ids(self.m.handle_complete(a, None), "review")
                self.w2.review(r1, "clean", text)
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
                done = self.m.handle_complete(r1, None, judge=lambda head: why)
                self.assertEqual(done[0], f"reject {r1}")
                [r2] = self._ids(done, "review")                                   # round 1 again
                self.assertEqual(self.m.reviews(a)["ROUNDS"], "0/1")
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
                self.assertEqual(self.m.handle_reject(r1), [])                     # NEXT=wait
                self.w2.review(r2, "clean", "\n## Review 2\nchecked every section; no findings\n")
                self.m.tick()
                self.assertEqual(self.m.handle_complete(r2, lambda wt: True, judge=lambda head: None),
                                 ["accepted"])
                self.assertFalse((self.m.hive / "accepted" / f"{r1}.json").exists())

    def test_a_master_that_dies_after_rejecting_a_review_posts_on_the_reject_wake(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nok\n")
        self.m.tick()
        self.m.reject(r1, "review rejected: too thin")                            # dies before the rerun
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(len(self._ids(self.m.handle_reject(r1), "review")), 1)

    def test_the_merge_rechecks_reviews_before_the_fast_forward(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        tip = git(self.m.wt, "rev-parse", "rip-swarm/integration")

        def check(wt):                                   # the board moves during the check
            self.m.reject(r1, "review rejected: found late")
            return True

        done = self.m.handle_complete(r1, check)
        self.assertEqual(done[0], "moved")
        self.assertEqual(len(self._ids(done, "review")), 1)
        self.assertEqual(git(self.m.wt, "rev-parse", "rip-swarm/integration"), tip)
        self.assertEqual(git(self.m.wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{a}").returncode, 0)

    def test_zero_rounds_is_todays_path(self):
        a = self.m.post("Spec", "--kind", "spec", "--body", "Write spec.md")      # the profile's 0
        self.assertEqual(self.m.inbox(a)["min_reviews"], 0)
        self._write_artifact(a, "T\n")
        self.m.tick()
        self.assertFalse(self.m.in_chain(a))
        self.assertEqual(self.m.handle_complete(a, lambda wt: True), "pass")
        self.assertEqual(len(list((self.m.hive / "inbox").glob("task_*.json"))), 1)
        self.assertEqual(self.m.tick(), Wake("all-complete"))
```

In `test_zero_rounds_is_todays_path`, `_write_artifact` writes `spec.md`, and `handle_complete`'s ordinary path runs `check(wt)` on it; `lambda wt: True` is enough there.

- [ ] **Step 5: Run the rehearsal**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_rehearsal -v 2>&1 | tail -8`
Expected: `OK`. A failure here is a disagreement between the skill, the helpers and this rehearsal: find which one is wrong against the spec, and fix that one.

- [ ] **Step 6: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add tests/test_rehearsal.py
git commit -m "test: rehearse review rounds, independence and a takeover mid-chain

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Rehearsal of review chains — failure paths (spec §5.9 rehearsal cases)

**Files:**
- Modify: `tests/test_rehearsal.py`

**Interfaces:**
- Consumes: the Task 9 helpers.

- [ ] **Step 1: Write the cases**

Add to `TestRehearsal`:

```python
    def _conflicting_review(self, message=True):
        """A's review cannot build: integration took another task's spec.md first.
        Returns (a, sha of A, the released review)."""
        a = self._artifact(1)
        x = self.m.post("Other", "--body", "Write spec.md too")
        sa = self._write_artifact(a)
        self.assertEqual(self.w2.claim(x), 0)
        self.w2.start_task()
        self.w2.work("spec.md", "# Other\n", f"{x}: other")
        self.w2.complete(x)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{x} complete"))
        self.assertEqual(self.m.handle_complete(x, lambda wt: True), "pass")
        self.assertEqual(self.w2.review(r1, "clean", "x", message=message), "released")
        stone = next((self.w2.hive / "claims").glob(f"{r1}.release.*.json"))
        note = json.loads(stone.read_text(encoding="utf-8"))["note"]
        self.assertEqual(note, f"review {r1} cannot build on {sa}: conflict")  # the body's sha
        self.assertNotEqual(git(self.w2.wt, "rev-parse", "--short", "HEAD"), sa)
        self.assertNotEqual(run(self.w2.wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        return a, sa, r1

    def _rebases(self, a):
        sync(self.m.hive)
        board = read_board(self.m.hive, self.m.now)
        return [v.task_id for v in chain(board, a) if v.title.startswith("Rebase ")]

    def test_a_review_that_cannot_build_is_rebased_once_and_costs_a_round(self):
        for message in (True, False):
            with self.subTest(message=message):
                self.tearDown()
                self.setUp()
                a, sa, r1 = self._conflicting_review(message=message)
                if message:                                                        # the message first
                    self.assertEqual(self.m.tick(), Wake("message"))
                    done = self.m.handle_messages()
                    self.assertEqual(self._ids(done, "reject"), [r1])
                    # Both tombstones wake now; by name `reject` sorts before `release`.
                    wakes = sorted(self.m.tick().detail for _ in range(2))
                    self.assertEqual(wakes, sorted([f"{r1} reject", f"{r1} release"]))
                    self.assertEqual(self.m.handle_release(r1), [])                # posts nothing more
                    self.assertEqual(self.m.handle_reject(r1), [])
                else:                                                              # the release alone
                    self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} release"))
                    self.assertEqual(self._ids(self.m.handle_release(r1), "reject"), [r1])
                    self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
                    self.assertEqual(self.m.handle_reject(r1), [])
                [f] = self._rebases(a)
                self.assertEqual(self.w1.body_sha(f), sa)
                self.assertEqual(self.w2.fold(f, "x"), "refused")                  # released a review
                self.w1.fold(f, "# Spec\nv1\n# Other\n")                           # the author resolves
                self.m.tick()
                [r2] = self._ids(self.m.handle_complete(f, None), "review")
                self.assertEqual(self.m.reviews(a)["ROUNDS"], "0/1")               # another round
                self.w2.review(r2, "clean", "\n## Review 1\nno findings\n")
                self.m.tick()
                self.assertEqual(self.m.handle_complete(r2, lambda wt: True), ["accepted"])
                self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_a_takeover_after_a_release_posts_the_rebase_then_recovers_a_crash(self):
        a, sa, r1 = self._conflicting_review()
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        self.w1.now = self.w2.now = LATER
        self.assertEqual(b.tick(), Wake("task-finished", f"{a} complete"))        # its first chain wake
        line = b.reviews(a)
        self.assertEqual((line["NEXT"], line["REVIEW"], line["SHA"]), ("post-rebase", r1, sa))
        title = b.inbox(a)["title"]
        f = b.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a, "--body",
                   f"Merge {sa} onto rip-swarm/integration and resolve the conflict; "
                   "the resolution is the work.")                                  # then b dies
        self.w1.fold(f, "# Spec\nv1\n# Other\n")
        self.assertEqual(b.tick(), Wake("task-finished", f"{f} complete"))
        self.assertEqual(b.handle_complete(f, None), [f"reject {r1}"])            # NEXT=reject-review
        self.assertEqual(b.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(len(self._ids(b.handle_reject(r1), "review")), 1)
        self.assertEqual(self._rebases(a), [f])

    def test_rejecting_a_revise_runs_reviews_and_never_the_orphan_step(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: x\n")
        self.m.tick()
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        self.m.reject(f1, "wrong approach")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f1} reject"))
        done = self.m.handle_reject(f1, wanted={f1, a})
        self.assertFalse([d for d in done if d.startswith(("orphan", "post"))], done)
        self.assertEqual(len(self._ids(done, "revise")), 1)
        self.assertFalse(read_board(self.m.hive, T0)[a].rejected)

    def test_rejecting_an_artifact_while_a_review_is_claimed(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        [r2] = self._ids(self.m.handle_complete(r1, None), "review")
        self.assertEqual(self.w2.claim(r2), 0)                                     # held at the reject
        self.m.reject(a, "not worth pursuing")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
        before = int(git(self.origin, "rev-list", "--count", "swarm"))
        done = self.m.handle_reject(a, wanted={a})                                 # has a chain: no step 2
        self.assertEqual(done, [f"reject {r1}"])                                   # the skip is no stop
        self.assertEqual(int(git(self.origin, "rev-list", "--count", "swarm")), before + 1)
        self.assertEqual(self.m.replacements(a), [])
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(self.m.handle_reject(r1), [])                             # a cascade note
        self.assertIsNone(self.m.tick())                                           # silent: r2 is held
        self.w2.start_task(self.w2.body_sha(r2))
        self.w2.work("spec.md", "# Spec\nv1\n\n## Review 2\nno findings\n", f"{r2}: review")
        self.w2.complete(r2, "--verdict", "clean")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} complete"))
        self.assertEqual(self.m.handle_complete(r2, None), [])                     # NEXT=done
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))      # derived
        self.assertEqual(self.m.handle_reject(a, wanted={a}), [f"reject {r2}"])
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} reject"))
        self.assertEqual(self.m.handle_reject(r2), [])
        self.assertEqual(self.m.replacements(a), [])
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_badsha_at_merge_rejects_the_head_review_not_the_artifact(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n",
                       result_ref=f"rip-swarm/{self.w2.agent}@deadbee")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(r1, lambda wt: True), [f"reject {r1}"])
        self.assertFalse(read_board(self.m.hive, T0)[a].rejected)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(len(self._ids(self.m.handle_reject(r1), "review")), 1)    # the head fell back

    def test_a_review_whose_build_fails_without_a_merge_still_releases(self):
        a = self._artifact(1)
        self._write_artifact(a)
        r1 = self.m.post("Review 1 of Spec", "--reviews", a, "--body",
                         "Build on deadbee and review Spec there.")                # no such commit
        self.assertEqual(self.w2.review(r1, "clean", "x", message=False), "released")
        stone = next((self.w2.hive / "claims").glob(f"{r1}.release.*.json"))
        self.assertEqual(json.loads(stone.read_text(encoding="utf-8"))["note"],
                         f"review {r1} cannot build on deadbee: conflict")

    def test_not_worth_pursuing_at_merge(self):
        for wanted in (False, True):
            with self.subTest(wanted=wanted):
                self.tearDown()
                self.setUp()
                a = self._artifact(1)
                self._write_artifact(a)
                self.m.tick()
                [r1] = self._ids(self.m.handle_complete(a, None), "review")
                self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
                self.m.tick()
                tip = git(self.m.wt, "rev-parse", "rip-swarm/integration")
                done = self.m.handle_complete(r1, lambda wt: False, worth=False, wanted=wanted)
                self.assertEqual(done[-1], f"reject {a}")
                self.assertEqual(git(self.m.wt, "rev-parse", "rip-swarm/integration"), tip)
                self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                        f"refs/heads/rip-swarm/review-{a}").returncode, 0)
                reps = self.m.replacements(a)
                self.assertEqual(len(reps), 1 if wanted else 0)
                if wanted:                                                         # posted before the reject
                    self.assertEqual(done[0], f"post {reps[0]}")
                    doc = self.m.inbox(reps[0])
                    self.assertEqual((doc["kind"], doc["min_reviews"]), ("spec", 1))
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
                self.assertEqual(self.m.handle_reject(a, wanted={a}), [f"reject {r1}"])
                self.assertEqual(self.m.replacements(a), reps)                     # none posted again
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
                self.assertEqual(self.m.handle_reject(r1), [])
                if not wanted:
                    self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_shortfall_at_merge_posts_a_follow_up_that_costs_a_round(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        tip = git(self.m.wt, "rev-parse", "rip-swarm/integration")
        [f] = self._ids(self.m.handle_complete(r1, lambda wt: False), "short")
        self.assertEqual(git(self.m.wt, "rev-parse", "rip-swarm/integration"), tip)
        self.assertEqual(self.m.inbox(f)["fixes"], a)
        self.w1.fold(f, "# Spec\nv2\n\n## Review 1\nno findings\n")
        self.m.tick()
        self.assertEqual(len(self._ids(self.m.handle_complete(f, None), "review")), 1)
        self.assertEqual(self.m.reviews(a)["ROUNDS"], "1/1")                       # the head is the fix

    def test_an_idle_review_nobody_may_claim_is_reported(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.assertEqual(self.w1.review(r1, "clean", "x"), "refused")              # w2's brief excludes it
        self.m.now = T0 + timedelta(minutes=11)
        self.assertEqual(self.m.tick(), Wake("idle-board", r1))
        self.assertEqual(self.m.handle_idle(r1), [f"report {r1}"])
```

Two notes for the implementer:
- `test_rejecting_an_artifact_while_a_review_is_claimed` does the held review's work by hand after the cascade, because `Session.review` claims first and the claim is already held.
- `test_not_worth_pursuing_at_merge` with `wanted=True` leaves the replacement open, so it stops before `all-complete`.

- [ ] **Step 2: Run the rehearsal**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_rehearsal -v 2>&1 | tail -8`
Expected: `OK`. As in Task 9, a failure is a disagreement to resolve against the spec, not a test to loosen.

- [ ] **Step 3: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add tests/test_rehearsal.py
git commit -m "test: rehearse cannot-build rebases, rejects, badsha and idle reviews in a chain

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
### Task 11: Documentation

**Files:**
- Modify: `docs/specs/2026-09-26-roles-and-install.md` (§7.6, §8 step 3.6, §9 step 5 reject handler step 4, §10)
- Modify: `docs/specs/2026-09-26-execution-proposals.md` (status line only)
- Modify: `README.md`
- Modify: `skills/rip-swarm/templates/_swarm/PROTOCOL.md`
- Modify: `tests/test_packaging.py`

**Interfaces:**
- Consumes: the behaviour of Tasks 1–10.
- Never rewrite a review or dispositions section of either spec: those are the review history. Change only the body text named below.

- [ ] **Step 1: Write the failing packaging test**

Add to `TestPackaging`:

```python
    def test_docs_name_the_execution_proposals(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("`reviews.py`", readme)
        self.assertIn("docs/specs/2026-09-26-execution-proposals.md", readme)
        protocol = (SKILLS / "rip-swarm" / "templates" / "_swarm" / "PROTOCOL.md").read_text(encoding="utf-8")
        for needle in ("min_reviews", "reject --cascade", "--verdict"):
            self.assertIn(needle, protocol)
```

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_packaging -v 2>&1 | tail -4`
Expected: FAIL.

- [ ] **Step 2: The roles spec**

In `docs/specs/2026-09-26-roles-and-install.md`:

1. §7.6, second bullet: replace `(15 minutes at the default 30m)` with `(the heartbeat loop, execution proposals §3, reads the lease from the profile and does this for it)`.
2. §8 procedure step 3.6: delete `, then \`message --to orchestrator --type result --body "<task id>: <one-line headline>"\`, or \`--to '*'\` if no orchestrator is seated`, and after `Go back to 3.1.` add ` \`complete\` is the handoff: no result message (execution proposals §4).`
3. §9 step 5, reject handler step 4 (`**Cascade.**`): replace `(\`reject --task … --note "dependency <T> rejected"\`)` with `in one publish: \`reject --task T --cascade\` writes every tombstone with the note \`dependency <T> rejected\`, and skips (and prints) a task with a live claim (execution proposals §2)`.
4. §10, the `**Acceptance and master reject:**` bullet: append ` \`reject --cascade\` publishes \`claims/<id>.json\` and \`claims/<id>.*.json\` for every task it tombstones, plus \`store/claims.jsonl\` (execution proposals §2).`

- [ ] **Step 3: The execution-proposals status line**

In `docs/specs/2026-09-26-execution-proposals.md`, in the `**Status:**` line only, replace the final sentence `Not folded into the roles-and-install spec or the skills.` with `Implemented on \`feat/min-reviews\` by \`docs/plans/2026-09-27-execution-proposals.md\`; the roles-and-install spec and the skills now point here.`

- [ ] **Step 4: README and PROTOCOL**

In `README.md`:

1. In the helpers table, change the `inbox.py` row to `| \`inbox.py\` | Post a task (\`--after\`, \`--fixes\`, \`--kind\`, \`--min-reviews\`, \`--reviews\`) |`, the `claim.py` row to `| \`claim.py\` | Claim / heartbeat (\`--loop\`, \`--stop\`) / complete (\`--verdict\`) / release / reject (\`--cascade\`) |`, and add the row `| \`reviews.py\` | Read-only: the next step of a reviewed artifact's review chain |`.
2. In `## Docs`, add as the first bullet: `- [docs/specs/2026-09-26-execution-proposals.md](docs/specs/2026-09-26-execution-proposals.md): one-publish cascade, heartbeat loop, no completion message, minimum review rounds per artifact kind`.

In `skills/rip-swarm/templates/_swarm/PROTOCOL.md`, section `## Dependencies and acceptance`:

1. Replace the last bullet (`Rejecting is the only way to drop a task. The master rejects dependents of a rejected task in the same turn.`) with `Rejecting is the only way to drop a task. The master rejects the dependents of a rejected task in one publish, \`reject --cascade\`.`
2. Append:

```markdown
- A task posted with `--kind spec|plan|implementation` is an artifact. Its `min_reviews` (default: the profile's `min_reviews` for that kind, never lower) is the number of independent review rounds it needs. Review tasks (`--reviews <A>`) complete with `--verdict clean|findings`. Nobody reviews what they wrote, and nobody fixes what they reviewed; `claim` enforces both. `accept` refuses the artifact until it has its rounds and the latest result in its chain is a clean review.
```

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=skills/rip-swarm python3 -m unittest tests.test_packaging -v 2>&1 | tail -4`
Expected: `OK`.

- [ ] **Step 6: Run the full suite and commit**

Run: `PYTHONPATH=skills/rip-swarm timeout 600 python3 -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK`.

```bash
git add docs/specs/2026-09-26-roles-and-install.md docs/specs/2026-09-26-execution-proposals.md \
  README.md skills/rip-swarm/templates/_swarm/PROTOCOL.md tests/test_packaging.py
git commit -m "docs: point the roles spec, README and PROTOCOL at the execution proposals

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Spec coverage

| Spec | Task |
|---|---|
| §1 What stays (derived wake, one tombstone one wake, 30 s poll, `reviews_required_per_plan` advisory, `fixes` meaning, replacements) | unchanged code; Task 7 extends the derived wake without a seen set; Task 4 leaves `reviews_required_per_plan` unread |
| §2 `reject --cascade`: walk, skip live claims, expired first, one publish, exit codes, rerun, widened allowlist | 1 (helper), 3 (skill step 4 and the stop on a skip) |
| §3 `heartbeat --loop` from the profile; skills start and stop it; no "15 minutes" | 2, 3 |
| §4 no completion message | 3 (worker skill), 11 (roles spec §8) |
| §5.1 profile map, floor, goal phrase, resolution, which tasks are artifacts | 4 (profile, floor), 8 (master plan step, takeover) |
| §5.2 fields `kind`, `min_reviews`, `reviews`, `verdict` | 4, 5 |
| §5.3 chain, posting order, head, round, ready, independence | 4 (`chain`, `posting_order`), 5 (`chain_state`), 6 (`authors`, `reviewers`) |
| §5.4 `reviews.py` table, `CHAIN`, exit 1 cases, cannot-build rows | 5 |
| §5.5 guards: `inbox-add`, `claim`, `complete`, `accept`, cascade over the chain, derived wake, `status` | 4, 5, 6, 7 |
| §5.6 master skill arms | 8; rehearsed in 9 and 10 |
| §5.7 worker skill review tasks | 8; rehearsed in 9 and 10 |
| §5.8 what stays; N=0 is today's path | 6 (`test_zero_rounds_is_todays_path`), 9 (`test_zero_rounds_is_todays_path`) |
| §5.9 unit tests | 4, 5, 6, 7 |
| §5.9 rehearsal cases | 9, 10 (one test per case; the pass path is in `test_spec_with_two_rounds_findings_then_clean`) |
| §5.9 packaging needles | 3, 8 |
| §6 order: §2+§3, then §4, then §5 | Tasks 1–3, then 4–10 |

## Rulings made during execution

1. **Task 5b (added): `fold.has_final_tombstone` ignored `claim_id`.** A claim taken after a release was folded as corrupt, a bug pre-existing since `4e45318`. Now only a final tombstone carrying the active claim's `claim_id` counts as a crash window; a final tombstone from an earlier, already-released claim no longer poisons the fold of the claim that replaced it. Cost if wrong: small, one `fold.py` commit to revert.
2. **Master merge arm, re-check: heartbeat before re-running `reviews.py`, not after.** On a pass, the master stops the heartbeat loop and runs the one-shot heartbeat (which syncs the hive clone) before re-running `reviews.py` again, because `reviews.py` reads the local clone, and only a heartbeat fetches and fast-forwards it. Spec §5.6 says "heartbeat and run `reviews.py` again", which reads as either order; this picks the one that makes the second read see the true state. Cost if wrong: the loop stops a few seconds before the fast-forward instead of a few seconds after — a narrow, low-stakes window either way.
3. **Master merge arm, not worth pursuing (spec gap, worth fixing though not asked for): run the reject handler's step 2 in full before rejecting `A`.** §5.6 posts only `A`'s replacement, and the reject handler skips its own step 2 for an artifact that already has a chain — so on the letter of the spec, `A`'s `after` dependents would be cascade-rejected with no replacements ever posted for them. The merge arm now runs the reject handler's step 2 in full (a replacement for `A`, and for every still-wanted dependent that is not rejected yet) before rejecting `A`. This needs folding into spec §5.6; until then the implementation is stricter than the letter of the spec. Cost if wrong: none observed in the test suite; if the intent really was "only `A` gets a replacement", this over-preserves dependents that should have been dropped.
4. **`MOVED:` restarts the merge arm at item 1; every path stops the heartbeat loop before a hive write.** A plain step 9 fast-forward failure (`MOVED:`) would otherwise fall through to step 9's own advice, which never re-runs the compare against the now-moved tip, leaving `CHAIN` unaccepted forever. Restarting the arm from item 1 re-syncs and re-compares. Stopping the loop before any hive write in the arm (stated once in the preamble, applying to every item including 4, 7 and 8) keeps the loop's own background publishes from racing the arm's writes. Cost if wrong: a spurious extra reviews.py/heartbeat round-trip on `MOVED:`, or, if the stop-before-write rule were dropped, a race between the loop's heartbeat publish and the arm's own hive write.
5. **Replacement floor: a replacement refused below the profile floor is re-posted without `--min-reviews`, taking the profile's number.** §5.5's exact-copy exception for a replacement needs the original already rejected, but §5.6 and the merge arm post the replacement *before* that reject, so the exception cannot apply yet; §5.1's "larger of" fallback (post again with `--kind` alone on a floor refusal) already covers exactly this shape, so it is reused here instead of inventing a new rule. This needs folding into the spec, which does not currently say what happens when a replacement's carried-over `--min-reviews` is below the profile floor. Cost if wrong: such a replacement needs more review rounds than its original did, a correctness-neutral but slower path.
6. **Worker skill: a pointer at the top of section 5 sends review tasks to *Review tasks*.** Without it, step 1 of an ordinary task ("Start the task from `rip-swarm/integration`...") would run for a review task too and could tell a reviewer to resolve a conflict itself, which the review contract forbids (a reviewer never resolves; it reports `BUILD=conflict` and stops). The pointer routes a review task away before step 1 ever runs. Cost if wrong: none worth naming — the pointer only routes a review task to the procedure the spec already prescribes; without it a reviewer could follow step 1 and resolve a conflict inside a review.
7. **Final review I1: `inbox-add` refuses a new chain task of a settled reviewed artifact.** A `--fixes A` or `--reviews A` posted after a reviewed `A` (`min_reviews` ≥ 1) was accepted hung: `reviews.py` says `done`, the master does nothing, the task is never accepted, and `all-complete` never fires. `inbox.create_task` now refuses both when `accepted/<A>.json` exists or a `claims/<A>.reject.*.json` exists, exit 1, `<A> is accepted|rejected; post a new task instead`. An unreviewed target keeps today's rule, so a replacement of an ordinary follow-up still copies `--fixes` of a rejected original. Why at post time: the refusal is where the slip is made, and it leaves nothing on the board to clean up. Cost if wrong: a master that meant to post a late fix of an accepted artifact must post it as a new, unlinked task; no path in the skills posts one.
8. **Final review I2: a heartbeat loop ends when the session that started it dies.** An unbounded `--loop` whose session died hard kept the lease alive forever: the claim never expired, and a new master was refused. The loop records its process ancestry (the parent up to init, from `/proc`; the parent alone elsewhere) at the start, checks it on every wake-up, and sleeps at most 30 seconds at a time; once it changes, it prints why, releases its lock and exits 0. The ancestry, not `os.getppid()` alone as the ruling first said: checked on this machine, a Claude Code background command runs as `python3` under a `bash -c` wrapper under `claude`, and that wrapper outlives a harness killed hard, so the direct parent never changes (the end-to-end test `test_a_loop_ends_when_the_session_that_started_it_dies` fails with `getppid()` alone). The check is injectable (`parent=`) for tests. README ("A stuck heartbeat loop") and the reference skill give the recovery: `claim.py heartbeat --hive H --task <id> --agent A --stop` from that agent's hive clone. Cost if wrong: a loop started with a trailing `&` in a shell that then exits is reparented at once and ends within 30 seconds, so a long check could lose its lease; the skills start the loop as a harness background command, whose wrapper lives as long as the loop.
9. **Final review m1: `accept` of a reviewed artifact also waits for chain work.** Readiness (rounds met, head a clean review) held while a later chain task was open, claimed or blocked, so `accept` could take `A` while `reviews.py` would say `wait`. `accept` now refuses that case with `<A> has chain work in progress: <id>` (exit 2, the first such task in posting order), exactly when `reviews.py` would not say `merge`. Cost if wrong: an operator who accepts by hand must first settle (complete or reject) the stray chain task.

## Grok implementation review 1 (2026-09-27) — `4ef55aa`

**Reviewer:** Grok Build (`grok-4.7`, high effort), a fresh read-only run through the grok-build bridge over `221ff96..4ef55aa`. Recorded as given, without its preamble.

Needs another pass. The board rules for a review chain hold: `reviews.py`, `accept`, independence, `--cascade`, and the derived wake match sections 5.3–5.5 and the execution rulings. A review that fails to start, and stopping the heartbeat loop, can still stall the master or wedge the hive.

### What holds

Checked against `reviews.py`, `acceptance.py`, `claim.py`, `inbox.py`, `board.py`, `waiter.py`, and the master skill's chain arms, including the re-check of `HEAD=` and `SHA=` before the fast-forward. `NEXT` follows the section 5.4 table, including cannot-build versus a rebase already posted. `accept` refuses an artifact until the rounds are met and the head is a clean review, and `--via` does not skip that. A holder reject still counts as a reviewer; a master reject does not, because only the master tombstone stores `"action": "reject"`. Cascade skips a live claim, tombstones an expired one first, and publishes once. The derived wake stays quiet while a chain claim is live. With `min_reviews` at 0, none of that runs.

### Major

#### M1. A review that fails to start is released under the ordinary note, so the chain waits

`skills/swarm-worker/SKILL.md:101`, `skills/swarm-worker/SKILL.md:123`, `skills/rip-swarm/rip_swarm/reviews.py:14`

Review tasks say to run ordinary step 1, then, if `SYNC=error`, `BUILD=conflict`, or `BUILD=error`, release with `review <id> cannot build on <sha>: conflict`. Step 1's own `SYNC=error` / `BUILD=error` bullet is already terminal: release with `cannot start from integration: <git error>` and go back to waiting. Unlike the conflict bullet, it never mentions a review task. `reviews.py` only treats a note that fully matches `review <id> cannot build on <sha>: conflict` as `post-rebase`.

Scenario: the review body names `deadbee`, which is not a commit. The start block sets `BUILD=error` and leaves no merge. Following step 1 releases the review with the ordinary note and stops. The next chain wake sees an open review and prints `NEXT=wait`. No rebase is posted. Later claims fail the same way. `tests/test_rehearsal.py:126` never takes that branch: `Session.review` always writes the cannot-build note, and `test_a_review_whose_build_fails_without_a_merge_still_releases` only checks that helper. A one-line check of the same regex matches the cannot-build note and does not match `cannot start from integration: …`.

Fix: in step 1, point `SYNC=error` and `BUILD=error` at Review tasks the way the conflict bullet already does, and say that a review runs only the start block, then review step 2.

#### M2. Stopping the heartbeat loop does not wait for its publish

`skills/rip-swarm/rip_swarm/lease.py:107`, `skills/rip-swarm/rip_swarm/cli.py:541`, `skills/rip-swarm/rip_swarm/gitops.py:530`

`stop_loop` sends `SIGTERM` and returns. The loop's handler is `sys.exit(0)`. `publish` resets the hive only on `Exception`. `SystemExit` is not an `Exception`, so a signal during fetch, commit, or push skips that reset. `os.kill` does not wait for the process, and it does not signal the `git` child.

Scenario: the acceptance check outlasts half the lease, so the background loop is inside `heartbeat` on the master's hive clone. The merge arm stops the loop and immediately heartbeats or accepts. The loop's `git` is still using that clone. The next publish hits a dirty tree, an `index.lock`, or an unpushed commit and exits 1. A dirty hive blocks every later publish. `tests/test_lease.py:101` kills a sleeping process and waits in the test, not inside `stop_loop`. The rehearsal never starts the loop (`tests/test_rehearsal.py:445`).

Fix: block `SIGTERM` for the duration of `beat()`, and have `stop_loop` wait until that pid has exited before it returns.

#### M3. A review task never heartbeats

`skills/swarm-worker/SKILL.md:70`, `skills/swarm-worker/SKILL.md:108`, `skills/swarm-worker/SKILL.md:121`

Section 5 sends a review to Review tasks instead of steps 1–6. The heartbeat loop lives in step 3. Review tasks commit like step 4 and complete like step 5, and they never start the loop. Section 3 says the worker skill heartbeats around a long edit, using `worker_lease_ttl`. The template lease is 15 minutes.

Scenario: the review takes longer than the lease. The claim expires. Another worker claims it. The first worker's `complete` exits 2, the re-claim fails, and that review is abandoned. The packaging test checks `--verdict` and the conflict release in the review section, not a heartbeat.

Fix: start the loop before reading the artifact and stop it before `release` or `complete`, with the same commands as step 3.

### Minor

#### m1. Two heartbeat loops can start together

`skills/rip-swarm/rip_swarm/lease.py:89`, `skills/rip-swarm/rip_swarm/state.py:93`

The lock file is created empty, and the pid is written afterward. A second `--loop` that reads the empty file gets no pid, unlinks the file, and starts its own loop. Both then publish the same claim. The plan's rule that a second loop exits 3 holds only after the pid is on disk. `test_a_second_loop_is_refused` takes the lock only once it is fully written.

Fix: write the pid through the creating fd before any other process can observe the file, and do not unlink a file that is empty and only a moment old.

#### m2. A bad `result_ref` on the artifact itself never takes the badsha path

`skills/swarm-master/SKILL.md:90`, `skills/swarm-master/SKILL.md:211`, `skills/rip-swarm/rip_swarm/reviews.py:171`

Step 0 sends the artifact's complete wake through the chain arms and skips ordinary step 6, which is where a missing commit is rejected. `badsha` in the chain runs only inside `merge`, and at `merge` the head is a review. If the head is the artifact, `NEXT=post-review` and the body names that sha.

Scenario: the artifact's `result_ref` is `deadbee`. The master posts a review. The worker releases it with the cannot-build note. The master posts `Merge deadbee onto rip-swarm/integration` and rejects the review. The author hits `BUILD=error` on the rebase and releases it with `cannot start from integration`. The review is already rejected, the rebase is open, and `reviews.py` prints `wait`. Nothing rejects the artifact. `test_badsha_at_merge_rejects_the_head_review_not_the_artifact` only covers a bad sha on the head review.

Fix: in `post-review` and `post-rebase`, if `SHA` is not a commit, reject the artifact with the ordinary badsha note instead of posting another chain task.

## Dispositions (Grok implementation review 1)

| Finding | Disposition | Where (commit/file) |
|---|---|---|
| M1. A review that fails to start is released under the ordinary note | Accepted, fixed. Review tasks step 1 runs only step 1's command block (handling `SYNC=dirty` as step 1 says), then goes to Review tasks step 2, never to step 1's other bullets; step 1's `SYNC=error`/`BUILD=error` bullet now carries "(never in a review task: see **Review tasks**)", as the conflict bullet does. | `6d31569`; `skills/swarm-worker/SKILL.md`; needle `test_worker_review_runs_only_the_start_block` |
| M2. Stopping the heartbeat loop does not wait for its publish | Accepted, fixed. The loop's SIGTERM handler (`LoopStop`) only records a stop that arrives during a beat, and the loop ends right after that beat; outside a beat it ends at once. `stop_loop` polls until the pid has exited (a zombie counts), up to 60 s, and exits 1 (`LoopStopTimeout`) if it has not. The reference skill says so. | `a85b32b`; `skills/rip-swarm/rip_swarm/lease.py`, `cli.py`, `skills/rip-swarm/SKILL.md`; `tests/test_lease.py` (`test_a_stop_during_a_beat_lets_the_beat_finish`, `test_stop_waits_for_a_loop_mid_beat_to_finish_and_exit`, `test_stop_reports_a_loop_that_does_not_exit`) |
| M3. A review task never heartbeats | Accepted, fixed. Review tasks step 3 starts the heartbeat loop before the artifact is read, and stops it before `release` or `complete`, on every path; step 6 stops it before the commit and `complete`. | `6d31569`; `skills/swarm-worker/SKILL.md`; needle `test_worker_review_heartbeats_until_it_releases_or_completes` |
| m1. Two heartbeat loops can start together | Accepted, fixed. `state.create_pid_file` writes the pid to a temporary file in the same directory and hard-links it into place, so a lock file is never seen without its pid. The wait lock had the same window and uses the same helper. | `a85b32b`; `skills/rip-swarm/rip_swarm/state.py`, `lease.py`; `test_a_loop_lock_is_never_seen_without_its_pid` |
| m2. A bad `result_ref` on the artifact itself never takes the badsha path | Accepted, fixed. *Review chains* checks `rev-parse -q --verify "<SHA>^{commit}"` before posting: `post-review` rejects the head (`A` or a fix of `A`) with the ordinary badsha note and messages its author; `post-rebase` whose note names a non-commit rejects the released review, posts no rebase, and runs the helper again, whose `post-review` check then rejects the head. Arm 2's `git show` of a review head that is not a commit is `merge` item 8's case: that review is rejected. Mirrored in the rehearsal's `Master.chain_wake`. | `56ce9bb`; `skills/swarm-master/SKILL.md`; `tests/test_rehearsal.py` (`test_an_artifact_whose_result_ref_is_not_a_commit_is_rejected_unreviewed`, `test_a_review_released_on_a_sha_that_is_not_a_commit_rejects_the_artifact`, `test_a_review_head_that_is_not_a_commit_gets_no_next_round`); needle `test_master_checks_the_sha_before_posting_a_chain_task` |

## Grok implementation review 2 (2026-09-27) — `f7089ff`

**Reviewer:** Grok Build (`grok-4.7`, high effort), a fresh read-only run through the grok-build bridge over `221ff96..f7089ff`, with the brief of review 1 plus a check of its fixes. Recorded as given, without its preamble.

Needs another pass. The board rules for a review chain hold, and the five fixes from the first review still hold, but a second cannot-build release posts another rebase, and a dirty chain merge follows the ordinary accept path and leaves the chain unaccepted.

### What holds

Checked against `reviews.py`, `acceptance.py`, `claim.py`, `inbox.py`, `board.py`, `waiter.py`, `lease.py`, and both role skills. `NEXT` follows the §5.4 table, including a rebase already posted after one cannot-build release. `accept` refuses an artifact until the rounds are met and the head is a clean review, and `--via` does not skip that. A holder reject still counts as a reviewer (the tombstone has no `"action"` field); a master reject does not. Cascade skips a live claim, tombstones an expired one first, and publishes once. The derived wake stays quiet while a chain claim is live. With `min_reviews` at 0, none of that runs.

The first-pass fixes hold. A review runs only the start block and then releases with `review <id> cannot build on <sha>: conflict` (`skills/swarm-worker/SKILL.md` lines 101 and 122–127). `stop_loop` lets the beat finish and waits until that pid has exited (`lease.py` lines 88–106 and 153–170). The review procedure starts the heartbeat loop before the read and stops it before `release` or `complete`. The chain checks `rev-parse "<SHA>^{commit}"` before posting, and a non-commit on the artifact rejects that head. The execution rulings I checked are sound: the callable allowlist, the heartbeat-before-re-check order, restarting the merge arm on `MOVED:`, copying a replacement only after the floor check, and running the replacement step before rejecting `A`. Ten focused tests covering those fixes, the one-release rebase, accept, and the derived wake passed.

### Critical

None.

### Major

#### M1. A later cannot-build release hides the rebase already posted

`skills/rip-swarm/rip_swarm/reviews.py:157-167`

The cannot-build row uses the latest release only. `since` is that tombstone's stamp, and a fix counts only when its `created_at` is at or after that stamp. A review that was released as unable to build stays open until the master rejects it, so another worker can claim it and release it again with the same note. That newer stamp is after the rebase, so the rebase no longer counts.

Scenario: Alice completes spec `A`. Bob releases review `R` with `review R cannot build on abc1234: conflict`. The master posts rebase `F` (`NEXT=reject-review`). Before the reject lands, Carol claims `R` and releases it with the same note. `reviews.py` prints `NEXT=post-rebase` again. The master posts a second rebase of `A`. I ran that board: after the first release the helper printed `post-rebase`, after `F` it printed `reject-review`, and after Carol's release it printed `post-rebase` again. `test_a_review_that_cannot_build_asks_for_a_rebase_once` never releases a second time. The rehearsal's `Session.review` releases once and stops.

Fix: if the latest release note matches, decide `post-rebase` versus `reject-review` from the earliest matching cannot-build release, not the latest. A fix posted after that first release stays "already posted" through every later release of the same review. Add that second release to the `reviews.py` table.

#### M2. A dirty chain merge follows ordinary step 9 and accepts only A

`skills/swarm-master/SKILL.md:150`, `skills/swarm-master/SKILL.md:206`, `skills/swarm-master/SKILL.md:210`

Merge item 7 says to do ordinary steps 7 and 8 with `T=<A>`. Step 7 says: if the dirt is leftover from your own check, remove it and run step 4 again. Item 3 already says what that sentence does on a chain: ordinary step 9 accepts `A` alone, and `CHAIN` stays unaccepted. Item 7 still points at it. Step 9's via-walk follows `fixes`, and the reviews do not fix each other, so they are never accepted.

Scenario: the chain is ready to merge and the integration worktree still has a file from the previous acceptance check. The outcome block prints `OUTCOME=dirty` and creates no review branch. The master cleans the file, runs ordinary step 4, and on `OUTCOME=merged` runs step 9 with `T=<A>`. `A` is accepted. The review and the revise stay in `awaiting_acceptance`. `all-complete` never fires, and a takeover does not see those completes again. `Master.merge_arm` returns `"dirty"` at `tests/test_rehearsal.py:477` and does not follow step 7, so the rehearsal never takes this path.

Fix: on `OUTCOME=dirty`, clean up and restart the merge arm at item 1, the same way item 3 already restarts on `MOVED:`. Point the rehearsal's dirty outcome at that restart.

### Minor

#### m1. The chain merge cites the outcome block and not the loop that has to cover the check

`skills/swarm-master/SKILL.md:94`, `skills/swarm-master/SKILL.md:204-205`

The loop is started in the sentence above step 4's script. Merge item 1 says to run "step 4 (the `OUTCOME=` block)", then run the acceptance check, and only item 2 heartbeats, after that check. Copying the fenced script leaves the check with no loop. Section 3 is this window: a check longer than `orchestrator_lease_ttl` (the template is 30 minutes) drops the baton, and `join` seats a second master on the same integration worktree. `merge_arm` documents that it never starts the loop (`tests/test_rehearsal.py:464`).

Fix: in merge item 1, start the loop before the outcome block and keep the existing stop before the next hive write.

#### m2. A heartbeat stop that times out still lets the next hive write run

`skills/rip-swarm/rip_swarm/lease.py:165-166`, `skills/swarm-worker/SKILL.md:130-133`

`stop_loop` raises `LoopStopTimeout` after 60 seconds, and the CLI turns that into exit 1. The worker review steps say to stop the loop and then commit and `complete`, and they never mention that exit. The reference skill does (`skills/rip-swarm/SKILL.md:85`). The master's "no hive write until you have stopped it" (`skills/swarm-master/SKILL.md:41`) is not in the review steps.

Scenario: the review's loop is inside a heartbeat whose `git push` hangs. `--stop` returns 1 at 60 seconds and the loop is still in that publish. The worker runs `complete` on the same hive clone. The publish hits `index.lock` or a dirty tree and exits 1, and a publish that is left dirty blocks later hive writes. `test_stop_reports_a_loop_that_does_not_exit` checks the timeout itself, not the skill's next command.

Fix: in both role skills, say that exit 1 from `--stop` means the loop may still be publishing, and the next hive write waits until a later `--stop` exits 0.

### Nit

#### n1. Stopping a loop treats any `--loop` process as its own

`skills/rip-swarm/rip_swarm/lease.py:113-120`

`_is_loop` returns true when `/proc/<pid>/cmdline` contains `--loop`. It does not look for the task id, which the command line also carries. A stale pid file whose pid was reused by another task's loop is signalled by `--stop`, and that other loop exits. The window is a hard-killed loop and a recycled pid before `--stop`. `test_stop_never_kills_a_process_that_is_not_a_loop` covers a process that is not a loop.

Fix: require the task id in the command line as well as `--loop`.

## Dispositions (Grok implementation review 2)

| Finding | Disposition | Where |
|---|---|---|
| M1. A later cannot-build release hides the rebase already posted | Accepted, fixed. When the latest release of an open review is a cannot-build note, `post-rebase` versus `reject-review` is decided from the earliest cannot-build release of that review: a fix posted at or after it is the rebase already posted, through every later release of the same review. Spec §5.4's row still says "since that release" (the latest); it needs the same wording. | `140ce3b`; `skills/rip-swarm/rip_swarm/reviews.py`; `tests/test_reviews.py` (`test_a_second_cannot_build_release_keeps_the_rebase_posted`); `tests/test_rehearsal.py` (`test_a_second_release_of_the_same_review_posts_no_second_rebase`) |
| M2. A dirty chain merge follows ordinary step 9 and accepts only A | Accepted, fixed. Merge item 7 now handles `OUTCOME=dirty` itself: on leftovers of the master's own check, stop the heartbeat loop, remove them and restart the merge arm at item 1, as item 3 does on `MOVED:`, and never follow step 7's "run step 4 again"; any other dirt is reported and the master stops, as step 7 says. `OUTCOME=error` stays as step 8. The rehearsal's `Master.merge_arm` follows the restart. | `5f94921`; `skills/swarm-master/SKILL.md`; `tests/test_rehearsal.py` (`test_a_dirty_merge_restarts_the_arm_and_accepts_the_whole_chain`); needle `test_master_merge_arm_restarts_on_its_own_dirt` |
| m1. The chain merge cites the outcome block and not the loop that has to cover the check | Accepted, fixed. Merge item 1 now says: heartbeat (the full command), then start the heartbeat loop in the background (section 2), then run step 4's `OUTCOME=` block; the loop covers the block and the acceptance check. | `5f94921`; `skills/swarm-master/SKILL.md`; needle `test_master_merge_arm_starts_the_loop_before_the_block` |
| m2. A heartbeat stop that times out still lets the next hive write run | Accepted, fixed. Where each role skill stops the loop (master section 2; worker section 5 step 3 and *Review tasks* step 3): exit 1 from `--stop` means the loop has not exited within 60 seconds and may still be publishing; run `--stop` again, and make no hive write until it exits 0. | `5f94921`; `skills/swarm-master/SKILL.md`, `skills/swarm-worker/SKILL.md`; needle `test_role_skills_wait_for_a_stop_that_timed_out` |
| n1. Stopping a loop treats any `--loop` process as its own | Accepted, fixed. `_is_loop` requires `--task <id>` (or `--task=<id>`) for the task at hand as well as `--loop`, both for `--stop` and for the lock's live-loop check. | `7f7efb7`; `skills/rip-swarm/rip_swarm/lease.py`; `tests/test_lease.py` (`test_stop_never_kills_the_loop_of_another_task`, `test_a_task_given_with_an_equals_sign_is_its_loop`) |

## Grok implementation review 3 (2026-09-27) — `5a0ad86`

**Reviewer:** Grok Build (`grok-4.7`, high effort), a fresh read-only run through the grok-build bridge over `221ff96..5a0ad86`, with the brief of review 1 plus a check of the fixes of reviews 1 and 2. Recorded as given, without its preamble.

Needs another pass. The helpers for sections 2–5 hold, and the fixes from the first two reviews still hold, but a chain merge that hits `OUTCOME=error` or foreign dirt leaves the heartbeat loop running, so the baton never comes free.

### What holds

Checked against `reviews.py`, `acceptance.py`, `claim.py`, `inbox.py`, `board.py`, `waiter.py`, `lease.py`, `fold.py`, and both role skills. `NEXT` follows the §5.4 table, including a second cannot-build release of the same review. `accept` refuses an artifact until the rounds are met and the head is a clean review, and `--via` does not skip that. A holder reject still counts as a reviewer; a master reject does not. Cascade skips a live claim, tombstones an expired one first, and publishes once. The derived wake stays quiet while a chain claim is live. With `min_reviews` at 0, none of that runs.

The earlier fixes hold. A review runs only the start block and then releases with `review <id> cannot build on <sha>: conflict`. `stop_loop` lets the beat finish, waits until that pid has exited, and signals only the loop whose command line has both `--loop` and this task id. The review procedure heartbeats from the read until `release` or `complete`. The chain checks `rev-parse "<SHA>^{commit}"` before posting. A later cannot-build release still counts the rebase posted after the first one. A dirty chain merge restarts the arm, and the arm starts the loop before the outcome block. Exit 1 from `--stop` blocks the next hive write. The execution rulings I checked are sound: the callable allowlist, the heartbeat-before-re-check order, restarting on `MOVED:`, copying a replacement only after the floor check, and running the replacement step before rejecting `A`.

Verified by reading those paths and by `tests.test_lease`, `tests.test_reviews`, `tests.test_cascade`, `tests.test_packaging`, `tests.test_fold`, `tests.test_inbox`, `tests.test_profile`, `tests.test_board` (168 tests) and `tests.test_rehearsal`, `tests.test_wait` (74 tests), all passing.

### Critical

None.

### Major

#### M1. A chain merge that errors, or stops on dirt it did not make, leaves the heartbeat loop running

`skills/swarm-master/SKILL.md:204`, `skills/swarm-master/SKILL.md:210`, `skills/swarm-master/SKILL.md:145`, `docs/specs/2026-09-26-execution-proposals.md:64`

Merge item 1 starts the loop, then says the items below are what stop it. The preamble stops the loop only before a hive write. Item 7 stops it on leftovers of the master's own check. Foreign dirt is "report and stop, as step 7 does." `OUTCOME=error` is "as step 8," which reports and stops and never runs `--stop`. Ordinary step 4 does stop the loop right after any `OUTCOME=` other than `merged`. Spec §3 requires that kill on the error path, once the worktree is back on `rip-swarm/integration`. The loop is a background process started so it outlives the shell; `--stop` is how it ends.

Scenario: the chain is ready, so the master starts `heartbeat --loop` for the orchestrator and runs the outcome block. `git switch` fails (`RESUMED=failed` or the final else). The block prints `OUTCOME=error` and is back on `rip-swarm/integration`. The master reports that and stops, or returns to section 5. The loop keeps heartbeating for half of `orchestrator_lease_ttl` (the template is 30 minutes). A new `join --role master` exits 2 until that process dies. If this master waits instead, `wait` and the loop both publish on the same hive clone. The same loop is left running when item 7 reports foreign dirt. `Master.merge_arm` never starts a loop (`tests/test_rehearsal.py:463-467`) and returns `"error"` or `"dirty"` with no stop (`tests/test_rehearsal.py:483-484`). `test_master_merge_arm_stops_the_loop_on_every_path` only checks that the preamble sentence exists.

Fix: on foreign dirt and on `OUTCOME=error`, run `--stop` and do not start `wait` until it exits 0, at the same moment ordinary step 4 stops after a non-merged `OUTCOME=`. Have the rehearsal record that stop, and have the packaging needle require `--stop` on item 7's error sentence.

### Minor

#### m1. A holder reject of a chain task, once A is already rejected, runs the ordinary replacement step

`skills/swarm-master/SKILL.md:230-235`, `tests/test_rehearsal.py:283-286`

The chain-task bullet skips step 2 only while `A` is not rejected. After `A` is rejected, the wake falls through to "run the steps as they are." Step 1 short-circuits only when the note is `dependency <id> rejected`. A holder reject carries the holder's own note. Step 2 copies body, `--fixes`, `--kind`, and `--min-reviews`, and does not copy `--reviews`. The handler also says not to skip a step from memory.

Scenario: not-worth-pursuing rejects `A` while Bob holds review `R`. Cascade prints `skipped R (chain of A)` and the master waits; the derived wake stays quiet. Bob rejects `R`. The next wake sees `A` rejected, so the chain-task bullet does not apply, and step 1 does not match. Step 2 can post `Review … (replaces R)` with no `--reviews`. That task is outside `A`'s chain, so a later cascade of `A` does not reject it. It stays open as ordinary work, and `all-complete` waits on it. `test_rejecting_an_artifact_while_a_review_is_claimed` completes the held review; it does not reject it. The rehearsal's `handle_reject` uses the same `A is not rejected` guard.

Fix: once `A` is rejected, a chain task's reject runs step 4 only, as a cascade note already does. Do not post a replacement.

### Nit

None.

## Dispositions (Grok implementation review 3)

| Finding | Disposition | Where |
|---|---|---|
| M1. A chain merge that errors, or stops on dirt it did not make, leaves the heartbeat loop running | Accepted, fixed. The merge preamble now says to stop the heartbeat loop on every path of the arm, before its next hive write and before reporting and stopping or going back to section 5, and gives the `--stop` command, run again until it exits 0 (section 2); on an `OUTCOME=` other than `merged`, right after that line, as ordinary step 4 does. Items 4 and 8 name the stop; item 7 runs `--stop` until it exits 0 before reporting foreign dirt and before `OUTCOME=error`'s step 8. The rehearsal's `Master` records the loop's start and stop in `review` and `merge_arm`; the merge-arm cases assert it stopped. Audit of the other exits: ordinary step 9's `MOVED:` ran step 4 again with the loop still running, and step 4 opens with the one-shot heartbeat that section 2 forbids while the loop runs; step 4's stop sentence and *Passes* item 1 now stop the loop on `MOVED:` first. No other exit of the merge arm or of steps 4–9 ends with the loop running. | `48505bd`; `skills/swarm-master/SKILL.md`; `tests/test_rehearsal.py` (`test_an_error_at_merge_stops_the_loop`, `test_a_conflict_at_merge_stops_the_loop_and_posts_the_rebase`, `test_a_moved_integration_stops_the_loop_before_step_4_again`, loop assertions in `test_a_dirty_merge_restarts_the_arm_and_accepts_the_whole_chain` and the other merge-arm cases); needles `test_master_merge_arm_stops_the_loop_on_every_path` (tightened), `test_master_stops_the_loop_on_moved_before_step_4_again`, `test_master_merge_arm_restarts_on_its_own_dirt` (updated) |
| m1. A holder reject of a chain task, once A is already rejected, runs the ordinary replacement step | Accepted, fixed. A new reject-handler bullet: a chain task whose `A` is rejected (a `claims/<A>.reject.*.json` exists) runs step 4 only, whatever the note says, and not *Review chains*; `A`'s own reject decided about replacements, and step 3 has nothing to do. `Master.handle_reject` treats it as a cascade note. | `48505bd`; `skills/swarm-master/SKILL.md`; `tests/test_rehearsal.py` (`test_a_holders_reject_after_the_artifact_is_rejected_posts_nothing`); needle in `test_master_runs_reviews_py_on_every_chain_wake` |
