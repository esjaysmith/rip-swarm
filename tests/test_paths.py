import os
import tempfile
import unittest
from pathlib import Path
from rip_swarm.paths import HivePathError, HivePaths, resolve_hive


class TestPaths(unittest.TestCase):
    def test_resolve_explicit_wins(self):
        p = resolve_hive("/tmp/hive-explicit", env={"RIP_SWARM_HIVE": "/tmp/other"})
        self.assertEqual(p, Path("/tmp/hive-explicit").resolve())

    def test_resolve_env(self):
        p = resolve_hive(None, env={"RIP_SWARM_HIVE": "/tmp/from-env"})
        self.assertEqual(p, Path("/tmp/from-env").resolve())

    def test_resolve_default_swarm_dir(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_swarm").mkdir()
            self.assertEqual(resolve_hive(None, env={}, cwd=Path(d)), (Path(d) / "_swarm").resolve())

    def test_resolve_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(HivePathError):
                resolve_hive(None, env={}, cwd=Path(d))

    def test_layout(self):
        h = HivePaths(Path("/tmp/h"))
        self.assertEqual(h.claim("task_abc"), Path("/tmp/h/claims/task_abc.json"))
        self.assertEqual(h.inbox_task("task_abc"), Path("/tmp/h/inbox/task_abc.json"))
        self.assertEqual(h.outbox("alice"), Path("/tmp/h/agents/alice/outbox"))
        self.assertEqual(h.current, Path("/tmp/h/orchestrator/CURRENT.json"))


if __name__ == "__main__":
    unittest.main()
