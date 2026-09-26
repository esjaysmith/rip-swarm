# tests/test_acceptance.py — accept records and master reject (spec §7.4)
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, local_hive
from rip_swarm import cli
from rip_swarm.acceptance import accept_task, holds_baton, master_reject
from rip_swarm.board import list_tombstones, read_board
from rip_swarm.claim import ClaimDenied, complete, try_claim
from rip_swarm.cli import main
from rip_swarm.inbox import create_task
from rip_swarm.orchestrator import promote
from rip_swarm.paths import HivePaths

SHA = "0123abc4567def"


class TestAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))
        promote(self.hive, agent="alice", harness="claude-code", now=T0, lease_seconds=1800,
                reason="master", allow_self_promote=False, operators=["op"], by="op")

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, title, **kw):
        return create_task(self.hive, title=title, created_by="alice", now=T0, **kw)["id"]

    def _done(self, tid):
        try_claim(self.hive, tid, "bob", "grok", T0, 900)
        complete(self.hive, tid, "bob", T0, result_ref="rip-swarm/bob@0123abc")

    def test_only_the_live_baton_holder_accepts(self):
        t = self._task("t")
        self._done(t)
        self.assertTrue(holds_baton(self.hive, "alice", T0))
        with self.assertRaises(ClaimDenied):
            accept_task(self.hive, agent="bob", task_id=t, integration_sha=SHA, now=T0)
        # Review focus 5: a master whose baton expired cannot accept.
        late = T0 + timedelta(seconds=1801)
        with self.assertRaises(ClaimDenied):
            accept_task(self.hive, agent="alice", task_id=t, integration_sha=SHA, now=late)
        self.assertFalse(HivePaths(self.hive).accepted_record(t).exists())

    def test_accept_needs_complete_or_accepted_via(self):
        t = self._task("t")
        with self.assertRaises(ClaimDenied):
            accept_task(self.hive, agent="alice", task_id=t, integration_sha=SHA, now=T0)
        self._done(t)
        f = self._task("f", fixes=t)
        with self.assertRaises(ClaimDenied):
            accept_task(self.hive, agent="alice", task_id=t, integration_sha=SHA, via=[f], now=T0)
        self._done(f)
        accept_task(self.hive, agent="alice", task_id=f, integration_sha=SHA, now=T0)
        doc = accept_task(self.hive, agent="alice", task_id=t, integration_sha=SHA, via=[f], now=T0)
        self.assertEqual(doc["via"], [f])
        self.assertEqual(doc["by"], "alice")

    def test_accept_is_idempotent(self):
        t = self._task("t")
        self._done(t)
        first = accept_task(self.hive, agent="alice", task_id=t, integration_sha=SHA, now=T0)
        path = HivePaths(self.hive).accepted_record(t)
        before = path.read_text(encoding="utf-8")
        again = accept_task(self.hive, agent="alice", task_id=t, integration_sha="fff0000", now=T0)
        self.assertEqual(again, {"task_id": t, "already": True})
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(json.loads(before)["integration_sha"], first["integration_sha"])

    def test_accept_rejects_a_non_hex_sha(self):
        t = self._task("t")
        self._done(t)
        with self.assertRaises(ValueError):
            accept_task(self.hive, agent="alice", task_id=t, integration_sha="HEAD", now=T0)

    def test_master_rejects_an_unclaimed_task(self):
        t = self._task("t")
        body = master_reject(self.hive, agent="alice", task_id=t, note="dropped", now=T0)
        self.assertEqual(body["action"], "reject")
        self.assertTrue(read_board(self.hive, T0)[t].rejected)
        audit = (HivePaths(self.hive).claims_jsonl).read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(audit[-1])["action"], "reject")
        with self.assertRaises(ClaimDenied) as ctx:
            try_claim(self.hive, t, "bob", "grok", T0, 900)
        self.assertIn("rejected", str(ctx.exception))
        again = master_reject(self.hive, agent="alice", task_id=t, note="again", now=T0)
        self.assertEqual(again, {"task_id": t, "already": True})

    def test_master_rejects_an_expired_claim_after_tombstoning_it(self):
        t = self._task("t")
        try_claim(self.hive, t, "bob", "grok", T0, 60)
        later = T0 + timedelta(seconds=120)
        master_reject(self.hive, agent="alice", task_id=t, note="stale", now=later)
        actions = [s.action for s in list_tombstones(self.hive) if s.task_id == t]
        self.assertEqual(sorted(actions), ["expired", "reject"])
        self.assertFalse(HivePaths(self.hive).claim(t).exists())

    def test_master_reject_refuses_a_live_claim(self):
        t = self._task("t")
        try_claim(self.hive, t, "bob", "grok", T0, 900)
        with self.assertRaises(ClaimDenied) as ctx:
            master_reject(self.hive, agent="alice", task_id=t, note="x", now=T0)
        self.assertIn("held by bob", str(ctx.exception))

    def test_non_master_cannot_reject_an_unclaimed_task(self):
        t = self._task("t")
        with self.assertRaises(ClaimDenied):
            master_reject(self.hive, agent="bob", task_id=t, note="x", now=T0)

    def test_cli_accept_and_master_reject(self):
        # The baton was granted at T0; pin the CLI clock there.
        clock = mock.patch("rip_swarm.cli.now_utc", return_value=T0)
        clock.start()
        self.addCleanup(clock.stop)
        t = self._task("t")
        self._done(t)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["accept", "--hive", str(self.hive), "--local", "--agent", "alice",
                       "--task", t, "--integration-sha", SHA])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), f"accepted {t} at {SHA}\n")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["accept", "--hive", str(self.hive), "--local", "--agent", "alice",
                       "--task", t, "--integration-sha", SHA])
        self.assertEqual((rc, out.getvalue()), (0, f"already accepted {t}\n"))
        u = self._task("u")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["reject", "--hive", str(self.hive), "--local", "--agent", "alice",
                       "--task", u, "--note", "drop"])
        self.assertEqual((rc, out.getvalue()), (0, f"reject {u} as alice\n"))
        v = self._task("v")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["reject", "--hive", str(self.hive), "--local", "--agent", "bob",
                       "--task", v, "--note", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("no active claim", err.getvalue())

    def test_reject_cli_uses_an_explicit_narrow_allowlist(self):
        # Controller ruling on spec §10: the CLI `reject` publish must not fall
        # back to gitops.default_allow, which also permits store/messages.jsonl
        # and agents/<A>/outbox/*.json. It must pass exactly the master-reject
        # allowlist, whichever branch (own claim or baton-holder) fires.
        t = self._task("t")
        captured: dict = {}

        def fake_run_op(hive, *, local, task_id, message, op, agent=None, now=None, allow=None):
            captured["allow"] = allow
            return op()

        with mock.patch.object(cli, "_run_op", side_effect=fake_run_op):
            args = cli._parser().parse_args([
                "reject", "--hive", str(self.hive), "--local", "--agent", "alice",
                "--task", t, "--note", "drop",
            ])
            cli._reject(args, self.hive, T0)
        self.assertEqual(
            captured["allow"],
            [f"claims/{t}.json", f"claims/{t}.*.json", "store/claims.jsonl"],
        )
        self.assertNotIn("store/messages.jsonl", captured["allow"])
        self.assertFalse(any("outbox" in p for p in captured["allow"]))


if __name__ == "__main__":
    unittest.main()
