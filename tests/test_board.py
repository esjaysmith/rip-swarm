# tests/test_board.py — derived task state, after/fixes, claim refusals (spec §7.3, §7.4)
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from hivekit import T0, local_hive
from rip_swarm.board import (
    blocked_by,
    finished_reason,
    fixers,
    list_tombstones,
    read_board,
)
from rip_swarm.claim import ClaimDenied, complete, reject, release, try_claim
from rip_swarm.cli import main
from rip_swarm.inbox import InboxError, create_task
from rip_swarm.io import excl_create_json
from rip_swarm.paths import HivePaths

MISSING = "task_" + "0" * 26


class TestBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, title, **kw):
        return create_task(self.hive, title=title, created_by="op", now=T0, **kw)["id"]

    def _accept(self, tid):
        excl_create_json(HivePaths(self.hive).accepted_record(tid),
                         {"task_id": tid, "by": "alice", "at": "2026-09-26T10:05:00Z",
                          "integration_sha": "abc1234", "via": []})

    def _done(self, tid, agent="alice"):
        try_claim(self.hive, tid, agent, "claude-code" if agent == "alice" else "grok", T0, 900)
        complete(self.hive, tid, agent, T0, result_ref=f"rip-swarm/{agent}@abc1234")

    def test_after_and_fixes_must_already_exist(self):
        with self.assertRaises(InboxError):
            self._task("b", after=[MISSING])
        with self.assertRaises(InboxError):
            self._task("f", fixes=MISSING)
        with self.assertRaises(InboxError):
            self._task("bad", after=["not-a-task"])
        a = self._task("a")
        doc = create_task(self.hive, title="b", created_by="op", now=T0, after=[a, a], fixes=a)
        self.assertEqual(doc["after"], [a])
        self.assertEqual(doc["fixes"], a)
        plain = create_task(self.hive, title="c", created_by="op", now=T0)
        self.assertNotIn("after", plain)
        self.assertNotIn("fixes", plain)

    def test_dependency_unblocks_on_acceptance_not_on_complete(self):
        a = self._task("a")
        b = self._task("b", after=[a])
        self.assertEqual(read_board(self.hive, T0)[b].blocked_by, (a,))
        self.assertFalse(read_board(self.hive, T0)[b].is_open)
        self._done(a)
        self.assertEqual(blocked_by(self.hive, b), [a])
        with self.assertRaises(ClaimDenied) as ctx:
            try_claim(self.hive, b, "bob", "grok", T0, 900)
        self.assertIn(f"blocked by {a}", str(ctx.exception))
        self._accept(a)
        self.assertTrue(read_board(self.hive, T0)[b].is_open)
        try_claim(self.hive, b, "bob", "grok", T0, 900)

    def test_finished_tasks_cannot_be_claimed(self):
        done = self._task("done")
        self._done(done)
        rej = self._task("rej")
        try_claim(self.hive, rej, "alice", "claude-code", T0, 900)
        reject(self.hive, rej, "alice", T0, note="no")
        acc = self._task("acc")
        self._done(acc)
        self._accept(acc)
        cases = {done: "already completed", rej: "rejected", acc: "already accepted"}
        for tid, reason in cases.items():
            self.assertEqual(finished_reason(self.hive, tid), reason)
            with self.assertRaises(ClaimDenied) as ctx:
                try_claim(self.hive, tid, "bob", "grok", T0, 900)
            self.assertEqual(str(ctx.exception), f"{tid} {reason}")
            self.assertFalse(HivePaths(self.hive).claim(tid).exists())

    def test_expired_claim_is_open_and_bumps_the_generation(self):
        t = self._task("t")
        try_claim(self.hive, t, "alice", "claude-code", T0, 60)
        later = T0 + timedelta(seconds=120)
        view = read_board(self.hive, later)[t]
        self.assertEqual((view.claim, view.holder), ("expired", "alice"))
        self.assertTrue(view.is_open)
        self.assertEqual(view.generation, 1)
        try_claim(self.hive, t, "bob", "grok", later, 900)       # steal
        view = read_board(self.hive, later)[t]
        self.assertEqual((view.claim, view.returns, view.generation), ("live", 1, 1))
        release(self.hive, t, "bob", later)
        self.assertEqual(read_board(self.hive, later)[t].generation, 2)
        actions = [x.action for x in list_tombstones(self.hive)]
        self.assertEqual(sorted(actions), ["expired", "release"])

    def test_awaiting_acceptance_settled_and_fixers(self):
        t = self._task("t")
        self._done(t)
        f = self._task("f", fixes=t)
        board = read_board(self.hive, T0)
        self.assertTrue(board[t].awaiting_acceptance)
        self.assertFalse(board[t].settled)
        self.assertEqual([v.task_id for v in fixers(board, t)], [f])
        self._accept(t)
        board = read_board(self.hive, T0)
        self.assertFalse(board[t].awaiting_acceptance)
        self.assertTrue(board[t].settled)

    def test_cli_inbox_add_after_and_fixes(self):
        import io
        from contextlib import redirect_stdout
        a = self._task("a")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["inbox-add", "--hive", str(self.hive), "--local", "--title", "b",
                       "--created-by", "op", "--after", a, "--fixes", a])
        self.assertEqual(rc, 0)
        b = out.getvalue().split()[1].rstrip(":")
        view = read_board(self.hive, T0)[b]
        self.assertEqual((view.after, view.fixes), ((a,), a))
        self.assertEqual(
            main(["inbox-add", "--hive", str(self.hive), "--local", "--title", "c",
                  "--created-by", "op", "--after", MISSING]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
