import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
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

    def test_cannot_write_other_outbox(self):
        doc = write_message(
            self.hive, agent="bob", harness="codex", type="ops",
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

if __name__ == "__main__":
    unittest.main()
