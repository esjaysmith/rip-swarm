# tests/test_fold.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import complete, try_claim
from rip_swarm.fold import (
    Corrupt,
    Expired,
    Free,
    Holder,
    active_holder,
    active_set,
    corrupt_claims,
    open_claim_count,
)
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

    def _write_claim(self, name: str, text: str) -> Path:
        p = self.hive / "claims" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _good(self, task_id, agent="alice", expires="2026-09-17T09:16:00Z"):
        return json.dumps({
            "task_id": task_id, "claim_id": "clm_x", "agent": agent,
            "harness": "claude-code", "exclusive": True,
            "created_at": "2026-09-17T09:01:00Z", "expires_at": expires,
        })

    def test_corrupt_bad_json(self):
        self._write_claim("task_bad.json", "{not json")
        rec = active_holder(self.hive, "task_bad", T0)
        self.assertIsInstance(rec, Corrupt)
        self.assertEqual(rec.task_id, "task_bad")
        self.assertTrue(rec.error)
        self.assertEqual(rec.path.name, "task_bad.json")

    def test_corrupt_empty_file(self):
        self._write_claim("task_empty.json", "")
        self.assertIsInstance(active_holder(self.hive, "task_empty", T0), Corrupt)

    def test_corrupt_missing_fields(self):
        self._write_claim("task_noexp.json", json.dumps({"task_id": "task_noexp", "agent": "a"}))
        self._write_claim("task_noagent.json", json.dumps(
            {"task_id": "task_noagent", "expires_at": "2026-09-17T09:16:00Z"}))
        self.assertIsInstance(active_holder(self.hive, "task_noexp", T0), Corrupt)
        self.assertIsInstance(active_holder(self.hive, "task_noagent", T0), Corrupt)

    def test_corrupt_bad_expires_at_value(self):
        self._write_claim("task_badts.json", json.dumps(
            {"task_id": "task_badts", "agent": "a", "claim_id": "c", "expires_at": "nope"}))
        self.assertIsInstance(active_holder(self.hive, "task_badts", T0), Corrupt)

    def test_corrupt_not_an_object(self):
        self._write_claim("task_list.json", "[1, 2]")
        self.assertIsInstance(active_holder(self.hive, "task_list", T0), Corrupt)

    def test_active_set_keys_on_filename_stem_and_flags_mismatch(self):
        self._write_claim("task_aaa.json", self._good("task_bbb"))
        self._write_claim("task_ccc.json", self._good("task_ccc"))
        got = active_set(self.hive, T0)
        self.assertEqual(sorted(got), ["task_ccc"])
        self.assertEqual(got["task_ccc"].task_id, "task_ccc")
        rec = active_holder(self.hive, "task_aaa", T0)
        self.assertIsInstance(rec, Corrupt)
        self.assertIn("task_bbb", rec.error)

    def test_corrupt_does_not_crash_active_set_or_count(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        self._write_claim("task_bad.json", "{not json")
        self._write_claim("task_empty.json", "")
        self.assertEqual(sorted(active_set(self.hive, T0)), [self.task["id"]])
        self.assertEqual(open_claim_count(self.hive, "alice", T0), 1)

    def test_corrupt_claims_lists_them(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        self._write_claim("task_bad.json", "{not json")
        self._write_claim("task_empty.json", "")
        got = corrupt_claims(self.hive)
        self.assertEqual(sorted(c.task_id for c in got), ["task_bad", "task_empty"])
        for c in got:
            self.assertIsInstance(c, Corrupt)
            self.assertTrue(c.error)

    def test_corrupt_claims_empty_when_clean(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        self.assertEqual(corrupt_claims(self.hive), [])
        self.assertEqual(corrupt_claims(Path(self.tmp.name) / "nope"), [])

    def test_tombstones_ignored_by_active_set(self):
        self._write_claim("task_zzz.complete.20260917T090100Z.json", self._good("task_zzz"))
        self.assertEqual(active_set(self.hive, T0), {})
        self.assertEqual(corrupt_claims(self.hive), [])

if __name__ == "__main__":
    unittest.main()
