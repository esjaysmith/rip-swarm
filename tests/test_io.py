import os
import tempfile
import unittest
from pathlib import Path
from rip_swarm.io import ExclExistsError, atomic_write_json, excl_create_json, read_json


class TestIo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_excl_create_then_reject_second(self):
        p = self.dir / "claims" / "task_x.json"
        excl_create_json(p, {"task_id": "task_x"})
        self.assertEqual(read_json(p)["task_id"], "task_x")
        with self.assertRaises(ExclExistsError):
            excl_create_json(p, {"task_id": "other"})

    def test_atomic_write_replaces(self):
        p = self.dir / "orchestrator" / "CURRENT.json"
        atomic_write_json(p, {"agent": "a"})
        atomic_write_json(p, {"agent": "b"})
        self.assertEqual(read_json(p)["agent"], "b")
        leftovers = list(p.parent.glob(".*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
