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


if __name__ == "__main__":
    unittest.main()
