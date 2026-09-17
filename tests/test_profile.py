# tests/test_profile.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied
from rip_swarm.inbox import create_task
from rip_swarm.policy import try_claim_with_policy
from rip_swarm.profile import deep_merge, load_profile
from rip_swarm.timeutil import parse_duration

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

DEFAULT_YAML = """name: default
reviews_required_per_plan: 1
orchestrator_lease_ttl: 30m
worker_lease_ttl: 15m
allow_self_promote: false
allow_preempt: false
operators: []
budget:
  max_claims_open_per_agent: 1
  spend_requires_operator: true
lookback:
  min_messages_before_run: 20
  write_dir: lookback/
slash:
  enabled:
    - lookback
    - status
"""

class TestProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        (self.hive / "profiles").mkdir()
        (self.hive / "profiles" / "default.yaml").write_text(DEFAULT_YAML, encoding="utf-8")
        (self.hive / "agents").mkdir()
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_replace(self):
        site = {"slash": {"enabled": ["lookback"]}}
        merged = deep_merge(load_profile(self.hive, "default"), site)
        self.assertEqual(merged["slash"]["enabled"], ["lookback"])

    def test_duration(self):
        prof = load_profile(self.hive, "default")
        self.assertEqual(parse_duration(prof["orchestrator_lease_ttl"]), 1800)
        self.assertEqual(parse_duration(prof["worker_lease_ttl"]), 900)

    def test_baton_does_not_consume_cap(self):
        t1 = create_task(self.hive, title="one", created_by="op", now=T0)
        profile = load_profile(self.hive, "default")
        try_claim_with_policy(
            self.hive, task_id="orchestrator", agent="alice", harness="claude-code",
            now=T0, profile=profile,
        )
        doc = try_claim_with_policy(
            self.hive, task_id=t1["id"], agent="alice", harness="claude-code",
            now=T0, profile=profile,
        )
        self.assertEqual(doc["expires_at"], "2026-09-17T09:16:00Z")

    def test_cap_blocks_second_claim(self):
        t1 = create_task(self.hive, title="one", created_by="op", now=T0)
        t2 = create_task(self.hive, title="two", created_by="op", now=T0)
        profile = load_profile(self.hive, "default")
        try_claim_with_policy(
            self.hive, task_id=t1["id"], agent="alice", harness="claude-code",
            now=T0, profile=profile,
        )
        with self.assertRaises(ClaimDenied):
            try_claim_with_policy(
                self.hive, task_id=t2["id"], agent="alice", harness="claude-code",
                now=T0, profile=profile,
            )
        lines = (self.hive / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        body = json.loads(lines[-1])["body"]
        self.assertEqual(body["rule"], "max_claims_open_per_agent")
        self.assertEqual(body["limit"], 1)
        self.assertEqual(body["observed"], 1)

    def test_idempotent_same_task(self):
        t1 = create_task(self.hive, title="one", created_by="op", now=T0)
        profile = load_profile(self.hive, "default")
        a = try_claim_with_policy(
            self.hive, task_id=t1["id"], agent="alice", harness="claude-code",
            now=T0, profile=profile,
        )
        b = try_claim_with_policy(
            self.hive, task_id=t1["id"], agent="alice", harness="claude-code",
            now=T0, profile=profile,
        )
        self.assertEqual(a["claim_id"], b["claim_id"])
        path = self.hive / "store" / "messages.jsonl"
        if path.exists():
            self.assertNotIn("budget_block", path.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
