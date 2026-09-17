# tests/test_profile.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied
from rip_swarm.inbox import create_task
from rip_swarm.orchestrator import promote
from rip_swarm.policy import try_claim_with_policy
from rip_swarm.profile import DEFAULT_PROFILE, deep_merge, load_profile
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

    def _write_site(self, name, text):
        (self.hive / "profiles" / f"{name}.yaml").write_text(text, encoding="utf-8")

    def test_list_replace(self):
        site = {"slash": {"enabled": ["lookback"]}}
        merged = deep_merge(load_profile(self.hive, "default"), site)
        self.assertEqual(merged["slash"]["enabled"], ["lookback"])

    def test_duration(self):
        prof = load_profile(self.hive, "default")
        self.assertEqual(parse_duration(prof["orchestrator_lease_ttl"]), 1800)
        self.assertEqual(parse_duration(prof["worker_lease_ttl"]), 900)

    # --- fallback to DEFAULT_PROFILE (item 4) ---

    def test_missing_site_profile_by_arg_falls_back(self):
        prof = load_profile(self.hive, "does-not-exist")
        self.assertEqual(prof["worker_lease_ttl"], "15m")
        self.assertEqual(prof["budget"]["max_claims_open_per_agent"], 1)
        self.assertIs(prof["allow_self_promote"], False)

    def test_missing_site_profile_by_env_falls_back(self):
        prof = load_profile(self.hive, None, env={"RIP_SWARM_PROFILE": "does-not-exist"})
        self.assertEqual(prof["worker_lease_ttl"], "15m")
        self.assertEqual(prof["budget"]["max_claims_open_per_agent"], 1)

    def test_missing_default_yaml_still_yields_defaults(self):
        (self.hive / "profiles" / "default.yaml").unlink()
        prof = load_profile(self.hive, None)
        self.assertEqual(prof, DEFAULT_PROFILE)
        self.assertIsNot(prof, DEFAULT_PROFILE)

    def test_no_profiles_dir_at_all(self):
        for child in (self.hive / "profiles").iterdir():
            child.unlink()
        (self.hive / "profiles").rmdir()
        prof = load_profile(self.hive, "site")
        self.assertEqual(prof["orchestrator_lease_ttl"], "30m")

    def test_partial_default_yaml_keeps_builtin_keys(self):
        (self.hive / "profiles" / "default.yaml").write_text("name: partial\n", encoding="utf-8")
        prof = load_profile(self.hive, None)
        self.assertEqual(prof["name"], "partial")
        self.assertEqual(prof["worker_lease_ttl"], "15m")
        self.assertEqual(prof["budget"]["max_claims_open_per_agent"], 1)
        self.assertIs(prof["budget"]["spend_requires_operator"], True)
        self.assertEqual(prof["lookback"]["write_dir"], "lookback/")

    def test_site_overrides_default(self):
        self._write_site(
            "site",
            "name: site\nworker_lease_ttl: 45s\nbudget:\n  max_claims_open_per_agent: 3\n",
        )
        prof = load_profile(self.hive, "site")
        self.assertEqual(prof["name"], "site")
        self.assertEqual(prof["worker_lease_ttl"], "45s")
        self.assertEqual(prof["budget"]["max_claims_open_per_agent"], 3)
        # untouched keys survive from default.yaml / DEFAULT_PROFILE
        self.assertIs(prof["budget"]["spend_requires_operator"], True)
        self.assertEqual(prof["orchestrator_lease_ttl"], "30m")

    def test_site_partial_over_partial_default(self):
        (self.hive / "profiles" / "default.yaml").write_text("name: partial\n", encoding="utf-8")
        self._write_site("site", "allow_self_promote: true\n")
        prof = load_profile(self.hive, "site")
        self.assertIs(prof["allow_self_promote"], True)
        self.assertEqual(prof["worker_lease_ttl"], "15m")

    # --- profile resolution order (item 10) ---

    def test_env_profile_used_when_no_arg(self):
        self._write_site("site", "name: site\nworker_lease_ttl: 45s\n")
        prof = load_profile(self.hive, None, env={"RIP_SWARM_PROFILE": "site"})
        self.assertEqual(prof["name"], "site")
        self.assertEqual(prof["worker_lease_ttl"], "45s")

    def test_explicit_arg_beats_env(self):
        self._write_site("site", "name: site\nworker_lease_ttl: 45s\n")
        self._write_site("other", "name: other\nworker_lease_ttl: 90s\n")
        prof = load_profile(self.hive, "other", env={"RIP_SWARM_PROFILE": "site"})
        self.assertEqual(prof["name"], "other")
        self.assertEqual(prof["worker_lease_ttl"], "90s")

    def test_empty_env_profile_means_default(self):
        prof = load_profile(self.hive, None, env={"RIP_SWARM_PROFILE": ""})
        self.assertEqual(prof["name"], "default")

    def test_no_env_means_default(self):
        prof = load_profile(self.hive, None, env={})
        self.assertEqual(prof["name"], "default")

    def test_profile_with_spec_inline_comments(self):
        self._write_site(
            "commented",
            "name: commented\n"
            "allow_self_promote: false  # note\n"
            "budget:\n"
            "  max_claims_open_per_agent: 1  # x\n",
        )
        prof = load_profile(self.hive, "commented")
        self.assertIs(prof["allow_self_promote"], False)
        self.assertEqual(prof["budget"]["max_claims_open_per_agent"], 1)

    # --- baton vs cap (item 5) ---

    def test_baton_does_not_consume_cap(self):
        t1 = create_task(self.hive, title="one", created_by="op", now=T0)
        profile = load_profile(self.hive, "default")
        promote(
            self.hive,
            agent="alice",
            harness="claude-code",
            now=T0,
            lease_seconds=parse_duration(profile["orchestrator_lease_ttl"]),
            reason="operator designated",
            allow_self_promote=True,
            operators=[],
        )
        doc = try_claim_with_policy(
            self.hive, task_id=t1["id"], agent="alice", harness="claude-code",
            now=T0, profile=profile,
        )
        self.assertEqual(doc["task_id"], t1["id"])
        self.assertEqual(doc["expires_at"], "2026-09-17T09:16:00Z")

    def test_claim_refuses_orchestrator_task_id(self):
        profile = load_profile(self.hive, "default")
        with self.assertRaises(ClaimDenied) as ctx:
            try_claim_with_policy(
                self.hive, task_id="orchestrator", agent="alice", harness="claude-code",
                now=T0, profile=profile,
            )
        self.assertIn("promote", str(ctx.exception))
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())

    def test_orchestrator_refusal_writes_nothing(self):
        profile = load_profile(self.hive, "default")
        with self.assertRaises(ClaimDenied):
            try_claim_with_policy(
                self.hive, task_id="orchestrator", agent="alice", harness="claude-code",
                now=T0, profile=profile,
            )
        self.assertFalse((self.hive / "store" / "claims.jsonl").exists())
        self.assertFalse((self.hive / "store" / "messages.jsonl").exists())

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
