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
        self.origin, self.repo = make_project(Path(self.tmp.name))
        self.m = join(self.repo, role="master", harness="grok", now=T0)

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
