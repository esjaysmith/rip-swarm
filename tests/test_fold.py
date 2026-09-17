# tests/test_fold.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import complete, try_claim
from rip_swarm.fold import Expired, Free, Holder, active_holder, active_set, open_claim_count
from rip_swarm.inbox import create_task
from rip_swarm.timeutil import add_seconds

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

class TestFold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        self.task = create_task(self.hive, title="T", created_by="op", now=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_free_expired_holder(self):
        self.assertIsInstance(active_holder(self.hive, self.task["id"], T0), Free)
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        h = active_holder(self.hive, self.task["id"], T0)
        self.assertIsInstance(h, Holder)
        self.assertEqual(h.agent, "alice")
        self.assertIsInstance(active_holder(self.hive, self.task["id"], add_seconds(T0, 901)), Expired)

    def test_jsonl_ignored_for_exclusivity(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        log = self.hive / "store" / "claims.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "claim", "agent": "mallory", "task_id": self.task["id"]}) + "\n")
        self.assertEqual(active_holder(self.hive, self.task["id"], T0).agent, "alice")

    def test_audit_appended_on_claim(self):
        doc = try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        lines = (self.hive / "store" / "claims.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["action"], "claim")
        self.assertEqual(row["claim_id"], doc["claim_id"])
        self.assertNotIn("id", row)
        self.assertEqual(row["task_id"], self.task["id"])
        self.assertTrue(row["ts"].endswith("Z"))

    def test_open_claim_count_skips_complete(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        self.assertEqual(open_claim_count(self.hive, "alice", T0), 1)
        complete(self.hive, self.task["id"], "alice", T0, result_ref="outbox/x.json")
        self.assertEqual(open_claim_count(self.hive, "alice", T0), 0)
        self.assertEqual(active_set(self.hive, T0), {})

    def test_orchestrator_baton_not_counted(self):
        try_claim(self.hive, "orchestrator", "alice", "claude-code", T0, 1800)
        self.assertEqual(open_claim_count(self.hive, "alice", T0), 0)
        self.assertIn("orchestrator", active_set(self.hive, T0))

    def test_expired_carries_agent(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        e = active_holder(self.hive, self.task["id"], add_seconds(T0, 901))
        self.assertIsInstance(e, Expired)
        self.assertEqual(e.agent, "alice")

if __name__ == "__main__":
    unittest.main()
