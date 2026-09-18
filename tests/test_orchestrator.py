# tests/test_orchestrator.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied, complete, reject, try_claim
from rip_swarm.registry import UnknownAgent
from rip_swarm.io import atomic_write_json, read_json
from rip_swarm.orchestrator import (
    current_matches_claim,
    heartbeat_orchestrator,
    orchestrator_state,
    promote,
    read_current,
    release_orchestrator,
)
from rip_swarm.timeutil import add_seconds, format_z

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

    def test_heartbeat_repairs_current_from_claim(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="operator designated",
            allow_self_promote=True, operators=[],
        )
        claim = read_json(self.hive / "claims" / "orchestrator.json")
        atomic_write_json(
            self.hive / "orchestrator" / "CURRENT.json",
            {
                "agent": "bob",
                "harness": "codex",
                "lease_expires_at": "2026-09-17T09:31:00Z",
                "reason": "keep-me",
                "claim_id": "clm_nope",
            },
        )
        repaired = heartbeat_orchestrator(
            self.hive, agent="alice", now=T0, lease_seconds=1800
        )
        self.assertEqual(repaired["agent"], "alice")
        self.assertEqual(repaired["harness"], "claude-code")
        self.assertEqual(repaired["claim_id"], claim["claim_id"])
        # reason is repaired from the claim file (SoT), never carried over from
        # the tampered CURRENT.json mirror.
        self.assertEqual(claim["note"], "operator designated")
        self.assertEqual(repaired["reason"], "operator designated")
        self.assertNotEqual(repaired["reason"], "keep-me")
        self.assertEqual(repaired["lease_expires_at"], format_z(add_seconds(T0, 1800)))
        self.assertEqual(read_current(self.hive)["agent"], "alice")
        self.assertEqual(read_current(self.hive)["claim_id"], claim["claim_id"])

    def test_complete_reject_orchestrator_leave_current(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        with self.assertRaises(ClaimDenied):
            complete(self.hive, "orchestrator", "alice", T0, result_ref="x")
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertTrue((self.hive / "claims" / "orchestrator.json").exists())
        with self.assertRaises(ClaimDenied):
            reject(self.hive, "orchestrator", "alice", T0)
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertTrue((self.hive / "claims" / "orchestrator.json").exists())

    def test_repromote_same_agent_updates_claim_note(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="first", allow_self_promote=True, operators=[],
        )
        cur = promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="second", allow_self_promote=True, operators=[],
        )
        self.assertEqual(cur["reason"], "second")
        claim = read_json(self.hive / "claims" / "orchestrator.json")
        self.assertEqual(claim["note"], "second")
        # CURRENT and the claim stay in lockstep across a heartbeat.
        repaired = heartbeat_orchestrator(
            self.hive, agent="alice", now=T0, lease_seconds=1800
        )
        self.assertEqual(repaired["reason"], "second")
        self.assertTrue(current_matches_claim(self.hive, T0))

    # --- promote persists reason as the claim note (item 7) ---

    def test_promote_persists_reason_in_claim_note(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="operator designated",
            allow_self_promote=True, operators=[],
        )
        claim = read_json(self.hive / "claims" / "orchestrator.json")
        self.assertEqual(claim["note"], "operator designated")

    def test_heartbeat_repairs_reason_when_current_missing(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="operator designated",
            allow_self_promote=True, operators=[],
        )
        (self.hive / "orchestrator" / "CURRENT.json").unlink()
        repaired = heartbeat_orchestrator(
            self.hive, agent="alice", now=T0, lease_seconds=1800
        )
        self.assertEqual(repaired["reason"], "operator designated")
        self.assertEqual(read_current(self.hive)["reason"], "operator designated")
        self.assertTrue(current_matches_claim(self.hive, T0))

    def test_non_holder_heartbeat_denied(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        before = read_current(self.hive)
        with self.assertRaises(ClaimDenied):
            heartbeat_orchestrator(self.hive, agent="bob", now=T0, lease_seconds=1800)
        self.assertEqual(read_current(self.hive), before)
        self.assertEqual(
            read_json(self.hive / "claims" / "orchestrator.json")["agent"], "alice"
        )

    def test_heartbeat_without_any_baton_denied(self):
        with self.assertRaises(ClaimDenied):
            heartbeat_orchestrator(self.hive, agent="alice", now=T0, lease_seconds=1800)
        self.assertFalse((self.hive / "orchestrator" / "CURRENT.json").exists())

    # --- release after expiry (item 6) ---

    def test_holder_can_release_own_expired_baton(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        later = add_seconds(T0, 1801)
        doc = release_orchestrator(self.hive, agent="alice", now=later)
        self.assertEqual(doc["agent"], "alice")
        self.assertFalse((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())
        tombstones = sorted(
            p.name for p in (self.hive / "claims").glob("orchestrator.release.*.json")
        )
        self.assertEqual(len(tombstones), 1)
        rows = [
            json.loads(line)
            for line in (self.hive / "store" / "claims.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(rows[-1]["action"], "release")
        self.assertEqual(rows[-1]["agent"], "alice")
        self.assertTrue(current_matches_claim(self.hive, later))

    def test_non_holder_cannot_release_expired_baton(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        later = add_seconds(T0, 1801)
        with self.assertRaises(ClaimDenied):
            release_orchestrator(self.hive, agent="bob", now=later)
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertTrue((self.hive / "claims" / "orchestrator.json").exists())

    def test_release_expired_with_note(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        later = add_seconds(T0, 1801)
        doc = release_orchestrator(self.hive, agent="alice", now=later, note="done")
        self.assertEqual(doc["note"], "done")

    def test_release_without_any_claim_denied(self):
        with self.assertRaises(ClaimDenied):
            release_orchestrator(self.hive, agent="alice", now=T0)

    def test_release_expired_baton_by_unregistered_agent_denied(self):
        claim_path = self.hive / "claims" / "orchestrator.json"
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            claim_path,
            {
                "task_id": "orchestrator",
                "claim_id": "clm_mallory",
                "agent": "mallory",
                "harness": "claude-code",
                "exclusive": True,
                "created_at": "2026-09-17T09:01:00Z",
                "expires_at": "2026-09-17T09:31:00Z",
                "note": None,
            },
        )
        atomic_write_json(
            self.hive / "orchestrator" / "CURRENT.json",
            {
                "agent": "mallory",
                "harness": "claude-code",
                "lease_expires_at": "2026-09-17T09:31:00Z",
                "reason": None,
                "claim_id": "clm_mallory",
            },
        )
        later = add_seconds(T0, 1801)
        with self.assertRaises(UnknownAgent):
            release_orchestrator(self.hive, agent="mallory", now=later)
        self.assertTrue(claim_path.exists())
        self.assertEqual(read_json(claim_path)["agent"], "mallory")
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").exists())

    # --- orchestrator_state half-states (item 10) ---

    def test_state_current_present_claim_absent(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        (self.hive / "claims" / "orchestrator.json").unlink()
        state = orchestrator_state(self.hive, T0)
        self.assertFalse(state["matches_claim"])
        self.assertTrue(state["present"])
        self.assertEqual(state["agent"], "alice")

    def test_state_claim_present_current_absent(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        (self.hive / "orchestrator" / "CURRENT.json").unlink()
        state = orchestrator_state(self.hive, T0)
        self.assertFalse(state["matches_claim"])
        self.assertFalse(state["present"])
        self.assertEqual(state["agent"], "alice")

    def test_state_both_absent_is_healthy(self):
        state = orchestrator_state(self.hive, T0)
        self.assertTrue(state["matches_claim"])
        self.assertFalse(state["present"])
        self.assertIsNone(state["agent"])
        self.assertFalse(state["expired"])

    def test_state_expired_claim_is_mismatch(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        later = add_seconds(T0, 1801)
        state = orchestrator_state(self.hive, later)
        self.assertTrue(state["expired"])
        self.assertFalse(state["matches_claim"])

    # --- M3/m1: promote is the only way to take the baton --------------------

    def test_try_claim_cannot_take_the_baton(self):
        with self.assertRaises(ClaimDenied) as ctx:
            try_claim(self.hive, "orchestrator", "alice", "claude-code", T0, 1800)
        self.assertIn("promote", str(ctx.exception))
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())
        self.assertIsNone(read_current(self.hive))
        self.assertTrue(current_matches_claim(self.hive, T0))

    def test_promote_refuses_harness_not_in_registry(self):
        with self.assertRaises(ClaimDenied) as ctx:
            promote(
                self.hive, agent="alice", harness="totally-wrong", now=T0,
                lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
            )
        msg = str(ctx.exception)
        self.assertIn("claude-code", msg)
        self.assertIn("totally-wrong", msg)
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())
        self.assertIsNone(read_current(self.hive))

    def test_promote_harness_comes_from_registry(self):
        cur = promote(
            self.hive, agent="bob", harness="codex", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        self.assertEqual(cur["harness"], "codex")
        self.assertEqual(read_current(self.hive)["harness"], "codex")
        self.assertEqual(
            read_json(self.hive / "claims" / "orchestrator.json")["harness"], "codex"
        )

    def test_promote_refuses_unregistered_agent(self):
        with self.assertRaises(UnknownAgent):
            promote(
                self.hive, agent="ghost", harness="codex", now=T0,
                lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
            )
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())

if __name__ == "__main__":
    unittest.main()
