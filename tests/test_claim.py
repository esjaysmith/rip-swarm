# tests/test_claim.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied, claim_baton, complete, heartbeat, reject, release, tombstone_claim, try_claim
from rip_swarm.inbox import create_task
from rip_swarm.registry import UnknownAgent
from rip_swarm.io import read_json
from rip_swarm.timeutil import add_seconds, format_z, parse_z
import json

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

REGISTRY = (
    "- id: alice\n  harness: claude-code\n  role: worker\n"
    "- id: bob\n  harness: codex\n  role: worker\n"
    "- id: carol\n  harness: cursor\n  role: operator\n"
)


def seed_registry(hive, body=REGISTRY):
    p = hive / "agents" / "registry.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p

class TestClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        seed_registry(self.hive)
        self.task = create_task(self.hive, title="T", created_by="op", now=T0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_creates_file(self):
        doc = try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        self.assertEqual(doc["agent"], "alice")
        self.assertEqual(doc["expires_at"], "2026-09-17T09:16:00Z")
        self.assertTrue(doc["exclusive"])
        path = self.hive / "claims" / f"{self.task['id']}.json"
        self.assertEqual(read_json(path)["claim_id"], doc["claim_id"])

    def test_second_claimer_denied_while_unexpired(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        with self.assertRaises(ClaimDenied):
            try_claim(self.hive, self.task["id"], "bob", "codex", T0, 900)
        self.assertEqual(read_json(self.hive / "claims" / f"{self.task['id']}.json")["agent"], "alice")

    def test_expired_steal_renames_then_creates(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        later = add_seconds(T0, 901)
        doc = try_claim(self.hive, self.task["id"], "bob", "codex", later, 900)
        self.assertEqual(doc["agent"], "bob")
        tombstones = list((self.hive / "claims").glob(f"{self.task['id']}.expired.*"))
        self.assertEqual(len(tombstones), 1)

    def test_heartbeat_holder_only(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        hb = heartbeat(self.hive, self.task["id"], "alice", add_seconds(T0, 60), 900)
        self.assertEqual(hb["expires_at"], "2026-09-17T09:17:00Z")
        with self.assertRaises(ClaimDenied):
            heartbeat(self.hive, self.task["id"], "bob", add_seconds(T0, 60), 900)

    def test_complete_requires_result_ref_and_tombstones(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        with self.assertRaises(ClaimDenied):
            complete(self.hive, self.task["id"], "alice", T0, result_ref="  ")
        complete(self.hive, self.task["id"], "alice", T0, result_ref="agents/alice/outbox/msg_x.json")
        self.assertFalse((self.hive / "claims" / f"{self.task['id']}.json").exists())
        done = list((self.hive / "claims").glob(f"{self.task['id']}.complete.*"))
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].name, f"{self.task['id']}.complete.20260917T090100Z.json")
        self.assertEqual(read_json(done[0])["result_ref"], "agents/alice/outbox/msg_x.json")

    def test_non_holder_complete_ignored(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        with self.assertRaises(ClaimDenied):
            complete(self.hive, self.task["id"], "bob", T0, result_ref="x")
        self.assertTrue((self.hive / "claims" / f"{self.task['id']}.json").exists())

    def test_missing_inbox_denied(self):
        with self.assertRaises(ClaimDenied):
            try_claim(self.hive, "task_01J00000000000000000000000", "alice", "claude-code", T0, 900)

    def test_claim_baton_needs_no_inbox(self):
        doc = claim_baton(self.hive, "alice", "claude-code", T0, 1800)
        self.assertEqual(doc["task_id"], "orchestrator")

    def test_reclaim_by_holder_is_idempotent(self):
        a = try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        b = try_claim(self.hive, self.task["id"], "alice", "claude-code", add_seconds(T0, 10), 900)
        self.assertEqual(a["claim_id"], b["claim_id"])
        self.assertEqual(b["expires_at"], a["expires_at"])

    def test_expired_holder_cannot_complete_without_reclaim(self):
        try_claim(self.hive, self.task["id"], "alice", "claude-code", T0, 900)
        later = add_seconds(T0, 901)
        with self.assertRaises(ClaimDenied):
            complete(self.hive, self.task["id"], "alice", later, result_ref="x")
        doc = try_claim(self.hive, self.task["id"], "alice", "claude-code", later, 900)
        complete(self.hive, self.task["id"], "alice", later, result_ref="x")
        self.assertTrue(list((self.hive / "claims").glob(f"{self.task['id']}.expired.*")))
        self.assertEqual(read_json(next((self.hive / "claims").glob(f"{self.task['id']}.complete.*")))["claim_id"], doc["claim_id"])

    def _audit(self):
        log = self.hive / "store" / "claims.jsonl"
        if not log.is_file():
            return []
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]

    # --- tombstone collisions -------------------------------------------------
    def test_two_release_cycles_at_same_now_keep_both_tombstones(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        release(self.hive, tid, "alice", T0, note="r1")
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        release(self.hive, tid, "alice", T0, note="r1")
        done = sorted((self.hive / "claims").glob(f"{tid}.release.*"))
        self.assertEqual(len(done), 2)
        names = {p.name for p in done}
        self.assertIn(f"{tid}.release.20260917T090100Z.json", names)
        self.assertIn(f"{tid}.release.20260917T090100Z-2.json", names)

    def test_tombstone_collision_third_gets_dash_3(self):
        claims = self.hive / "claims"
        claims.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            src = claims / f"{self.task['id']}.json"
            src.write_text(json.dumps({"n": i}), encoding="utf-8")
            tombstone_claim(src, "release", T0)
        names = sorted(p.name for p in claims.glob(f"{self.task['id']}.release.*"))
        self.assertEqual(names, [
            f"{self.task['id']}.release.20260917T090100Z-2.json",
            f"{self.task['id']}.release.20260917T090100Z-3.json",
            f"{self.task['id']}.release.20260917T090100Z.json",
        ])

    def test_stamp_uses_utc_for_aware_non_utc_now(self):
        from datetime import timedelta
        local = datetime(2026, 9, 17, 11, 1, 0, tzinfo=timezone(timedelta(hours=2)))
        try_claim(self.hive, self.task["id"], "alice", "claude-code", local, 900)
        complete(self.hive, self.task["id"], "alice", local, result_ref="r")
        done = list((self.hive / "claims").glob(f"{self.task['id']}.complete.*"))
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].name, f"{self.task['id']}.complete.20260917T090100Z.json")

    # --- steal race -----------------------------------------------------------
    def test_lost_race_stealing_expired_claim(self):
        """The other stealer tombstones the file first; our tombstone of a
        now-missing source must be a refusal, not a traceback."""
        import rip_swarm.claim as claim_mod
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        later = add_seconds(T0, 901)
        active = self.hive / "claims" / f"{tid}.json"
        real = claim_mod.tombstone_claim

        def racing(path, action, now):
            # the rival wins: the active file is gone before we touch it
            Path(path).unlink()
            return real(path, action, now)

        claim_mod.tombstone_claim = racing
        try:
            with self.assertRaises(ClaimDenied) as ctx:
                try_claim(self.hive, tid, "bob", "codex", later, 900)
        finally:
            claim_mod.tombstone_claim = real
        self.assertIn("lost race stealing expired claim", str(ctx.exception))
        self.assertFalse(active.exists())

    def test_tombstone_claim_raises_file_not_found_when_source_gone(self):
        missing = self.hive / "claims" / f"{self.task['id']}.json"
        missing.parent.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(FileNotFoundError):
            tombstone_claim(missing, "expired", T0)

    # --- active path never carries result_ref --------------------------------
    def test_complete_never_writes_result_ref_to_active_path(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        active = self.hive / "claims" / f"{tid}.json"
        seen = []
        import rip_swarm.io as io_mod
        real = io_mod.os.replace

        def spy(src, dst):
            if str(dst) == str(active):
                seen.append(read_json(Path(src)))
            return real(src, dst)

        io_mod.os.replace = spy
        try:
            complete(self.hive, tid, "alice", T0, result_ref="r")
        finally:
            io_mod.os.replace = real
        self.assertTrue(all("result_ref" not in d for d in seen))
        self.assertFalse(active.exists())

    # --- release --------------------------------------------------------------
    def test_release_by_holder(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        release(self.hive, tid, "alice", T0, note="stepping away")
        self.assertFalse((self.hive / "claims" / f"{tid}.json").exists())
        dest = self.hive / "claims" / f"{tid}.release.20260917T090100Z.json"
        self.assertTrue(dest.is_file())
        self.assertEqual(read_json(dest)["note"], "stepping away")
        self.assertNotIn("result_ref", read_json(dest))
        self.assertEqual(self._audit()[-1]["action"], "release")
        self.assertEqual(self._audit()[-1]["note"], "stepping away")

    def test_release_by_non_holder_leaves_file_unchanged(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        active = self.hive / "claims" / f"{tid}.json"
        before = active.read_bytes()
        with self.assertRaises(ClaimDenied):
            release(self.hive, tid, "bob", T0, note="nope")
        self.assertEqual(active.read_bytes(), before)
        self.assertEqual(list((self.hive / "claims").glob(f"{tid}.release.*")), [])
        self.assertEqual([r["action"] for r in self._audit()], ["claim"])

    # --- reject ---------------------------------------------------------------
    def test_reject_by_holder(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        reject(self.hive, tid, "alice", T0, note="out of scope")
        self.assertFalse((self.hive / "claims" / f"{tid}.json").exists())
        dest = self.hive / "claims" / f"{tid}.reject.20260917T090100Z.json"
        self.assertTrue(dest.is_file())
        self.assertEqual(read_json(dest)["note"], "out of scope")
        self.assertEqual(self._audit()[-1]["action"], "reject")
        self.assertEqual(self._audit()[-1]["note"], "out of scope")

    def test_reject_by_non_holder_leaves_file_unchanged(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        active = self.hive / "claims" / f"{tid}.json"
        before = active.read_bytes()
        with self.assertRaises(ClaimDenied):
            reject(self.hive, tid, "bob", T0, note="nope")
        self.assertEqual(active.read_bytes(), before)
        self.assertEqual(list((self.hive / "claims").glob(f"{tid}.reject.*")), [])
        self.assertEqual([r["action"] for r in self._audit()], ["claim"])

    def test_release_without_note_keeps_original_note(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900, note="initial")
        release(self.hive, tid, "alice", T0)
        dest = self.hive / "claims" / f"{tid}.release.20260917T090100Z.json"
        self.assertEqual(read_json(dest)["note"], "initial")

    # --- path traversal -------------------------------------------------------
    def test_try_claim_rejects_unsafe_task_ids(self):
        for tid in ["../evil", "a/b", "a\\b", "..", ".hidden", "x/../y", "./x"]:
            with self.subTest(tid=tid):
                with self.assertRaises(ClaimDenied):
                    try_claim(self.hive, tid, "alice", "claude-code", T0, 900)

    def test_try_claim_traversal_writes_nothing_outside_claims(self):
        outside = self.hive / "pwned.json"
        with self.assertRaises(ClaimDenied):
            try_claim(self.hive, "../pwned", "alice", "claude-code", T0, 900)
        self.assertFalse(outside.exists())

    # --- M2: registry gate at the primitive layer -----------------------------
    def _claims_snapshot(self):
        claims = self.hive / "claims"
        if not claims.is_dir():
            return set()
        return {p.name for p in claims.iterdir()}

    def test_unregistered_agent_refused_on_every_primitive(self):
        tid = self.task["id"]
        try_claim(self.hive, tid, "alice", "claude-code", T0, 900)
        before = self._claims_snapshot()
        calls = {
            "try_claim": lambda: try_claim(self.hive, tid, "ghost", "codex", T0, 900),
            "heartbeat": lambda: heartbeat(self.hive, tid, "ghost", T0, 900),
            "complete": lambda: complete(self.hive, tid, "ghost", T0, result_ref="r"),
            "release": lambda: release(self.hive, tid, "ghost", T0),
            "reject": lambda: reject(self.hive, tid, "ghost", T0),
            "claim_baton": lambda: claim_baton(self.hive, "ghost", "codex", T0, 1800),
        }
        for name, call in calls.items():
            with self.subTest(primitive=name):
                with self.assertRaises(UnknownAgent):
                    call()
                self.assertEqual(self._claims_snapshot(), before)

    def test_unregistered_agent_writes_no_audit_line(self):
        with self.assertRaises(UnknownAgent):
            try_claim(self.hive, self.task["id"], "ghost", "codex", T0, 900)
        self.assertEqual(self._audit(), [])

    def test_harness_mismatch_on_try_claim_denied(self):
        with self.assertRaises(ClaimDenied) as ctx:
            try_claim(self.hive, self.task["id"], "alice", "totally-wrong", T0, 900)
        msg = str(ctx.exception)
        self.assertIn("claude-code", msg)
        self.assertIn("totally-wrong", msg)
        self.assertFalse((self.hive / "claims" / f"{self.task['id']}.json").exists())

    def test_harness_mismatch_on_claim_baton_denied(self):
        with self.assertRaises(ClaimDenied):
            claim_baton(self.hive, "alice", "codex", T0, 1800)
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())

    def test_registered_second_harness_is_accepted(self):
        t2 = create_task(self.hive, title="T2", created_by="op", now=T0)
        doc = try_claim(self.hive, t2["id"], "bob", "codex", T0, 900)
        self.assertEqual(doc["agent"], "bob")

    # --- M3: the baton is promote-only ----------------------------------------
    def test_try_claim_refuses_orchestrator(self):
        with self.assertRaises(ClaimDenied) as ctx:
            try_claim(self.hive, "orchestrator", "alice", "claude-code", T0, 1800)
        self.assertIn("promote", str(ctx.exception))
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())

    def test_claim_baton_steal_and_idempotence(self):
        a = claim_baton(self.hive, "alice", "claude-code", T0, 1800)
        again = claim_baton(self.hive, "alice", "claude-code", add_seconds(T0, 10), 1800)
        self.assertEqual(a["claim_id"], again["claim_id"])
        with self.assertRaises(ClaimDenied):
            claim_baton(self.hive, "bob", "codex", T0, 1800)
        later = add_seconds(T0, 1801)
        stolen = claim_baton(self.hive, "bob", "codex", later, 1800)
        self.assertEqual(stolen["agent"], "bob")
        self.assertTrue(list((self.hive / "claims").glob("orchestrator.expired.*")))

    # --- m5: claimable ids follow the inbox pattern ---------------------------
    def test_planted_non_ulid_inbox_task_is_not_claimable(self):
        planted = self.hive / "inbox" / "weird-task.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(json.dumps({"id": "weird-task"}), encoding="utf-8")
        with self.assertRaises(ClaimDenied):
            try_claim(self.hive, "weird-task", "alice", "claude-code", T0, 900)
        self.assertFalse((self.hive / "claims" / "weird-task.json").exists())

    def test_reserved_and_malformed_ids_refused(self):
        for tid in ["weird-task", "task_lowercase", "CURRENT", "registry", "task_01J"]:
            with self.subTest(tid=tid):
                with self.assertRaises(ClaimDenied):
                    try_claim(self.hive, tid, "alice", "claude-code", T0, 900)

if __name__ == "__main__":
    unittest.main()
