# tests/test_orchestrator.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied
from rip_swarm.io import atomic_write_json, read_json
from rip_swarm.orchestrator import current_matches_claim, promote, read_current, release_orchestrator
from rip_swarm.timeutil import add_seconds

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

def _reg(hive):
    p = hive / "agents" / "registry.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(
        "- id: alice\n  harness: claude-code\n  role: worker\n"
        "- id: bob\n  harness: codex\n  role: worker\n",
        encoding="utf-8",
    )

class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        _reg(self.hive)

    def tearDown(self):
        self.tmp.cleanup()

    def test_promote_writes_claim_and_current(self):
        cur = promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="operator designated",
            allow_self_promote=True, operators=[],
        )
        claim = read_json(self.hive / "claims" / "orchestrator.json")
        self.assertEqual(cur["claim_id"], claim["claim_id"])
        self.assertEqual(read_current(self.hive)["agent"], "alice")
        self.assertTrue(current_matches_claim(self.hive, T0))

    def test_second_promoter_denied(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        with self.assertRaises(ClaimDenied):
            promote(
                self.hive, agent="bob", harness="codex", now=T0,
                lease_seconds=1800, reason="b", allow_self_promote=True, operators=[],
            )

    def test_expired_steal(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        later = add_seconds(T0, 1801)
        cur = promote(
            self.hive, agent="bob", harness="codex", now=later,
            lease_seconds=1800, reason="reclaim", allow_self_promote=True, operators=[],
        )
        self.assertEqual(cur["agent"], "bob")

    def test_mismatch(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        atomic_write_json(
            self.hive / "orchestrator" / "CURRENT.json",
            {"agent": "bob", "harness": "codex", "lease_expires_at": "2026-09-17T09:31:00Z",
             "reason": "tamper", "claim_id": "clm_nope"},
        )
        self.assertFalse(current_matches_claim(self.hive, T0))

    def test_operators_without_self_promote(self):
        with self.assertRaises(ClaimDenied):
            promote(
                self.hive, agent="alice", harness="claude-code", now=T0,
                lease_seconds=1800, reason="a", allow_self_promote=False, operators=[],
            )
        cur = promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=False, operators=["alice"],
        )
        self.assertEqual(cur["agent"], "alice")

    def test_operator_promotes_someone_else(self):
        with self.assertRaises(ClaimDenied):
            promote(
                self.hive, agent="alice", harness="claude-code", now=T0, by="bob",
                lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
            )
        cur = promote(
            self.hive, agent="alice", harness="claude-code", now=T0, by="bob",
            lease_seconds=1800, reason="designated", allow_self_promote=False, operators=["bob"],
        )
        self.assertEqual(cur["agent"], "alice")
        last = (self.hive / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn('"by": "bob"', last)

    def test_release_clears_current(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        with self.assertRaises(ClaimDenied):
            release_orchestrator(self.hive, agent="bob", now=T0)
        release_orchestrator(self.hive, agent="alice", now=T0)
        self.assertFalse((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())
        self.assertTrue(current_matches_claim(self.hive, T0))  # both absent = no orchestrator = healthy

if __name__ == "__main__":
    unittest.main()
