# tests/test_member_race.py — member ids race through publish (spec §3.2)
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rip_swarm.gitops as g
from hivekit import T0, clone_hive, git, local_hive, remote_files, seed_remote_hive
from rip_swarm.gitops import GitopsError, MemberTaken, publish, publish_or_apply, register_member
from rip_swarm.members import create_member


class TestMemberRace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        seed_remote_hive(self.root, self.origin)
        self.ha = clone_hive(self.root, self.origin, "a")
        self.hb = clone_hive(self.root, self.origin, "b")

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_member_publishes(self):
        doc = register_member(self.ha, harness="claude-code", now=T0)
        self.assertEqual(doc["id"], "claude-1")
        self.assertIn("agents/claude-1/member.json", remote_files(self.origin))
        self.assertEqual(git(self.ha, "status", "--porcelain"), "")

    def test_race_retries_with_the_next_number(self):
        real = g.create_member
        fired = {"done": False}

        def racing(hive, **kw):
            if not fired["done"]:
                fired["done"] = True
                # A's join lands claude-1 on the remote while B is mid-op.
                register_member(self.ha, harness="claude-code", now=T0)
            return real(hive, **kw)

        with mock.patch.object(g, "create_member", side_effect=racing):
            doc = register_member(self.hb, harness="claude-code", now=T0)
        self.assertEqual(doc["id"], "claude-2")
        files = remote_files(self.origin)
        self.assertIn("agents/claude-1/member.json", files)
        self.assertIn("agents/claude-2/member.json", files)
        self.assertEqual(git(self.hb, "status", "--porcelain"), "")

    def test_same_second_same_harness_bodies_differ(self):
        # Review focus 1: identical bodies would merge silently in git.
        with tempfile.TemporaryDirectory() as tmp:
            one = local_hive(Path(tmp) / "one")
            two = local_hive(Path(tmp) / "two")
            a = create_member(one, agent_id="claude-1", harness="claude-code", now=T0)
            b = create_member(two, agent_id="claude-1", harness="claude-code", now=T0)
        self.assertNotEqual(a["session"], b["session"])

    def test_race_exhausted_is_retry_join(self):
        register_member(self.ha, harness="claude-code", now=T0)
        with mock.patch.object(g, "next_member_id", return_value="claude-1"):
            with self.assertRaises(GitopsError) as ctx:
                register_member(self.hb, harness="claude-code", now=T0)
        self.assertIn("retry join", str(ctx.exception))
        self.assertEqual(git(self.hb, "status", "--porcelain"), "")

    def test_other_conflicts_stay_gitops_errors(self):
        def write(hive, text):
            (hive / "notes").mkdir(exist_ok=True)
            (hive / "notes" / "x.json").write_text(text, encoding="utf-8")
            return {}

        def b_op():
            publish(self.ha, task_id="__none__", op=lambda: write(self.ha, '{"a": 1}\n'),
                    message="a", agent=None, now=T0, allow=["notes/*.json"])
            return write(self.hb, '{"b": 2}\n')

        with self.assertRaises(GitopsError) as ctx:
            publish(self.hb, task_id="__none__", op=b_op, message="b", agent=None, now=T0,
                    allow=["notes/*.json"], contested="agents/*/member.json")
        self.assertNotIsInstance(ctx.exception, MemberTaken)
        self.assertEqual(git(self.hb, "status", "--porcelain"), "")

    def test_publish_or_apply_without_upstream_just_applies(self):
        hive = local_hive(self.root / "plain")
        doc = publish_or_apply(
            hive, task_id="__none__",
            op=lambda: create_member(hive, agent_id="grok-1", harness="grok", now=T0),
            message="join", agent=None, now=T0, allow=["agents/*/member.json"],
        )
        self.assertEqual(doc["id"], "grok-1")
        self.assertTrue((hive / "agents" / "grok-1" / "member.json").is_file())

    def test_publish_or_apply_returns_doc_when_nothing_changed(self):
        doc = publish_or_apply(self.ha, task_id="__none__", op=lambda: {"already": True},
                               message="noop", agent=None, now=T0, allow=[])
        self.assertEqual(doc, {"already": True})


if __name__ == "__main__":
    unittest.main()
