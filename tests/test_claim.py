# tests/test_claim.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied, complete, heartbeat, reject, release, try_claim
from rip_swarm.inbox import create_task
from rip_swarm.io import read_json
from rip_swarm.timeutil import add_seconds, format_z, parse_z

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

class TestClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        self.task = create_task(self.hive, title="T", created_by="op", now=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_creates_file(self):
        doc = try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        self.assertEqual(doc["agent"], "alice")
        self.assertEqual(doc["expires_at"], "2026-09-17T09:16:00Z")
        self.assertTrue(doc["exclusive"])
        path = self.hive / "claims" / f"{self.task['id']}.json"
        self.assertEqual(read_json(path)["claim_id"], doc["claim_id"])

    def test_second_claimer_denied_while_unexpired(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        with self.assertRaises(ClaimDenied):
            try_claim(self.hive, self.task["id"], "bob", "codex", T0, 900)
        self.assertEqual(read_json(self.hive / "claims" / f"{self.task['id']}.json")["agent"], "alice")

    def test_expired_steal_renames_then_creates(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        later = add_seconds(T0, 901)
        doc = try_claim(self.hive, self.task["id"], "bob", "codex", later, 900)
        self.assertEqual(doc["agent"], "bob")
        tombstones = list((self.hive / "claims").glob(f"{self.task['id']}.expired.*"))
        self.assertEqual(len(tombstones), 1)

    def test_heartbeat_holder_only(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        hb = heartbeat(self.hive, self.task["id"], "alice", add_seconds(T0, 60), 900)
        self.assertEqual(hb["expires_at"], "2026-09-17T09:17:00Z")
        with self.assertRaises(ClaimDenied):
            heartbeat(self.hive, self.task["id"], "bob", add_seconds(T0, 60), 900)

    def test_complete_requires_result_ref_and_tombstones(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        with self.assertRaises(ClaimDenied):
            complete(self.hive, self.task["id"], "alice", T0, result_ref="  ")
        complete(self.hive, self.task["id"], "alice", T0, result_ref="agents/alice/outbox/msg_x.json")
        self.assertFalse((self.hive / "claims" / f"{self.task['id']}.json").exists())
        done = list((self.hive / "claims").glob(f"{self.task['id']}.complete.*"))
        self.assertEqual(len(done), 1)
        self.assertEqual(read_json(done[0])["result_ref"], "agents/alice/outbox/msg_x.json")

    def test_non_holder_complete_ignored(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        with self.assertRaises(ClaimDenied):
            complete(self.hive, self.task["id"], "bob", T0, result_ref="x")
        self.assertTrue((self.hive / "claims" / f"{self.task['id']}.json").exists())

    def test_missing_inbox_denied(self):
        with self.assertRaises(ClaimDenied):
            try_claim(self.hive, "task_01J00000000000000000000000", "alice", "claude-code", T0, 900)

    def test_orchestrator_needs_no_inbox(self):
        doc = try_claim(self.hive, "orchestrator", "alice", "claude-code", T0, 1800)
        self.assertEqual(doc["task_id"], "orchestrator")

    def test_reclaim_by_holder_is_idempotent(self):
        a = try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        b = try_claim(self.hive, self.task["id"], "alice", "claude-code", add_seconds(T0, 10), 900)
        self.assertEqual(a["claim_id"], b["claim_id"])
        self.assertEqual(b["expires_at"], a["expires_at"])

    def test_expired_holder_cannot_complete_without_reclaim(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        later = add_seconds(T0, 901)
        with self.assertRaises(ClaimDenied):
            complete(self.hive, self.task["id"], "alice", later, result_ref="x")
        doc = try_claim(self.hive, self.task["id"], "alice", "claude-code", later, 900)
        complete(self.hive, self.task["id"], "alice", later, result_ref="x")
        self.assertTrue(list((self.hive / "claims").glob(f"{self.task['id']}.expired.*")))
        self.assertEqual(read_json(next((self.hive / "claims").glob(f"{self.task['id']}.complete.*")))["claim_id"], doc["claim_id"])

if __name__ == "__main__":
    unittest.main()
