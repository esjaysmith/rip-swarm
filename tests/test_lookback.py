# tests/test_lookback.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import try_claim
from rip_swarm.inbox import create_task
from rip_swarm.lookback import write_lookback
from rip_swarm.timeutil import add_seconds

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)
HEADINGS = [
    "# Lookback 2026-09-17",
    "## Double claims",
    "## Expired leases",
    "## CURRENT vs last promote",
    "## Inbox with no claim",
    "## JSONL parse errors",
    "## Suggested PROTOCOL/profile diffs",
]

class TestLookback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
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

if __name__ == "__main__":
    unittest.main()
