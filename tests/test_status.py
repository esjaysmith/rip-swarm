# tests/test_status.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import complete, reject, try_claim
from rip_swarm.inbox import create_task
from rip_swarm.io import atomic_write_json
from rip_swarm.orchestrator import promote
from rip_swarm.status import format_status, status_report
from rip_swarm.timeutil import add_seconds

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

def _reg(hive, body="- id: alice\n  harness: claude-code\n  role: worker\n"):
    p = hive / "agents" / "registry.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")

class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        _reg(self.hive)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty(self):
        r = status_report(self.hive, T0)
        self.assertEqual(r["active_claims"], [])
        self.assertEqual(r["inbox_without_claim"], [])
        self.assertIsNone(r["orchestrator"]["agent"])

    def test_inbox_without_claim(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        r = status_report(self.hive, T0)
        self.assertEqual(r["inbox_without_claim"], [t["id"]])

    def test_current_mismatch(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        atomic_write_json(
            self.hive / "orchestrator" / "CURRENT.json",
            {"agent": "eve", "harness": "x", "lease_expires_at": "2026-09-17T09:31:00Z",
             "reason": "x", "claim_id": "clm_x"},
        )
        r = status_report(self.hive, T0)
        self.assertTrue(r["current_mismatch"])
        self.assertFalse(r["orchestrator"]["matches_claim"])

    def test_jsonl_parse_error(self):
        log = self.hive / "store" / "messages.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{bad\n", encoding="utf-8")
        r = status_report(self.hive, T0)
        self.assertEqual(r["jsonl_parse_errors"][0]["line"], 1)

    def test_unknown_agent_on_claim(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "ghost", "codex", T0, 900)
        r = status_report(self.hive, T0)
        self.assertIn("ghost", r["unknown_agents"])
        self.assertIn(t["id"], format_status(r))

    def test_expired_claim_file(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        r = status_report(self.hive, add_seconds(T0, 901))
        self.assertEqual(r["expired_claim_files"], [t["id"]])

    def test_orchestrator_excluded_from_active_claims(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, "orchestrator", "alice", "claude-code", T0, 1800)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        r = status_report(self.hive, T0)
        self.assertEqual([c["task_id"] for c in r["active_claims"]], [t["id"]])
        self.assertNotIn("orchestrator", r["inbox_without_claim"])

    def test_inbox_without_claim_excludes_completed(self):
        done = create_task(self.hive, title="done", created_by="op", now=T0)
        rejected = create_task(self.hive, title="rej", created_by="op", now=T0)
        open_t = create_task(self.hive, title="open", created_by="op", now=T0)
        try_claim(self.hive, done["id"], "alice", "claude-code", T0, 900)
        complete(self.hive, done["id"], "alice", T0, result_ref="r")
        try_claim(self.hive, rejected["id"], "alice", "claude-code", T0, 900)
        reject(self.hive, rejected["id"], "alice", T0, note="no")
        r = status_report(self.hive, T0)
        self.assertEqual(r["inbox_without_claim"], sorted([rejected["id"], open_t["id"]]))

    def test_corrupt_claims_reported_and_no_crash(self):
        claims = self.hive / "claims"
        claims.mkdir(parents=True, exist_ok=True)
        (claims / "task_bad.json").write_text("{not json", encoding="utf-8")
        (claims / "task_empty.json").write_text("", encoding="utf-8")
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        r = status_report(self.hive, T0)
        self.assertEqual(sorted(c["task_id"] for c in r["corrupt_claims"]),
                         ["task_bad", "task_empty"])
        for c in r["corrupt_claims"]:
            self.assertEqual(sorted(c), ["error", "path", "task_id"])
            self.assertTrue(c["error"])
        self.assertEqual([c["task_id"] for c in r["active_claims"]], [t["id"]])
        out = format_status(r)
        self.assertIn("corrupt_claims:", out)
        self.assertIn("task_bad", out)

    def test_corrupt_claims_empty_section_printed(self):
        r = status_report(self.hive, T0)
        self.assertEqual(r["corrupt_claims"], [])
        self.assertIn("corrupt_claims:", format_status(r))

if __name__ == "__main__":
    unittest.main()
