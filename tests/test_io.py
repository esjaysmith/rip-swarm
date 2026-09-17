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

    def test_failed_serialization_leaves_no_tmp_and_next_write_works(self):
        p = self.dir / "orchestrator" / "CURRENT.json"

        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            atomic_write_json(p, {"bad": Unserializable()})
        self.assertFalse(p.exists())
        self.assertEqual(list(self.dir.rglob(".*.tmp")), [])
        atomic_write_json(p, {"agent": "a"})
        self.assertEqual(read_json(p)["agent"], "a")
        self.assertEqual(list(self.dir.rglob(".*.tmp")), [])

    def test_failed_write_over_existing_keeps_previous(self):
        p = self.dir / "o" / "CURRENT.json"
        atomic_write_json(p, {"agent": "a"})
        with self.assertRaises(TypeError):
            atomic_write_json(p, {"bad": object()})
        self.assertEqual(read_json(p)["agent"], "a")
        self.assertEqual(list(p.parent.glob(".*.tmp")), [])

    def test_tmp_names_are_unique_per_call(self):
        seen = set()
        p = self.dir / "n" / "x.json"
        real = os.replace
        try:
            def spy(src, dst):
                seen.add(Path(src).name)
                real(src, dst)

            os.replace = spy
            for _ in range(5):
                atomic_write_json(p, {"i": 1})
        finally:
            os.replace = real
        self.assertEqual(len(seen), 5)


if __name__ == "__main__":
    unittest.main()
