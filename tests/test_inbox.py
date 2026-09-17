# tests/test_inbox.py
import tempfile
import unittest
from pathlib import Path
from rip_swarm.inbox import InboxError, create_task, read_task
from rip_swarm.io import ExclExistsError


class TestInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_read(self):
        doc = create_task(self.hive, title="Do the thing", created_by="op")
        self.assertTrue(doc["id"].startswith("task_"))
        self.assertEqual(doc["title"], "Do the thing")
        self.assertTrue(doc["created_at"].endswith("Z"))
        on_disk = self.hive / "inbox" / f"{doc['id']}.json"
        self.assertTrue(on_disk.is_file())
        self.assertEqual(read_task(self.hive, doc["id"])["title"], "Do the thing")

    def test_duplicate_id_fails(self):
        doc = create_task(self.hive, title="A", created_by="op")
        with self.assertRaises(ExclExistsError):
            create_task(self.hive, title="B", created_by="op", task_id=doc["id"])

    def test_empty_title_rejected(self):
        with self.assertRaises(InboxError):
            create_task(self.hive, title="  ", created_by="op")

    def test_explicit_valid_task_id_accepted(self):
        tid = "task_01J00000000000000000000000"
        doc = create_task(self.hive, title="A", created_by="op", task_id=tid)
        self.assertEqual(doc["id"], tid)
        self.assertTrue((self.hive / "inbox" / f"{tid}.json").is_file())

    def test_bad_task_ids_rejected(self):
        bad = [
            "",
            "   ",
            "nope",
            "task_",
            "task_01J0000000000000000000000",      # 25 chars
            "task_01J000000000000000000000000",    # 27 chars
            "task_01j00000000000000000000000",     # lowercase
            "task_01I00000000000000000000000",     # I not in Crockford
            "task_01L00000000000000000000000",     # L not in Crockford
            "task_01O00000000000000000000000",     # O not in Crockford
            "task_01U00000000000000000000000",     # U not in Crockford
            "task_01J00000000000000000000000\n",
            "../task_01J00000000000000000000000",
            "task_01J00000000000000000000000/x",
            "orchestrator",
            "CURRENT",
        ]
        for tid in bad:
            with self.subTest(tid=tid):
                with self.assertRaises(InboxError):
                    create_task(self.hive, title="A", created_by="op", task_id=tid)
        self.assertFalse(any((self.hive / "inbox").glob("*")) if (self.hive / "inbox").is_dir() else False)


if __name__ == "__main__":
    unittest.main()
