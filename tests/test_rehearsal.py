# tests/test_rehearsal.py — model-free rehearsal: one master, two workers (spec §11)
import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, git, make_project, remote_show
from rip_swarm.board import fixers, read_board
from rip_swarm.cli import main
from rip_swarm.gitops import sync
from rip_swarm.join import join, leave
from rip_swarm.state import load_state, save_state
from rip_swarm.waiter import Wake, tick

LATER = T0 + timedelta(minutes=31)   # past the 30m baton and worker leases


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def cli(*argv, at=T0, err=None):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO() if err is None else err), \
            mock.patch("rip_swarm.cli.now_utc", return_value=at):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue().strip()


class Session:
    """What a role skill does between wakes, minus the model."""

    def __init__(self, result, now=T0):
        self.agent, self.hive, self.wt, self.now = result.agent, result.hive, result.worktree, now

    def tick(self):
        sync(self.hive)
        state = load_state(self.hive, self.agent)
        wake = tick(self.hive, self.agent, state, self.now, idle_after=600)
        save_state(self.hive, state)
        return wake

    def post(self, title, *extra):
        rc, out = cli("inbox-add", "--hive", self.hive, "--created-by", self.agent,
                      "--title", title, *extra, at=self.now)
        assert rc == 0, out
        return out.split()[1].rstrip(":")

    def claim(self, task):
        return cli("claim", "--hive", self.hive, "--task", task, "--agent", self.agent, at=self.now)[0]

    def start_task(self, build_on=""):
        """/swarm-worker §5 step 1, line for line: (SYNC, BUILD, KEPT)."""
        wt, kept, build = self.wt, "none", "none"
        if git(wt, "status", "--porcelain"):
            sync_ = "dirty"                                                   # nothing touched
        elif run(wt, "show-ref", "--verify", "--quiet", "refs/heads/rip-swarm/integration").returncode:
            sync_ = "none"
        elif git(wt, "rev-list", "--count", "rip-swarm/integration..HEAD") == "0":
            sync_ = "merged" if run(wt, "merge", "--no-edit", "rip-swarm/integration").returncode == 0 else "error"
        else:
            kept = f"refs/rip-swarm/prev/{self.agent}/{git(wt, 'rev-parse', '--short', 'HEAD')}"
            ok = (run(wt, "update-ref", kept, "HEAD").returncode == 0
                  and run(wt, "reset", "-q", "--hard", "rip-swarm/integration").returncode == 0)
            sync_ = "reset" if ok else "error"
        if build_on and sync_ not in ("dirty", "error"):
            if run(wt, "merge", "--no-edit", build_on).returncode == 0:
                build = "merged"
            elif run(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
                build = "conflict"
            else:
                build = "error"
        return sync_, build, kept

    def work(self, name, text, msg):
        """Step 2's edit, then step 4's `add -A && commit -m`, line for line."""
        (self.wt / name).write_text(text, encoding="utf-8")                # may be untracked
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", msg)
        return git(self.wt, "rev-parse", "HEAD")

    def complete(self, task):
        sha = git(self.wt, "rev-parse", "--short", "HEAD")      # what /swarm-worker records
        rc, out = cli("complete", "--hive", self.hive, "--task", task, "--agent", self.agent,
                      "--result-ref", f"rip-swarm/{self.agent}@{sha}", at=self.now)
        assert rc == 0, out
        return sha


class Master(Session):
    def result_sha(self, task):
        sync(self.hive)
        stone = next((self.hive / "claims").glob(f"{task}.complete.*.json"))
        return json.loads(stone.read_text(encoding="utf-8"))["result_ref"].split("@", 1)[1]

    def handle_complete(self, task, check):
        """`wake task-finished <task> complete` per /swarm-master §6."""
        sync(self.hive)
        if (self.hive / "accepted" / f"{task}.json").exists():
            return "noop"
        board = read_board(self.hive, self.now)
        if any(not view.settled for view in fixers(board, task)):
            return "skip"
        return self.review(task, self.result_sha(task), check)

    def review(self, task, sha, check):
        """Step 4's block, then step 9's check and Pass/Falls-short commands, per /swarm-master §6."""
        outcome = self.review_block(task, sha)
        if outcome != "merged":
            return outcome
        wt, review = self.wt, f"rip-swarm/review-{task}"
        if not check(wt):
            if not self._ok("switch", "rip-swarm/integration") or not self._ok("branch", "-D", review):
                return "failed"
            return "short"
        if git(wt, "status", "--porcelain"):
            return "dirty"
        if git(wt, "rev-parse", "rip-swarm/integration") != self.last_tip:
            return "moved"
        if not (self._ok("switch", "rip-swarm/integration")
                and self._ok("merge", "--ff-only", review)
                and self._ok("branch", "-d", review)):
            return "failed"
        new_tip = git(wt, "rev-parse", "HEAD")
        rc, out = cli("accept", "--hive", self.hive, "--agent", self.agent, "--task", task,
                      "--integration-sha", new_tip, at=self.now)
        assert rc == 0, out
        child = task
        while True:
            parent = json.loads((self.hive / "inbox" / f"{child}.json").read_text()).get("fixes")
            if not parent:
                break
            err = io.StringIO()
            rc, out = cli("accept", "--hive", self.hive, "--agent", self.agent, "--task", parent,
                          "--integration-sha", new_tip, "--via", child, at=self.now, err=err)
            if rc == 2 and f"{parent} is rejected; it cannot be accepted" in err.getvalue():
                break                                                     # dropped: the walk stops
            assert rc == 0, out
            if out.startswith("already accepted"):
                break
            child = parent
        return "pass"

    def _ok(self, *args):
        return run(self.wt, *args).returncode == 0

    def review_block(self, task, short):
        """The step 4 block, line for line: merged | conflict | badsha | dirty | error."""
        wt, review = self.wt, f"rip-swarm/review-{task}"
        tip = self.last_tip = git(wt, "rev-parse", "rip-swarm/integration")
        resumed = False
        if self._ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            git(wt, "merge", "--abort")                                   # a crash mid-merge
        resolved = run(wt, "rev-parse", "-q", "--verify", f"{short}^{{commit}}")
        if resolved.returncode != 0:
            return "badsha"                                               # nothing touched
        full = resolved.stdout.strip()
        if git(wt, "status", "--porcelain"):
            return "dirty"                                                # nothing touched
        if self._ok("show-ref", "--verify", "--quiet", f"refs/heads/{review}"):
            # Empty when the branch is not a merge (e.g. right after the abort): the recreate path.
            p1 = run(wt, "rev-parse", "-q", "--verify", f"{review}^1").stdout.strip()
            p2 = run(wt, "rev-parse", "-q", "--verify", f"{review}^2").stdout.strip()
            if p1 == tip and p2 == full:
                if git(wt, "branch", "--show-current") == review or self._ok("switch", review):
                    resumed = True
                else:
                    resumed = "failed"                                    # the resume switch failed
            else:
                run(wt, "switch", "rip-swarm/integration")
                run(wt, "branch", "-D", review)
        if resumed is True:
            return "merged"
        if resumed == "failed":
            run(wt, "switch", "rip-swarm/integration")
            return "error"
        if self._ok("switch", "-c", review, tip) and self._ok("merge", "--no-ff", "--no-edit", full):
            return "merged"
        if self._ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            run(wt, "merge", "--abort")
            run(wt, "switch", "rip-swarm/integration")
            run(wt, "branch", "-D", review)
            return "conflict"
        run(wt, "switch", "rip-swarm/integration")
        return "error"

    def reject(self, task, note):
        rc, out = cli("reject", "--hive", self.hive, "--agent", self.agent, "--task", task,
                      "--note", note, at=self.now)
        assert rc == 0, out

    def handle_reject(self, task, wanted=()):
        """`wake task-finished <task> reject` per /swarm-master §6, step by step.

        `wanted` is the model's call: the rejected tasks whose work is still
        wanted. The orphan is always rejected here; the other choice is `review`.
        Every step reads the board first, so a rerun does nothing twice.
        Returns what it did."""
        sync(self.hive)
        board = read_board(self.hive, self.now)
        done = []
        stone = next((self.hive / "claims").glob(f"{task}.reject.*.json"))
        note = json.loads(stone.read_text(encoding="utf-8")).get("note") or ""
        root = not re.fullmatch(r"dependency \S+ rejected", note)                 # step 1
        chain = self._downstream(board, task)
        if root:                                                                   # step 2
            replaced = {}
            for old in [task, *(tid for tid in chain if not board[tid].rejected)]:
                if old not in wanted:
                    continue
                found = self.replacements(old)
                if found:
                    replaced[old] = found[0]                                       # the first
                    continue
                inbox = json.loads((self.hive / "inbox" / f"{old}.json").read_text(encoding="utf-8"))
                after = [replaced.get(dep, dep) for dep in board[old].after
                         if dep in replaced or dep not in (task, *chain)]
                extra = [arg for dep in after for arg in ("--after", dep)]
                if inbox.get("body") is not None:
                    extra += ["--body", inbox["body"]]
                if inbox.get("fixes"):
                    extra += ["--fixes", inbox["fixes"]]
                replaced[old] = self.post(f"{board[old].title} (replaces {old})", *extra)
                done.append(f"post {replaced[old]}")
            board = read_board(self.hive, self.now)
        x = board[task].fixes
        if root and x and not board[x].settled and all(                           # step 3
                view.settled for view in fixers(board, x) if view.task_id != task):
            self.reject(x, "fix abandoned")
            done.append(f"orphan {x}")
        for tid in chain:                                                          # step 4
            if not board[tid].rejected:
                self.reject(tid, f"dependency {task} rejected")
                done.append(f"reject {tid}")
        return done

    @staticmethod
    def _downstream(board, task):
        """Every task whose `after` chain leads to `task`, in dependency order."""
        out, frontier = [], [task]
        while frontier:
            nxt = sorted(tid for tid, v in board.items()
                         if tid not in out and any(dep in frontier for dep in v.after))
            out.extend(nxt)
            frontier = nxt
        return out

    def replacements(self, task):
        """Inbox tasks titled `… (replaces <task>)`, per the handler's grep."""
        sync(self.hive)
        return sorted(p.stem for p in (self.hive / "inbox").glob("task_*.json")
                      if json.loads(p.read_text(encoding="utf-8"))["title"]
                      .endswith(f" (replaces {task})"))


def says(text):
    return lambda wt: (wt / "t.txt").read_text(encoding="utf-8") == text


class TestRehearsal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin, self.repo = make_project(Path(self.tmp.name))
        self.m = Master(join(self.repo, role="master", harness="grok", now=T0))
        self.w1 = Session(join(self.repo, role="worker", harness="claude-code", now=T0))
        self.w2 = Session(join(self.repo, role="worker", harness="claude-code", now=T0))

    def tearDown(self):
        self.tmp.cleanup()

    def _done(self, worker, task, text):
        self.assertEqual(worker.claim(task), 0)
        self.assertIn(worker.start_task()[0], ("merged", "reset"))
        worker.work("t.txt", text, f"work {task}")
        return worker.complete(task)

    def test_plan_race_accept_unblocks_the_dependent(self):
        a = self.m.post("A")
        b = self.m.post("B", "--after", a)
        c = self.m.post("C")
        first, second = sorted([a, c])
        self.assertEqual(self.w1.tick(), Wake("task-available", first))    # one task per wake
        self.assertEqual(self.w1.tick(), Wake("task-available", second))
        self.w2.tick()
        self.assertEqual(self.w1.claim(a), 0)
        self.assertEqual(self.w2.claim(a), 2)                       # lost: someone else holds it
        self.assertEqual(self.w2.claim(b), 2)                       # blocked
        self.assertEqual(self.w2.claim(c), 0)
        self.assertEqual(self.w1.start_task(), ("merged", "none", "none"))
        self.w1.work("t.txt", "A\n", "A")
        self.w1.complete(a)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        self.assertEqual(self.m.handle_complete(a, says("A\n")), "pass")
        self.assertEqual(git(self.m.wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{a}").returncode, 0)
        self.assertEqual(self.w1.tick(), Wake("task-available", b))

    def test_shortfall_is_reviewed_off_integration_then_fixed(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        c = self.m.post("C")
        self.w1.tick()
        bad = self._done(self.w1, t, "wrong\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        wt, review = self.m.wt, f"rip-swarm/review-{t}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", bad)
        # Mid-review, w2 claims other work and merges integration.
        self.w2.tick()
        self.assertEqual(self.w2.claim(c), 0)
        self.assertEqual(self.w2.start_task()[0], "merged")
        self.assertNotEqual(run(self.w2.wt, "merge-base", "--is-ancestor", bad, "HEAD").returncode, 0)
        # Falls short: drop the review branch; the sha stays only on the worker branch.
        git(wt, "switch", "rip-swarm/integration")
        git(wt, "branch", "-D", review)
        holders = git(self.repo, "branch", "--contains", bad, "--format=%(refname:short)").split()
        self.assertEqual(holders, [f"rip-swarm/{self.w1.agent}"])
        f = self.m.post("Fix T", "--fixes", t, "--body", f"build on {bad}; t.txt must say right")
        self.assertEqual(self.w1.tick(), Wake("task-available", f))
        self.assertEqual(self.w1.claim(f), 0)
        self.assertEqual(self.w1.start_task(bad)[:2], ("reset", "merged"))   # the fixes sha, always
        self.w1.work("t.txt", "right\n", "fix T")
        self.w1.complete(f)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} complete"))
        self.assertEqual(self.m.handle_complete(f, says("right\n")), "pass")
        record = json.loads((self.m.hive / "accepted" / f"{t}.json").read_text())
        self.assertEqual(record["via"], [f])
        self.assertEqual(self.w1.tick(), Wake("task-available", d))

    def test_review_conflict_rebase_task_is_the_work(self):
        x = self.m.post("X")
        self.m.now += timedelta(milliseconds=1)                            # x's id sorts before y's
        y = self.m.post("Y")
        self.w1.tick()
        self.w2.tick()
        self._done(self.w1, x, "one\n")
        sha_y = self._done(self.w2, y, "two\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{x} complete"))
        self.assertEqual(self.m.handle_complete(x, says("one\n")), "pass")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{y} complete"))
        self.assertEqual(self.m.handle_complete(y, says("one\ntwo\n")), "conflict")
        r = self.m.post("Rebase Y", "--fixes", y, "--body", f"merge {sha_y} onto integration")
        self.assertEqual(self.w2.tick(), Wake("task-available", r))
        self.assertEqual(self.w2.claim(r), 0)
        self.assertEqual(self.w2.start_task(sha_y)[:2], ("reset", "conflict"))
        (self.w2.wt / "t.txt").write_text("one\ntwo\n", encoding="utf-8")   # the resolution is the work
        git(self.w2.wt, "add", "-A")                                       # step 1's conflict commit
        git(self.w2.wt, "commit", "-q", "--no-edit")
        self.assertEqual(git(self.w2.wt, "status", "--porcelain"), "")      # nothing left to commit
        self.w2.complete(r)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r} complete"))
        self.assertEqual(self.m.handle_complete(r, says("one\ntwo\n")), "pass")
        self.assertTrue((self.m.hive / "accepted" / f"{y}.json").is_file())

    def test_own_release_is_not_reoffered_to_the_releaser(self):
        t = self.m.post("T")
        self.w1.tick()
        self.w2.tick()
        self.assertEqual(self.w1.claim(t), 0)
        rc, _ = cli("release", "--hive", self.w1.hive, "--task", t, "--agent", self.w1.agent,
                    "--note", "cannot merge integration: t.txt")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.w1.tick())
        self.assertEqual(self.w2.tick(), Wake("task-available", t))

    def test_a_crashed_workers_claim_is_stolen(self):
        t = self.m.post("T")
        self.assertEqual(self.w1.claim(t), 0)
        self.w2.now = LATER
        self.assertEqual(self.w2.tick(), Wake("task-available", t))
        self.assertEqual(self.w2.claim(t), 0)
        self.assertIn(f'"agent": "{self.w2.agent}"', remote_show(self.origin, f"claims/{t}.json"))

    def test_a_late_worker_picks_up_open_work(self):
        t = self.m.post("T")
        late = Session(join(self.repo, role="worker", harness="grok", now=T0))
        self.assertEqual(late.tick(), Wake("task-available", t))

    def _handoff(self, *, with_fix):
        t = self.m.post("T")
        self.w1.tick()
        bad = self._done(self.w1, t, "wrong\n")
        f = None
        if with_fix:
            self.m.tick()
            self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
            f = self.m.post("Fix T", "--fixes", t, "--body", f"build on {bad}")
        # Master A stops heartbeating; master B joins after the baton expired.
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        return b, t, f, bad

    def test_handoff_reviews_a_bare_complete_once(self):
        b, t, _f, _bad = self._handoff(with_fix=False)
        self.assertEqual(b.tick(), Wake("task-finished", f"{t} complete"))
        self.assertIsNone(b.tick())

    def test_handoff_skips_while_a_fix_is_live(self):
        b, t, f, _bad = self._handoff(with_fix=True)
        self.assertEqual(b.tick(), Wake("task-finished", f"{t} complete"))
        self.assertEqual(b.handle_complete(t, says("right\n")), "skip")
        self.assertNotEqual(b.tick(), Wake("task-finished", f"{t} complete"))

    def _handoff_with_finished_fix(self):
        b, t, f, bad = self._handoff(with_fix=True)
        self.w1.now = LATER
        self.assertEqual(self.w1.claim(f), 0)
        self.assertEqual(self.w1.start_task(bad)[:2], ("reset", "merged"))
        self.w1.work("t.txt", "right\n", "fix T")
        self.w1.complete(f)
        return b, t, f

    def test_handoff_fix_first(self):
        b, t, f = self._handoff_with_finished_fix()
        self.assertEqual(b.handle_complete(f, says("right\n")), "pass")
        self.assertEqual(b.handle_complete(t, says("right\n")), "noop")
        rc, out = cli("accept", "--hive", b.hive, "--agent", b.agent, "--task", t,
                      "--integration-sha", "abc1234", at=LATER)
        self.assertEqual((rc, out), (0, f"already accepted {t}"))

    def test_handoff_original_first(self):
        b, t, f = self._handoff_with_finished_fix()
        self.assertEqual(b.handle_complete(t, says("right\n")), "skip")     # f awaits acceptance
        self.assertEqual(b.handle_complete(f, says("right\n")), "pass")
        self.assertEqual(json.loads((b.hive / "accepted" / f"{t}.json").read_text())["via"], [f])

    def test_crash_left_review_branch_matching_is_resumed(self):
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")
        wt, review = self.m.wt, f"rip-swarm/review-{t}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", sha)                      # crash before the check
        merged = git(wt, "rev-parse", review)
        seen = []
        self.assertEqual(self.m.review(t, sha, lambda w: seen.append(git(w, "rev-parse", "HEAD")) or True), "pass")
        self.assertEqual(seen, [merged])                                    # no second merge

    def test_failed_resume_switch_is_an_error(self):
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")
        wt, review = self.m.wt, f"rip-swarm/review-{t}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", sha)                      # crash before the check
        git(wt, "switch", "rip-swarm/integration")
        other = Path(self.tmp.name) / "elsewhere"
        git(self.repo, "worktree", "add", "-q", str(other), review)        # switch now refuses it
        self.assertEqual(self.m.review_block(t, sha), "error")
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertFalse((self.m.hive / "accepted" / f"{t}.json").exists())

    def test_crash_left_review_branch_mismatched_is_recreated(self):
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")
        wt = self.m.wt
        git(wt, "switch", "-c", f"rip-swarm/review-{t}", git(wt, "rev-parse", "rip-swarm/integration"))
        self.assertEqual(self.m.review(t, sha, says("T\n")), "pass")
        self.assertEqual(run(self.repo, "merge-base", "--is-ancestor", sha, "rip-swarm/integration").returncode, 0)

    def test_crash_mid_conflict_is_aborted_and_recreated(self):
        x = self.m.post("X")
        y = self.m.post("Y")
        self.w1.tick()
        self.w2.tick()
        self._done(self.w1, x, "one\n")
        sha_y = self._done(self.w2, y, "two\n")
        self.assertEqual(self.m.review(x, self.m.result_sha(x), says("one\n")), "pass")
        wt = self.m.wt
        git(wt, "switch", "-c", f"rip-swarm/review-{y}", git(wt, "rev-parse", "rip-swarm/integration"))
        self.assertNotEqual(run(wt, "merge", "--no-ff", "--no-edit", sha_y).returncode, 0)  # crash mid-merge
        self.assertEqual(self.m.review(y, sha_y, says("one\ntwo\n")), "conflict")
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertNotEqual(run(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        self.assertNotEqual(run(wt, "show-ref", "--verify", "--quiet", f"refs/heads/rip-swarm/review-{y}").returncode, 0)

    def test_orphaned_original_and_reject_cascade_reach_all_complete(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        self.w1.tick()
        self._done(self.w1, t, "wrong\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t)
        self.m.reject(f, "not worth it")                                   # the master abandons the fix
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} reject"))
        board = read_board(self.m.hive, T0)
        self.assertTrue(all(view.settled for view in fixers(board, t)))    # T is orphaned
        self.m.reject(t, "fix abandoned")
        self.m.reject(d, f"dependency {t} rejected")                       # cascade
        wakes = sorted(self.m.tick().detail for _ in range(2))
        self.assertEqual(wakes, sorted([f"{t} reject", f"{d} reject"]))
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_reject_cascade_survives_a_master_change(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        e = self.m.post("E", "--after", d)
        self.m.reject(t, "not worth it")
        self.m.reject(d, f"dependency {t} rejected")                       # master A dies here
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        self.assertEqual(b.tick(), Wake("task-finished", f"{d} reject"))    # E still waits on D
        self.assertEqual(b.tick(), Wake("task-finished", f"{d} reject"))    # until E is rejected
        b.reject(e, f"dependency {d} rejected")
        self.assertEqual(b.tick(), Wake("task-finished", f"{e} reject"))
        self.assertEqual(b.tick(), Wake("all-complete"))

    def test_reject_cascade_survives_the_same_master_dying(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        e = self.m.post("E", "--after", d)
        self.m.reject(t, "not worth it")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.m.reject(d, f"dependency {t} rejected")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{d} reject"))
        # the session dies here; the same agent's next wait keeps its state file
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{d} reject"))
        self.m.reject(e, f"dependency {d} rejected")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{e} reject"))
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def _chain(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        e = self.m.post("E", "--after", d)
        return t, d, e

    def test_reject_handler_resumed_after_a_crash_posts_no_second_replacement(self):
        t, d, e = self._chain()
        self.m.reject(t, "wrong approach")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        r = self.m.post(f"T (replaces {t})")                              # the session dies here
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))    # D still waits on T
        self.m.handle_reject(t, wanted={t, d, e})
        self.assertEqual(self.m.replacements(t), [r])
        [d2], [e2] = self.m.replacements(d), self.m.replacements(e)
        board = read_board(self.m.hive, T0)
        self.assertEqual((board[d2].after, board[e2].after), ((r,), (d2,)))
        self.assertTrue(board[d].rejected and board[e].rejected)
        # The cascade's own tombstones wake the master; they change nothing.
        for dep in sorted([d, e]):
            self.assertEqual(self.m.tick(), Wake("task-finished", f"{dep} reject"))
            self.assertEqual(self.m.handle_reject(dep, wanted={t, d, e}), [])
        self.assertIsNone(self.m.tick())                                  # R, D2, E2 are the plan now
        self.assertEqual(len(list((self.m.hive / "inbox").glob("task_*.json"))), 6)

    def test_a_cascade_reject_posts_no_replacement_of_its_own(self):
        t, d, e = self._chain()
        self.m.reject(t, "not worth it")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.assertEqual(self.m.handle_reject(t), [f"reject {d}", f"reject {e}"])
        for dep in sorted([d, e]):
            self.assertEqual(self.m.tick(), Wake("task-finished", f"{dep} reject"))
            self.assertEqual(self.m.handle_reject(dep, wanted={d, e}), [])     # the note decides
        self.assertEqual(self.m.replacements(d), [])
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_a_replacement_never_waits_on_a_rejected_task(self):
        t, d, e = self._chain()
        self.m.reject(t, "not needed")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.m.handle_reject(t, wanted={d})                               # T is not wanted, D is
        [d2] = self.m.replacements(d)
        self.assertEqual(read_board(self.m.hive, T0)[d2].after, ())
        for dep in sorted([d, e]):
            self.assertEqual(self.m.tick(), Wake("task-finished", f"{dep} reject"))
        self.assertIsNone(self.m.tick())                                  # no reject wake recurs

    def test_replacing_a_follow_up_keeps_fixes_and_leaves_the_original(self):
        t = self.m.post("T")
        self.w1.tick()
        self._done(self.w1, t, "wrong\n")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t, "--body", "t.txt must say right")
        self.m.reject(f, "wrong approach")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} reject"))
        done = self.m.handle_reject(f, wanted={f})
        [r] = self.m.replacements(f)
        self.assertEqual(done, [f"post {r}"])                             # no orphan step for T
        doc = json.loads((self.m.hive / "inbox" / f"{r}.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["fixes"], doc["body"]), (t, "t.txt must say right"))
        self.assertFalse(read_board(self.m.hive, T0)[t].settled)          # R decides T now
        self.assertEqual(self.m.handle_reject(f, wanted={f}), [])

    def test_the_fixes_walk_stops_at_a_rejected_original(self):
        t = self.m.post("T")
        self.w1.tick()
        bad = self._done(self.w1, t, "wrong\n")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t)
        self.m.reject(t, "dropped")                                        # while F is still open
        self.assertEqual(self.w1.claim(f), 0)
        self.assertEqual(self.w1.start_task(bad)[:2], ("reset", "merged"))
        self.w1.work("t.txt", "right\n", "fix T")
        self.w1.complete(f)
        self.assertEqual(self.m.handle_complete(f, says("right\n")), "pass")
        self.assertFalse((self.m.hive / "accepted" / f"{t}.json").exists())

    def test_reject_handler_skips_an_orphan_already_settled(self):
        t = self.m.post("T")
        self.w1.tick()
        self._done(self.w1, t, "wrong\n")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t)
        self.m.reject(f, "not worth it")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} reject"))
        self.assertEqual(self.m.handle_reject(f), [f"orphan {t}"])
        self.assertEqual(self.m.handle_reject(f), [])                     # the wake again: X is settled

    def test_badsha_is_rejected_untouched(self):
        t = self.m.post("T")
        self.w1.tick()
        self.assertEqual(self.w1.claim(t), 0)
        (self.w1.wt / "t.txt").write_text("T\n", encoding="utf-8")
        blob = git(self.w1.wt, "hash-object", "-w", "t.txt")[:7]          # an object, not a commit
        ref = f"rip-swarm/{self.w1.agent}@{blob}"
        rc, out = cli("complete", "--hive", self.w1.hive, "--task", t, "--agent", self.w1.agent,
                      "--result-ref", ref)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        wt, before = self.m.wt, git(self.m.wt, "rev-parse", "rip-swarm/integration")
        self.assertEqual(self.m.handle_complete(t, says("T\n")), "badsha")
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertEqual(git(wt, "rev-parse", "rip-swarm/integration"), before)
        self.assertNotEqual(run(wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{t}").returncode, 0)
        self.m.reject(t, f"result_ref {ref} is not a commit")
        self.assertTrue(next((self.m.hive / "claims").glob(f"{t}.reject.*.json"), None))
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_rejected_work_does_not_ride_into_the_next_result(self):
        t1 = self.m.post("T1")
        self.m.now += timedelta(milliseconds=1)
        t2 = self.m.post("T2")
        self.w1.tick()
        self.assertEqual(self.w1.claim(t1), 0)
        self.assertEqual(self.w1.start_task()[0], "merged")
        bad = self.w1.work("bad.txt", "bad\n", "work T1")
        self.w1.complete(t1)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t1} complete"))
        self.assertEqual(self.m.handle_complete(t1, lambda wt: False), "short")   # not worth pursuing
        self.m.reject(t1, "not worth pursuing")
        self.assertEqual(self.w1.claim(t2), 0)                                   # same worker, next task
        sync_, build, kept = self.w1.start_task()
        self.assertEqual((sync_, build), ("reset", "none"))
        self.assertEqual(git(self.w1.wt, "rev-parse", kept), bad)               # the old tip stays reachable
        self.w1.work("good.txt", "good\n", "work T2")
        self.w1.complete(t2)
        self.m.tick()
        self.assertEqual(self.m.handle_complete(
            t2, lambda wt: (wt / "good.txt").is_file() and not (wt / "bad.txt").exists()), "pass")
        self.assertNotEqual(run(self.repo, "merge-base", "--is-ancestor", bad,
                                "rip-swarm/integration").returncode, 0)

    def test_all_complete_then_the_master_leaves(self):
        t = self.m.post("T")
        self.w1.tick()
        self._done(self.w1, t, "T\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        self.assertEqual(self.m.handle_complete(t, says("T\n")), "pass")
        self.assertEqual(self.m.tick(), Wake("all-complete"))
        lines = leave(self.m.hive, self.m.agent, T0)
        self.assertIn("released orchestrator", lines)
        self.assertIsNone(remote_show(self.origin, "claims/orchestrator.json"))
        self.assertEqual(git(self.repo, "show", "rip-swarm/integration:t.txt"), "T")
        self.assertTrue((self.repo / ".worktrees" / "integration").is_dir())


if __name__ == "__main__":
    unittest.main()
