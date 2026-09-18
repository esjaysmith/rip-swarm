import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied
from rip_swarm.outbox import write_message
from rip_swarm.registry import UnknownAgent

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

def _reg(hive, *ids):
    lines = [f"- id: {i}\n  harness: claude-code\n  role: worker\n" for i in ids]
    p = hive / "agents" / "registry.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(lines), encoding="utf-8")

class TestOutbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        _reg(self.hive, "alice", "bob")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_file_and_jsonl(self):
        doc = write_message(
            self.hive, agent="alice", harness="claude-code", type="ops",
            topic="ops", to="orchestrator", body={"text": "ready"}, now=T0,
        )
        path = self.hive / "agents" / "alice" / "outbox" / f"{doc['id']}.json"
        self.assertTrue(path.is_file())
        lines = (self.hive / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["id"], doc["id"])

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="nope",
                topic="ops", to="*", body={}, now=T0,
            )

    def test_unknown_agent(self):
        with self.assertRaises(UnknownAgent):
            write_message(
                self.hive, agent="mallory", harness="claude-code", type="ops",
                topic="ops", to="*", body={"text": "x"}, now=T0,
            )

    def test_harness_mismatch_refused_before_any_write(self):
        # Same contract as claim._require_registered: registry harness is SoT.
        with self.assertRaises(ClaimDenied) as ctx:
            write_message(
                self.hive, agent="alice", harness="WRONG", type="ops",
                topic="ops", to="*", body={"text": "x"}, now=T0,
            )
        msg = str(ctx.exception)
        self.assertIn("claude-code", msg)
        self.assertIn("WRONG", msg)
        self.assertFalse((self.hive / "store" / "messages.jsonl").exists())
        self.assertFalse((self.hive / "agents" / "alice" / "outbox").exists())

    def test_cannot_write_other_outbox(self):
        doc = write_message(
            self.hive, agent="bob", harness="claude-code", type="ops",
            topic="ops", to="*", body={"text": "x"}, now=T0,
        )
        self.assertFalse((self.hive / "agents" / "alice" / "outbox" / f"{doc['id']}.json").exists())
        self.assertTrue((self.hive / "agents" / "bob" / "outbox" / f"{doc['id']}.json").exists())

    def test_default_topic_and_bad_to(self):
        doc = write_message(
            self.hive, agent="alice", harness="claude-code", type="result",
            to="orchestrator", body={"path": "x"}, now=T0,
        )
        self.assertEqual(doc["topic"], "results")
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="ops",
                to="nobody", body={}, now=T0,
            )

    # --- body-shape enforcement (spec section 6) ---

    def _assert_no_writes(self):
        self.assertFalse((self.hive / "store" / "messages.jsonl").exists())
        self.assertFalse((self.hive / "agents" / "alice" / "outbox").exists())

    def test_budget_block_exact_body_accepted(self):
        doc = write_message(
            self.hive, agent="alice", harness="claude-code", type="budget_block",
            to="orchestrator",
            body={"agent": "alice", "rule": "max_claims_open_per_agent",
                  "limit": 1, "observed": 1},
            now=T0,
        )
        self.assertEqual(doc["topic"], "ops")
        self.assertEqual(
            set(doc["body"]), {"agent", "rule", "limit", "observed"}
        )

    def test_budget_block_extra_key_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="budget_block",
                to="orchestrator",
                body={"agent": "alice", "rule": "r", "limit": 1, "observed": 1,
                      "extra": "nope"},
                now=T0,
            )
        self._assert_no_writes()

    def test_budget_block_missing_key_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="budget_block",
                to="orchestrator",
                body={"agent": "alice", "rule": "r", "limit": 1},
                now=T0,
            )
        self._assert_no_writes()

    def test_budget_block_renamed_key_refused(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="budget_block",
                to="orchestrator",
                body={"agent": "alice", "rule": "r", "limit": 1, "seen": 1},
                now=T0,
            )
        self._assert_no_writes()

    def test_promote_exact_body_accepted(self):
        doc = write_message(
            self.hive, agent="alice", harness="claude-code", type="promote", to="*",
            body={"agent": "alice", "harness": "claude-code", "by": "alice",
                  "reason": "designated", "claim_id": "clm_x"},
            now=T0,
        )
        self.assertEqual(
            set(doc["body"]), {"agent", "harness", "by", "reason", "claim_id"}
        )

    def test_promote_extra_key_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="promote", to="*",
                body={"agent": "alice", "harness": "claude-code", "by": "alice",
                      "reason": "r", "claim_id": "clm_x", "extra": 1},
                now=T0,
            )
        self._assert_no_writes()

    def test_promote_missing_key_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="promote", to="*",
                body={"agent": "alice", "harness": "claude-code", "by": "alice",
                      "reason": "r"},
                now=T0,
            )
        self._assert_no_writes()

    def test_non_shaped_types_accept_free_bodies(self):
        for msg_type in ("ops", "note", "heartbeat"):
            write_message(
                self.hive, agent="alice", harness="claude-code", type=msg_type,
                to="*", body={"anything": [1, 2]}, now=T0,
            )
        lines = (self.hive / "store" / "messages.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 3)

    def test_non_dict_body_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="ops",
                to="*", body=["not", "a", "dict"], now=T0,
            )
        self._assert_no_writes()

    def test_bad_topic_refused_before_any_write(self):
        with self.assertRaises(ValueError):
            write_message(
                self.hive, agent="alice", harness="claude-code", type="ops",
                topic="nope", to="*", body={"text": "x"}, now=T0,
            )
        self._assert_no_writes()

if __name__ == "__main__":
    unittest.main()
