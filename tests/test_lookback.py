# tests/test_lookback.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.audit import append_jsonl
from rip_swarm.claim import release, try_claim
from rip_swarm.inbox import create_task
from rip_swarm.lookback import write_lookback
from rip_swarm.outbox import write_message
from rip_swarm.timeutil import add_seconds

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)
HEADINGS = [
    "# Lookback 2026-09-17",
    "## Double claims",
    "## Expired leases",
    "## CURRENT vs orchestrator claim",
    "## Inbox with no claim",
    "## JSONL parse errors",
    "## Suggested PROTOCOL/profile diffs",
]

def _reg2(hive):
    (hive / "agents" / "registry.yaml").write_text(
        "- id: alice\n  harness: claude-code\n  role: worker\n"
        "- id: bob\n  harness: codex\n  role: worker\n",
        encoding="utf-8",
    )


class TestLookback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name) / "hive"
        self.hive.mkdir()
        (self.hive / "PROTOCOL.md").write_text("# PROTOCOL\n", encoding="utf-8")
        (self.hive / "agents").mkdir()
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_headings_and_expired(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        proto_mtime = (self.hive / "PROTOCOL.md").stat().st_mtime
        path = write_lookback(self.hive, add_seconds(T0, 901))
        text = path.read_text(encoding="utf-8")
        for h in HEADINGS:
            self.assertIn(h, text)
        self.assertIn(t["id"], text)
        self.assertEqual(path.name, "2026-09-17.md")
        self.assertEqual((self.hive / "PROTOCOL.md").stat().st_mtime, proto_mtime)

    def test_second_same_day_suffix(self):
        write_lookback(self.hive, T0)
        p2 = write_lookback(self.hive, T0)
        self.assertEqual(p2.name, "2026-09-17-2.md")

    def test_write_dir_from_profile(self):
        p = write_lookback(self.hive, T0, profile={"lookback": {"write_dir": "reports/"}})
        self.assertEqual(p.parent, self.hive / "reports")

    # --- write_dir containment (item 9) ---

    def test_write_dir_escape_rejected(self):
        with self.assertRaises(ValueError):
            write_lookback(
                self.hive, T0, profile={"lookback": {"write_dir": "../escape/"}}
            )
        self.assertFalse((self.hive.parent / "escape").exists())

    def test_write_dir_deep_escape_rejected(self):
        with self.assertRaises(ValueError):
            write_lookback(
                self.hive, T0, profile={"lookback": {"write_dir": "a/../../escape/"}}
            )

    def test_write_dir_absolute_rejected(self):
        target = Path(self.tmp.name) / "abs-escape"
        with self.assertRaises(ValueError):
            write_lookback(
                self.hive, T0, profile={"lookback": {"write_dir": str(target)}}
            )
        self.assertFalse(target.exists())

    def test_write_dir_absolute_inside_hive_still_rejected(self):
        with self.assertRaises(ValueError):
            write_lookback(
                self.hive,
                T0,
                profile={"lookback": {"write_dir": str(self.hive / "inside")}},
            )

    def test_write_dir_nested_relative_allowed(self):
        p = write_lookback(
            self.hive, T0, profile={"lookback": {"write_dir": "reports/daily/"}}
        )
        self.assertEqual(p.parent, self.hive / "reports" / "daily")

    def test_write_dir_dot_segments_inside_hive_allowed(self):
        p = write_lookback(
            self.hive, T0, profile={"lookback": {"write_dir": "a/../reports/"}}
        )
        self.assertEqual(p.parent, self.hive / "reports")

    # --- min_messages_before_run (item 10) ---

    def test_short_report_keeps_all_headings(self):
        write_message(
            self.hive, agent="alice", harness="claude-code", type="ops",
            to="*", body={"text": "ready"}, now=T0,
        )
        path = write_lookback(
            self.hive, T0, profile={"lookback": {"min_messages_before_run": 20}}
        )
        text = path.read_text(encoding="utf-8")
        for h in HEADINGS:
            self.assertIn(h, text)
        self.assertIn("Not enough traffic: 1 messages.jsonl lines (min 20).", text)

    def test_enough_traffic_has_no_warning(self):
        path = write_lookback(
            self.hive, T0, profile={"lookback": {"min_messages_before_run": 0}}
        )
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("Not enough traffic", text)
        for h in HEADINGS:
            self.assertIn(h, text)

    # --- double claims = audit drift (item 10) ---

    def test_double_claims_detected_from_audit_drift(self):
        _reg2(self.hive)
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        # bob's claim line lands in the audit with no tombstone in between:
        # exactly the drift section 10 names.
        append_jsonl(
            self.hive / "store" / "claims.jsonl",
            {
                "claim_id": "clm_bob",
                "ts": "2026-09-17T09:02:00Z",
                "action": "claim",
                "task_id": t["id"],
                "agent": "bob",
                "harness": "codex",
                "expires_at": "2026-09-17T09:17:00Z",
                "result_ref": None,
                "note": None,
            },
        )
        text = write_lookback(self.hive, T0).read_text(encoding="utf-8")
        body = text.split("## Double claims", 1)[1].split("##", 1)[0]
        self.assertIn(t["id"], body)
        self.assertIn("alice", body)
        self.assertIn("bob", body)
        self.assertIn(
            "tombstone the prior claim before another agent claims the same task",
            text,
        )

    def test_no_double_claims_when_tombstoned_between(self):
        _reg2(self.hive)
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        release(self.hive, t["id"], "alice", T0)
        try_claim(self.hive, t["id"], "bob", "codex", T0, 900)
        text = write_lookback(self.hive, T0).read_text(encoding="utf-8")
        body = text.split("## Double claims", 1)[1].split("##", 1)[0]
        self.assertIn("None.", body)

    def test_no_double_claims_for_single_holder(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        text = write_lookback(self.hive, T0).read_text(encoding="utf-8")
        body = text.split("## Double claims", 1)[1].split("##", 1)[0]
        self.assertIn("None.", body)

if __name__ == "__main__":
    unittest.main()
