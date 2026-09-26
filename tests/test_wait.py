# tests/test_wait.py — wake reasons, seen sets, heartbeat, lock (spec §7)
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import REGISTRY, T0, local_hive
from rip_swarm.acceptance import accept_task, master_reject
from rip_swarm.claim import ClaimDenied, complete, release, try_claim
from rip_swarm.cli import main
from rip_swarm.inbox import create_task
from rip_swarm.members import create_member, write_left
from rip_swarm.messages import unread_messages
from rip_swarm.orchestrator import promote
from rip_swarm.outbox import write_message
from rip_swarm.state import (
    acquire_wait_lock,
    ensure_state,
    load_state,
    mark_seen_open,
    release_wait_lock,
    save_state,
    seed_state,
)
from rip_swarm.waiter import Wake, run_wait, tick

REG = REGISTRY + "- id: carol\n  harness: claude-code\n  role: worker\n"
S = timedelta(seconds=1)


class TestTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name), REG)
        promote(self.hive, agent="alice", harness="claude-code", now=T0, lease_seconds=1800,
                reason="master", allow_self_promote=False, operators=["op"], by="op")
        self.states = {}

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, title, **kw):
        return create_task(self.hive, title=title, created_by="alice", now=T0, **kw)["id"]

    def tick(self, agent, now=T0):
        if agent not in self.states:
            self.states[agent] = seed_state(self.hive, agent)
        return tick(self.hive, agent, self.states[agent], now, idle_after=600)

    def test_worker_is_offered_each_generation_once(self):
        t = self._task("t")
        self.assertEqual(self.tick("bob"), Wake("task-available", t))
        self.assertIsNone(self.tick("bob"))
        try_claim(self.hive, t, "carol", "claude-code", T0, 900)
        release(self.hive, t, "carol", T0)
        self.assertEqual(self.tick("bob"), Wake("task-available", t))

    def test_own_release_is_not_reoffered_to_the_releaser(self):
        t = self._task("t")
        self.tick("bob")
        self.tick("carol")
        try_claim(self.hive, t, "bob", "grok", T0, 900)
        release(self.hive, t, "bob", T0)
        save_state(self.hive, self.states["bob"])
        mark_seen_open(self.hive, "bob", f"{t}#1")          # what CLI release does
        self.states["bob"] = load_state(self.hive, "bob")
        self.assertIsNone(self.tick("bob"))
        self.assertEqual(self.tick("carol"), Wake("task-available", t))
        try_claim(self.hive, t, "carol", "claude-code", T0, 900)
        release(self.hive, t, "carol", T0)
        self.assertEqual(self.tick("bob"), Wake("task-available", t))

    def test_expired_unstolen_claim_is_offered(self):
        t = self._task("t")
        try_claim(self.hive, t, "carol", "claude-code", T0, 60)
        self.assertEqual(self.tick("bob", T0 + 120 * S), Wake("task-available", t))

    def test_worker_is_offered_one_open_task_per_wake(self):
        # max_claims_open_per_agent is 1: a wake names one task, and only that
        # one is marked seen, so the other is offered after this one is done.
        t1, t2 = sorted([self._task("t1"), self._task("t2")])
        self.assertEqual(self.tick("bob"), Wake("task-available", t1))
        try_claim(self.hive, t1, "bob", "grok", T0, 900)
        self.assertIsNone(self.tick("bob"))
        complete(self.hive, t1, "bob", T0, result_ref="rip-swarm/bob@abc1234")
        self.assertEqual(self.tick("bob"), Wake("task-available", t2))
        self.assertIsNone(self.tick("bob"))

    def test_declined_offer_moves_on_to_the_next_task(self):
        t1, t2 = sorted([self._task("t1"), self._task("t2")])
        self.assertEqual(self.tick("bob"), Wake("task-available", t1))
        # bob does not claim t1 (not his, or exit 2): the next wake is t2, not t1
        self.assertEqual(self.tick("bob"), Wake("task-available", t2))
        self.assertIsNone(self.tick("bob"))

    def test_worker_with_a_claim_is_not_offered_more(self):
        t1 = self._task("t1")
        self._task("t2")
        try_claim(self.hive, t1, "bob", "grok", T0, 900)
        self.assertIsNone(self.tick("bob"))

    def test_message_comes_first_and_does_not_advance_the_cursor(self):
        self._task("t")
        self.states["bob"] = seed_state(self.hive, "bob")
        write_message(self.hive, agent="alice", harness="claude-code", type="note",
                      to="bob", body={"text": "hi"}, now=T0 + S)
        self.assertEqual(self.tick("bob"), Wake("message"))
        self.assertEqual(self.tick("bob"), Wake("message"))

    def test_lease_lost_but_not_after_own_complete(self):
        t = self._task("t")
        try_claim(self.hive, t, "bob", "grok", T0, 60)
        self.assertIsNone(self.tick("bob"))
        try_claim(self.hive, t, "carol", "claude-code", T0 + 120 * S, 900)   # steal
        self.assertEqual(self.tick("bob", T0 + 120 * S), Wake("lease-lost", t))
        u = self._task("u")
        try_claim(self.hive, u, "bob", "grok", T0, 900)
        self.assertIsNone(self.tick("bob"))
        complete(self.hive, u, "bob", T0, result_ref="rip-swarm/bob@abc1234")
        self.assertIsNone(self.tick("bob"))

    def test_master_hears_each_tombstone_once(self):
        t = self._task("t")
        self.tick("alice")
        try_claim(self.hive, t, "bob", "grok", T0, 900)
        complete(self.hive, t, "bob", T0, result_ref="rip-swarm/bob@abc1234")
        self.assertEqual(self.tick("alice"), Wake("task-finished", f"{t} complete"))
        self.assertIsNone(self.tick("alice"))

    def test_master_hears_an_unstolen_expiry_once(self):
        t = self._task("t")
        try_claim(self.hive, t, "bob", "grok", T0, 60)
        self.assertEqual(self.tick("alice", T0 + 120 * S), Wake("task-finished", f"{t} expired"))
        self.assertIsNone(self.tick("alice", T0 + 120 * S))

    def test_all_complete_needs_acceptance_or_rejection(self):
        t = self._task("t")
        u = self._task("u", after=[t])
        try_claim(self.hive, t, "bob", "grok", T0, 900)
        complete(self.hive, t, "bob", T0, result_ref="rip-swarm/bob@abc1234")
        self.assertEqual(self.tick("alice"), Wake("task-finished", f"{t} complete"))
        self.assertIsNone(self.tick("alice"))             # t unaccepted, u blocked
        accept_task(self.hive, agent="alice", task_id=t, integration_sha="abc1234", now=T0)
        self.assertIsNone(self.tick("alice"))             # u open now, not settled
        master_reject(self.hive, agent="alice", task_id=u, note="drop", now=T0)
        self.assertEqual(self.tick("alice"), Wake("task-finished", f"{u} reject"))
        self.assertEqual(self.tick("alice"), Wake("all-complete"))

    def test_idle_board_once_per_return(self):
        t = self._task("t")
        self.assertIsNone(self.tick("alice", T0 + 599 * S))
        self.assertEqual(self.tick("alice", T0 + 601 * S), Wake("idle-board", t))
        self.assertIsNone(self.tick("alice", T0 + 700 * S))

    def test_reject_cascade_resumes_after_a_master_crash(self):
        # T <- D <- E. The master rejects T and D, then dies before E.
        t = self._task("t")
        d = self._task("d", after=[t])
        e = self._task("e", after=[d])
        self.tick("alice")
        master_reject(self.hive, agent="alice", task_id=t, note="drop", now=T0)
        master_reject(self.hive, agent="alice", task_id=d, note=f"dependency {t} rejected", now=T0)
        del self.states["alice"]                               # crash: the state is gone
        # T's dependent D is settled, so T's reject stays seen; D's is not: E waits on it.
        self.assertEqual(self.tick("alice"), Wake("task-finished", f"{d} reject"))
        self.assertIsNone(self.tick("alice"))
        self.assertIsNone(self.tick("bob"))                    # a worker is not disturbed
        master_reject(self.hive, agent="alice", task_id=e, note=f"dependency {d} rejected", now=T0)
        self.assertEqual(self.tick("alice"), Wake("task-finished", f"{e} reject"))
        self.assertEqual(self.tick("alice"), Wake("all-complete"))
        del self.states["alice"]                               # a later re-seed has nothing left
        self.assertEqual(self.tick("alice"), Wake("all-complete"))

    def test_bare_complete_wakes_a_new_master_once(self):
        t = self._task("t")
        try_claim(self.hive, t, "bob", "grok", T0, 900)
        complete(self.hive, t, "bob", T0, result_ref="rip-swarm/bob@abc1234")
        self.assertEqual(self.tick("alice"), Wake("task-finished", f"{t} complete"))
        self.assertIsNone(self.tick("alice"))


