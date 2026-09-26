# tests/test_members.py — member files and the merged registry (spec §3)
import tempfile
import unittest
from pathlib import Path

from hivekit import T0, local_hive
from rip_swarm.members import (
    MemberExists,
    create_member,
    has_left,
    list_members,
    member_ids,
    next_member_id,
    short_prefix,
    write_left,
)
from rip_swarm.registry import UnknownAgent, known_agents, load_registry, require_agent


class TestMembers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_short_prefix(self):
        self.assertEqual(short_prefix("claude-code"), "claude")
        self.assertEqual(short_prefix("grok"), "grok")
        self.assertEqual(short_prefix("Grok"), "grok")
        self.assertEqual(short_prefix("my.tool-x"), "mytool")
        self.assertEqual(short_prefix("_under-x"), "under")
        self.assertEqual(short_prefix("op"), "agent")
        self.assertEqual(short_prefix("--"), "agent")
        self.assertEqual(short_prefix(""), "agent")

    def test_member_registers_and_left_unregisters(self):
        doc = create_member(self.hive, agent_id="claude-1", harness="claude-code", now=T0)
        self.assertEqual(doc["role"], "worker")
        self.assertEqual(doc["joined_at"], "2026-09-26T10:00:00Z")
        self.assertRegex(doc["session"], r"^[0-9A-Z]{26}$")
        self.assertEqual(require_agent(self.hive, "claude-1")["harness"], "claude-code")
        write_left(self.hive, "claude-1", T0)
        self.assertTrue(has_left(self.hive, "claude-1"))
        self.assertTrue((self.hive / "agents" / "claude-1" / "member.json").is_file())
        self.assertTrue(
            (self.hive / "agents" / "claude-1" / "member.left.20260926T100000Z.json").is_file()
        )
        with self.assertRaises(UnknownAgent):
            require_agent(self.hive, "claude-1")
        self.assertIn("claude-1", known_agents(self.hive))
        self.assertIn("op", known_agents(self.hive))

    def test_two_left_tombstones_in_one_second_do_not_collide(self):
        create_member(self.hive, agent_id="claude-1", harness="claude-code", now=T0)
        write_left(self.hive, "claude-1", T0)
        write_left(self.hive, "claude-1", T0)
        names = sorted(p.name for p in (self.hive / "agents" / "claude-1").iterdir())
        self.assertEqual(
            names,
            ["member.json", "member.left.20260926T100000Z-2.json", "member.left.20260926T100000Z.json"],
        )

    def test_next_id_is_integer_max_plus_one(self):
        self.assertEqual(next_member_id(self.hive, "claude-code"), "claude-1")
        for n in (9, 10):
            create_member(self.hive, agent_id=f"claude-{n}", harness="claude-code", now=T0)
        self.assertEqual(next_member_id(self.hive, "claude-code"), "claude-11")
        self.assertEqual(next_member_id(self.hive, "grok"), "grok-1")

    def test_left_ids_are_never_reused(self):
        create_member(self.hive, agent_id="grok-1", harness="grok", now=T0)
        write_left(self.hive, "grok-1", T0)
        self.assertEqual(next_member_id(self.hive, "grok"), "grok-2")

    def test_hand_registered_yaml_id_is_skipped(self):
        create_member(self.hive, agent_id="claude-1", harness="claude-code", now=T0)
        create_member(self.hive, agent_id="claude-2", harness="claude-code", now=T0)
        self.assertEqual(
            next_member_id(self.hive, "claude-code", ["op", "claude-3"]), "claude-4"
        )

    def test_create_member_twice_raises(self):
        create_member(self.hive, agent_id="claude-1", harness="claude-code", now=T0)
        with self.assertRaises(MemberExists):
            create_member(self.hive, agent_id="claude-1", harness="claude-code", now=T0)

    def test_yaml_id_wins_over_member_file(self):
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: claude-1\n  harness: claude-code\n  role: operator\n", encoding="utf-8"
        )
        create_member(self.hive, agent_id="claude-1", harness="grok", now=T0)
        ids = [a["id"] for a in load_registry(self.hive)]
        self.assertEqual(ids, ["claude-1"])
        self.assertEqual(require_agent(self.hive, "claude-1")["role"], "operator")

    def test_unreadable_member_is_skipped_but_reserves_its_id(self):
        folder = self.hive / "agents" / "claude-4"
        folder.mkdir(parents=True)
        (folder / "member.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(list_members(self.hive), [])
        self.assertEqual(member_ids(self.hive), ["claude-4"])
        self.assertEqual(next_member_id(self.hive, "claude-code"), "claude-5")
        with self.assertRaises(UnknownAgent):
            require_agent(self.hive, "claude-4")


if __name__ == "__main__":
    unittest.main()

