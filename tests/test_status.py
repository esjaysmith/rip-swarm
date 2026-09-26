# tests/test_status.py
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import complete, reject, try_claim
from rip_swarm.inbox import create_task
from rip_swarm.io import atomic_write_json
from rip_swarm.orchestrator import promote
from rip_swarm.status import format_status, status_report
from rip_swarm.timeutil import add_seconds

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

REGISTRY = (
    "- id: alice\n  harness: claude-code\n  role: worker\n"
    "- id: bob\n  harness: codex\n  role: worker\n"
    "- id: carol\n  harness: cursor\n  role: operator\n"
)


def _reg(hive, body=REGISTRY):
    p = hive / "agents" / "registry.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")

class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name)
        _reg(self.hive)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty(self):
        r = status_report(self.hive, T0)
        self.assertEqual(r["active_claims"], [])
        self.assertEqual(r["inbox_without_claim"], [])
        self.assertIsNone(r["orchestrator"]["agent"])

    def test_inbox_without_claim(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        r = status_report(self.hive, T0)
        self.assertEqual(r["inbox_without_claim"], [t["id"]])

    def test_current_mismatch(self):
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        atomic_write_json(
            self.hive / "orchestrator" / "CURRENT.json",
            {"agent": "eve", "harness": "x", "lease_expires_at": "2026-09-17T09:31:00Z",
             "reason": "x", "claim_id": "clm_x"},
        )
        r = status_report(self.hive, T0)
        self.assertTrue(r["current_mismatch"])
        self.assertFalse(r["orchestrator"]["matches_claim"])

    def test_jsonl_parse_error(self):
        log = self.hive / "store" / "messages.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{bad\n", encoding="utf-8")
        r = status_report(self.hive, T0)
        self.assertEqual(r["jsonl_parse_errors"][0]["line"], 1)

    def test_unknown_agent_on_claim(self):
        # try_claim now refuses unregistered agents, so an unknown id can only
        # reach a claim file by being planted or by leaving the registry.
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        _reg(self.hive, "- id: bob\n  harness: codex\n  role: worker\n")
        r = status_report(self.hive, T0)
        self.assertIn("alice", r["unknown_agents"])
        self.assertIn(t["id"], format_status(r))

    def test_expired_claim_file(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        r = status_report(self.hive, add_seconds(T0, 901))
        self.assertEqual(r["expired_claim_files"], [t["id"]])

    def test_orchestrator_excluded_from_active_claims(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        promote(
            self.hive, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="a", allow_self_promote=True, operators=[],
        )
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        r = status_report(self.hive, T0)
        self.assertEqual([c["task_id"] for c in r["active_claims"]], [t["id"]])
        self.assertNotIn("orchestrator", r["inbox_without_claim"])

    def test_inbox_without_claim_excludes_completed(self):
        done = create_task(self.hive, title="done", created_by="op", now=T0)
        rejected = create_task(self.hive, title="rej", created_by="op", now=T0)
        open_t = create_task(self.hive, title="open", created_by="op", now=T0)
        try_claim(self.hive, done["id"], "alice", "claude-code", T0, 900)
        complete(self.hive, done["id"], "alice", T0, result_ref="r")
        try_claim(self.hive, rejected["id"], "alice", "claude-code", T0, 900)
        reject(self.hive, rejected["id"], "alice", T0, note="no")
        r = status_report(self.hive, T0)
        self.assertEqual(r["inbox_without_claim"], [open_t["id"]])

    def test_corrupt_claims_reported_and_no_crash(self):
        claims = self.hive / "claims"
        claims.mkdir(parents=True, exist_ok=True)
        (claims / "task_bad.json").write_text("{not json", encoding="utf-8")
        (claims / "task_empty.json").write_text("", encoding="utf-8")
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        r = status_report(self.hive, T0)
        self.assertEqual(sorted(c["task_id"] for c in r["corrupt_claims"]),
                         ["task_bad", "task_empty"])
        for c in r["corrupt_claims"]:
            self.assertEqual(sorted(c), ["error", "path", "task_id"])
            self.assertTrue(c["error"])
        self.assertEqual([c["task_id"] for c in r["active_claims"]], [t["id"]])
        out = format_status(r)
        self.assertIn("corrupt_claims:", out)
        self.assertIn("task_bad", out)

    def test_corrupt_claims_empty_section_printed(self):
        r = status_report(self.hive, T0)
        self.assertEqual(r["corrupt_claims"], [])
        self.assertIn("corrupt_claims:", format_status(r))

    # --- m2: active claim + complete tombstone after a crash -----------------

    def test_crash_window_reported_corrupt_not_active(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        active = self.hive / "claims" / f"{t['id']}.json"
        (self.hive / "claims" / f"{t['id']}.complete.20260917T090100Z.json").write_text(
            active.read_text(encoding="utf-8"), encoding="utf-8"
        )
        r = status_report(self.hive, T0)
        self.assertEqual(r["active_claims"], [])
        self.assertEqual(
            [(c["task_id"], c["error"]) for c in r["corrupt_claims"]],
            [(t["id"], "active claim and complete tombstone coexist")],
        )
        self.assertEqual(r["inbox_without_claim"], [])
        out = format_status(r)
        self.assertIn("active claim and complete tombstone coexist", out)

    # --- m1: active claim + release/reject tombstone is corrupt AND unattended -

    def test_release_crash_window_corrupt_and_unattended(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        active = self.hive / "claims" / f"{t['id']}.json"
        (self.hive / "claims" / f"{t['id']}.release.20260917T090100Z.json").write_text(
            active.read_text(encoding="utf-8"), encoding="utf-8"
        )
        r = status_report(self.hive, T0)
        self.assertEqual(r["active_claims"], [])
        self.assertEqual(
            [(c["task_id"], c["error"]) for c in r["corrupt_claims"]],
            [(t["id"], "active claim and release tombstone coexist")],
        )
        self.assertEqual(r["inbox_without_claim"], [t["id"]])

    def test_reject_crash_window_corrupt_and_unattended(self):
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        try_claim(self.hive, t["id"], "alice", "claude-code", T0, 900)
        active = self.hive / "claims" / f"{t['id']}.json"
        (self.hive / "claims" / f"{t['id']}.reject.20260917T090100Z.json").write_text(
            active.read_text(encoding="utf-8"), encoding="utf-8"
        )
        r = status_report(self.hive, T0)
        self.assertEqual(r["active_claims"], [])
        self.assertEqual(
            [(c["task_id"], c["error"]) for c in r["corrupt_claims"]],
            [(t["id"], "active claim and reject tombstone coexist")],
        )
        self.assertEqual(r["inbox_without_claim"], [])

    def test_member_that_left_is_not_an_unknown_agent(self):
        from rip_swarm.members import create_member, write_left
        t = create_task(self.hive, title="T", created_by="op", now=T0)
        create_member(self.hive, agent_id="grok-1", harness="grok", now=T0)
        try_claim(self.hive, t["id"], "grok-1", "grok", T0, 900)
        write_left(self.hive, "grok-1", T0)
        r = status_report(self.hive, T0)
        self.assertEqual(r["unknown_agents"], [])

    def test_members_blocked_awaiting_and_fixes(self):
        from rip_swarm.members import create_member, write_left
        from rip_swarm.outbox import write_message
        create_member(self.hive, agent_id="grok-1", harness="grok", now=T0)
        create_member(self.hive, agent_id="grok-2", harness="grok", now=T0)
        write_left(self.hive, "grok-2", T0)
        later = add_seconds(T0, 60)
        write_message(self.hive, agent="grok-1", harness="grok", type="note", to="*",
                      body={"text": "joined"}, now=later)
        a = create_task(self.hive, title="a", created_by="op", now=T0)
        b = create_task(self.hive, title="b", created_by="op", now=T0, after=[a["id"]])
        f = create_task(self.hive, title="f", created_by="op", now=T0, fixes=a["id"])
        try_claim(self.hive, a["id"], "alice", "claude-code", T0, 900)
        complete(self.hive, a["id"], "alice", T0, result_ref="rip-swarm/alice@abc1234")
        r = status_report(self.hive, T0)
        self.assertEqual(
            r["members"],
            [{"id": "grok-1", "harness": "grok", "joined_at": "2026-09-17T09:01:00Z",
              "last_activity": "2026-09-17T09:02:00Z"}],
        )
        self.assertEqual(r["left_members"], ["grok-2"])
        self.assertEqual(r["blocked"], [{"task_id": b["id"], "waiting_on": [a["id"]]}])
        self.assertEqual(r["awaiting_acceptance"], [a["id"]])
        self.assertEqual(r["fixes"], {f["id"]: a["id"]})
        self.assertEqual(r["inbox_without_claim"], [f["id"]])
        text = format_status(r)
        self.assertIn(f"  {b['id']} waiting on {a['id']}", text)
        self.assertIn(f"  {f['id']} f (fixes {a['id']})", text)
        self.assertIn("  grok-1 harness=grok joined_at=2026-09-17T09:01:00Z last_activity=2026-09-17T09:02:00Z", text)
        self.assertIn("left_members:\n  grok-2", text)
        self.assertIn(f"awaiting_acceptance:\n  {a['id']} a", text)

    def test_fixes_marker_on_claimed_blocked_and_awaiting(self):
        # Controller ruling on spec §7.4: `(fixes <T>)` is not only for
        # inbox_without_claim — it belongs next to a fixer wherever it shows
        # up (claimed, blocked, or awaiting acceptance).
        a = create_task(self.hive, title="a", created_by="op", now=T0)
        dep = create_task(self.hive, title="dep", created_by="op", now=T0)
        claimed_fix = create_task(
            self.hive, title="claimed fix", created_by="op", now=T0, fixes=a["id"]
        )
        blocked_fix = create_task(
            self.hive, title="blocked fix", created_by="op", now=T0,
            after=[dep["id"]], fixes=a["id"],
        )
        awaiting_fix = create_task(
            self.hive, title="awaiting fix", created_by="op", now=T0, fixes=a["id"]
        )
        try_claim(self.hive, claimed_fix["id"], "alice", "claude-code", T0, 900)
        try_claim(self.hive, awaiting_fix["id"], "bob", "codex", T0, 900)
        complete(self.hive, awaiting_fix["id"], "bob", T0, result_ref="r")
        r = status_report(self.hive, T0)
        self.assertEqual(r["fixes"][claimed_fix["id"]], a["id"])
        self.assertEqual(r["fixes"][blocked_fix["id"]], a["id"])
        self.assertEqual(r["fixes"][awaiting_fix["id"]], a["id"])
        self.assertEqual(
            r["blocked"], [{"task_id": blocked_fix["id"], "waiting_on": [dep["id"]]}]
        )
        self.assertEqual(r["awaiting_acceptance"], [awaiting_fix["id"]])
        text = format_status(r)
        marker = f"(fixes {a['id']})"
        active_line = next(
            line for line in text.splitlines() if claimed_fix["id"] in line and "agent=alice" in line
        )
        self.assertIn(marker, active_line)
        blocked_line = next(
            line for line in text.splitlines() if blocked_fix["id"] in line and "waiting on" in line
        )
        self.assertIn(marker, blocked_line)
        awaiting_line = next(
            line for line in text.splitlines()
            if line.strip().startswith(awaiting_fix["id"])
        )
        self.assertIn(marker, awaiting_line)

if __name__ == "__main__":
    unittest.main()