class TestRunWait(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name), REG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeout_and_heartbeat_while_waiting(self):
        t = create_task(self.hive, title="t", created_by="op", now=T0)["id"]
        try_claim(self.hive, t, "bob", "grok", T0, 1800)
        wake, leases, _notes = run_wait(
            self.hive, "bob", timeout=0, interval=1,
            clock=lambda: T0 + 1000 * S, sleep=lambda _s: None,
        )
        self.assertEqual(wake, Wake("timeout"))
        # 800s left of a 30m lease is under half, so wait heartbeated to +1800s.
        self.assertEqual(leases, [f"held {t} until 2026-09-26T10:46:40Z"])

    def test_heartbeat_race_is_skipped_not_a_refusal(self):
        # Ruling a: a ClaimDenied inside the heartbeat skips it; the next tick retries.
        t = create_task(self.hive, title="t", created_by="op", now=T0)["id"]
        try_claim(self.hive, t, "bob", "grok", T0, 1800)
        with mock.patch("rip_swarm.waiter.heartbeat", side_effect=ClaimDenied("lost race")):
            wake, leases, _notes = run_wait(
                self.hive, "bob", timeout=0, interval=1,
                clock=lambda: T0 + 1000 * S, sleep=lambda _s: None,
            )
        self.assertEqual(wake, Wake("timeout"))
        self.assertEqual(leases, [f"held {t} until 2026-09-26T10:30:00Z"])

    def test_cursor_advanced_while_waiting_survives(self):
        # Ruling b: a foreground `messages --new` between two ticks is not overwritten.
        ensure_state(self.hive, "bob")
        times = iter([T0, T0, T0 + 20 * S])

        def sleep(_s):
            write_message(self.hive, agent="alice", harness="claude-code", type="note",
                          to="bob", body={"text": "hi"}, now=T0 + S)
            state = load_state(self.hive, "bob")
            found = unread_messages(self.hive, agent="bob",
                                    cursor=state["messages_cursor"], now=T0 + 2 * S)
            state["messages_cursor"] = [found[-1]["ts"], found[-1].get("id", "")]
            save_state(self.hive, state)

        wake, _leases, notes = run_wait(
            self.hive, "bob", timeout=10, interval=1,
            clock=lambda: next(times), sleep=sleep,
        )
        self.assertEqual(wake, Wake("timeout"))
        self.assertEqual(notes, [])
        self.assertIsNotNone(load_state(self.hive, "bob")["messages_cursor"])

    def test_cli_prints_the_wake_line(self):
        t = create_task(self.hive, title="t", created_by="op", now=T0)["id"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["wait", "--hive", str(self.hive), "--agent", "bob",
                       "--timeout", "0", "--interval", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().splitlines()[0], f"wake task-available {t}")
        self.assertIn("NOTE state re-seeded", err.getvalue())

    def test_cli_exit_3_when_a_wait_is_running(self):
        lock = acquire_wait_lock(self.hive)
        try:
            with redirect_stderr(io.StringIO()) as err:
                rc = main(["wait", "--hive", str(self.hive), "--agent", "bob", "--timeout", "0"])
            self.assertEqual(rc, 3)
            self.assertIn("wait already running", err.getvalue())
        finally:
            release_wait_lock(lock)

    def test_cli_exit_1_for_an_agent_that_left(self):
        # Review focus 3: never spins, never re-registers.
        create_member(self.hive, agent_id="grok-1", harness="grok", now=T0)
        write_left(self.hive, "grok-1", T0)
        with redirect_stderr(io.StringIO()) as err:
            rc = main(["wait", "--hive", str(self.hive), "--agent", "grok-1", "--timeout", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown agent", err.getvalue())


if __name__ == "__main__":
    unittest.main()
