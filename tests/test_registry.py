import tempfile
import unittest
from pathlib import Path
from rip_swarm.registry import RegistryError, UnknownAgent, load_registry, require_agent

class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        p = self.hive / "agents" / "registry.yaml"
        p.parent.mkdir(parents=True)
        p.write_text("- id: alice\n  harness: claude-code\n  role: worker\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_require_known(self):
        self.assertEqual(require_agent(self.hive, "alice")["role"], "worker")
        self.assertEqual(load_registry(self.hive)[0]["id"], "alice")

    def test_require_unknown(self):
        with self.assertRaises(UnknownAgent):
            require_agent(self.hive, "mallory")

    def test_reserved_and_bad_ids_rejected(self):
        p = self.hive / "agents" / "registry.yaml"
        for bad in ("orchestrator", "*", "Bad Name"):
            p.write_text(f"- id: {bad}\n  harness: x\n  role: worker\n", encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_registry(self.hive)

    def test_quoted_reserved_id_rejected(self):
        p = self.hive / "agents" / "registry.yaml"
        p.write_text('- id: "*"\n  harness: x\n  role: worker\n', encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            load_registry(self.hive)
        self.assertIn("*", str(ctx.exception))

    def test_quoted_orchestrator_id_rejected(self):
        p = self.hive / "agents" / "registry.yaml"
        p.write_text('- id: "orchestrator"\n  harness: x\n  role: worker\n', encoding="utf-8")
        with self.assertRaises(RegistryError) as ctx:
            load_registry(self.hive)
        self.assertIn("orchestrator", str(ctx.exception))

    def test_duplicate_ids_rejected(self):
        p = self.hive / "agents" / "registry.yaml"
        p.write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: alice\n  harness: codex\n  role: observer\n",
            encoding="utf-8",
        )
        with self.assertRaises(RegistryError) as ctx:
            load_registry(self.hive)
        self.assertIn("alice", str(ctx.exception))
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_distinct_ids_accepted(self):
        p = self.hive / "agents" / "registry.yaml"
        p.write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: bob\n  harness: codex\n  role: observer\n",
            encoding="utf-8",
        )
        self.assertEqual([a["id"] for a in load_registry(self.hive)], ["alice", "bob"])

    def test_registry_with_comments(self):
        p = self.hive / "agents" / "registry.yaml"
        p.write_text(
            "# roster\n- id: alice  # the worker\n  harness: claude-code\n  role: worker\n",
            encoding="utf-8",
        )
        self.assertEqual(require_agent(self.hive, "alice")["harness"], "claude-code")

    def test_missing_registry_is_empty(self):
        (self.hive / "agents" / "registry.yaml").unlink()
        self.assertEqual(load_registry(self.hive), [])

if __name__ == "__main__":
    unittest.main()
