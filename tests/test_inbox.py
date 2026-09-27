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


from datetime import datetime, timezone

from rip_swarm.acceptance import master_reject
from rip_swarm.orchestrator import promote

T0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
FLOORS = {"spec": 2, "plan": 0, "implementation": 1}


class TestReviewFields(unittest.TestCase):
    """Execution proposals §5.2 and §5.5: the inbox-add rules."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        (self.hive / "agents").mkdir()
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: op\n  harness: human\n  role: operator\n"
            "- id: alice\n  harness: claude-code\n  role: worker\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, title="A", **kw):
        return create_task(self.hive, title=title, created_by="op", now=T0, floors=FLOORS, **kw)

    def test_kind_defaults_to_the_profile_number(self):
        doc = self.post(kind="spec")
        self.assertEqual((doc["kind"], doc["min_reviews"]), ("spec", 2))
        self.assertEqual(self.post(kind="plan")["min_reviews"], 0)

    def test_the_profile_is_a_floor(self):
        self.assertEqual(self.post(kind="spec", min_reviews=3)["min_reviews"], 3)
        with self.assertRaisesRegex(InboxError, r"min_reviews for spec is at least 2 \(profile\)"):
            self.post(kind="spec", min_reviews=1)

    def test_min_reviews_without_kind_is_allowed(self):
        self.assertEqual(self.post(min_reviews=1)["min_reviews"], 1)
        self.assertNotIn("kind", self.post(min_reviews=1))

    def test_unknown_kind_and_bad_numbers_are_refused(self):
        with self.assertRaisesRegex(InboxError, "unknown kind 'poem'"):
            self.post(kind="poem")
        for bad in (-1, "2", True, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(InboxError, "must be an integer >= 0"):
                    self.post(min_reviews=bad)

    def test_reviews_needs_a_reviewed_artifact(self):
        a = self.post(kind="spec")["id"]
        plain = self.post(title="plain")["id"]
        r = self.post(title="Review 1", reviews=a)
        self.assertEqual(r["reviews"], a)
        self.assertNotIn("min_reviews", r)
        with self.assertRaisesRegex(InboxError, f"{plain} is not a reviewed artifact"):
            self.post(reviews=plain)
        with self.assertRaisesRegex(InboxError, "unknown task"):
            self.post(reviews="task_01J00000000000000000000000")

    def test_reviews_combines_with_nothing_else(self):
        a = self.post(kind="spec")["id"]
        for extra in ({"fixes": a}, {"min_reviews": 1}, {"kind": "spec"}):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(InboxError, "--reviews cannot be combined"):
                    self.post(reviews=a, **extra)

    def test_a_replacement_may_copy_a_lower_number(self):
        promote(self.hive, agent="alice", harness="claude-code", now=T0, lease_seconds=1800,
                reason="m", allow_self_promote=False, operators=["op"], by="op")
        x = create_task(self.hive, title="Spec", created_by="op", now=T0,
                        floors={"spec": 1, "plan": 0}, kind="spec")["id"]     # a lighter profile
        title = f"Spec (replaces {x})"
        with self.assertRaisesRegex(InboxError, "at least 2"):
            self.post(title=title, kind="spec", min_reviews=1)            # X is not rejected
        master_reject(self.hive, agent="alice", task_id=x, note="drop", now=T0)
        doc = self.post(title=title, kind="spec", min_reviews=1)          # an exact copy
        self.assertEqual(doc["min_reviews"], 1)
        with self.assertRaisesRegex(InboxError, "at least 2"):
            self.post(title=title, kind="spec", min_reviews=0)            # not the same number
        with self.assertRaisesRegex(InboxError, "at least 1"):
            self.post(title=title, kind="implementation", min_reviews=0)  # not the same kind
        with self.assertRaisesRegex(InboxError, "at least 2"):
            self.post(title="Spec", kind="spec", min_reviews=1)           # not titled as a copy


if __name__ == "__main__":
    unittest.main()
