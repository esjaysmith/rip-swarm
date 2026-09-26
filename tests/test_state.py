# tests/test_state.py — local seen-state, unread messages, own release, wait lock (spec §6, §7.3, §7.5)
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, local_hive
from rip_swarm.claim import complete, reject, release, try_claim
from rip_swarm.cli import main
from rip_swarm.inbox import create_task
from rip_swarm.io import excl_create_json
from rip_swarm.outbox import write_message
from rip_swarm.paths import HivePaths
from rip_swarm.state import (
    WaitRunning,
    acquire_wait_lock,
    ensure_state,
    load_state,
    mark_seen_open,
    release_wait_lock,
    save_state,
    seed_state,
    state_dir,
)


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, title):
        return create_task(self.hive, title=title, created_by="op", now=T0)["id"]

    def _msg(self, frm, to, text, at):
        harness = {"alice": "claude-code", "bob": "grok", "op": "human"}[frm]
        return write_message(self.hive, agent=frm, harness=harness, type="note", to=to,
                             body={"text": text}, now=at)

    def test_seed_skips_bare_completes_only(self):
        bare = self._task("bare")
        try_claim(self.hive, bare, "alice", "claude-code", T0, 900)
        complete(self.hive, bare, "alice", T0, result_ref="r")
        acc = self._task("acc")
        try_claim(self.hive, acc, "bob", "grok", T0, 900)
        complete(self.hive, acc, "bob", T0, result_ref="r")
        excl_create_json(HivePaths(self.hive).accepted_record(acc),
                         {"task_id": acc, "by": "alice", "at": "2026-09-26T10:00:00Z",
                          "integration_sha": "abc1234", "via": []})
        rel = self._task("rel")
        try_claim(self.hive, rel, "alice", "claude-code", T0, 900)
        release(self.hive, rel, "alice", T0)
        rej = self._task("rej")
        try_claim(self.hive, rej, "bob", "grok", T0, 900)
        reject(self.hive, rej, "bob", T0)
        last = self._msg("alice", "bob", "hi", T0)
        state = seed_state(self.hive, "bob")
        names = state["seen_tombstones"]
        self.assertFalse(any(n.startswith(f"{bare}.complete.") for n in names))
        self.assertTrue(any(n.startswith(f"{acc}.complete.") for n in names))
        self.assertTrue(any(n.startswith(f"{rel}.release.") for n in names))
        self.assertTrue(any(n.startswith(f"{rej}.reject.") for n in names))
        self.assertEqual(state["messages_cursor"], [last["ts"], last["id"]])
        self.assertEqual((state["seen_open"], state["seen_expiries"], state["held"]), ([], [], []))

    def test_ensure_state_reseeds_missing_or_foreign_state(self):
        state, reseeded = ensure_state(self.hive, "bob")
        self.assertTrue(reseeded)
        self.assertEqual(ensure_state(self.hive, "bob"), (state, False))
        self._msg("alice", "bob", "old", T0)
        save_state(self.hive, {**state, "agent": "alice"})
        # Review focus 4: another agent's state is ignored, not replayed.
        fresh, reseeded = ensure_state(self.hive, "bob")
        self.assertTrue(reseeded)
        from rip_swarm.messages import unread_messages
        self.assertEqual(
            unread_messages(self.hive, agent="bob", cursor=fresh["messages_cursor"], now=T0), []
        )
        self.assertIsNone(load_state(self.hive, "alice"))

    def test_state_lives_in_dot_git_when_present(self):
        (self.hive / ".git").mkdir()
        self.assertEqual(state_dir(self.hive), self.hive / ".git")

    def test_messages_new_prints_each_message_once(self):
        ensure_state(self.hive, "bob")
        self._msg("alice", "bob", "first", T0)
        self._msg("alice", "*", "second", T0 + timedelta(seconds=1))
        self._msg("bob", "alice", "own", T0 + timedelta(seconds=2))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["messages", "--hive", str(self.hive), "--to", "bob", "--new"])
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("alice -> bob [note] first", lines[0])
        self.assertIn("alice -> * [note] second", lines[1])
        out = io.StringIO()
        with redirect_stdout(out):
            main(["messages", "--hive", str(self.hive), "--to", "bob", "--new"])
        self.assertEqual(out.getvalue(), "(no messages)\n")
        with redirect_stderr(io.StringIO()) as err:
            rc = main(["messages", "--hive", str(self.hive), "--new"])
        self.assertEqual(rc, 1)
        self.assertIn("--new requires --to", err.getvalue())

    def test_own_release_is_marked_seen_for_the_releaser_only(self):
        t = self._task("t")
        ensure_state(self.hive, "bob")
        with mock.patch("rip_swarm.cli.now_utc", return_value=T0), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["claim", "--hive", str(self.hive), "--local",
                                   "--task", t, "--agent", "bob"]), 0)
            self.assertEqual(main(["release", "--hive", str(self.hive), "--local",
                                   "--task", t, "--agent", "bob", "--note", "cannot merge"]), 0)
        self.assertIn(f"{t}#1", load_state(self.hive, "bob")["seen_open"])

    def test_release_without_state_seeds_it_then_marks_seen(self):
        # Ruling c: a releaser with no state file must not be re-offered its release.
        t = self._task("t")
        self.assertIsNone(load_state(self.hive, "bob"))
        mark_seen_open(self.hive, "bob", f"{t}#1")
        state = load_state(self.hive, "bob")
        self.assertEqual(state["seen_open"], [f"{t}#1"])

    def test_wait_lock(self):
        path = acquire_wait_lock(self.hive)
        with self.assertRaises(WaitRunning) as ctx:
            acquire_wait_lock(self.hive)
        self.assertEqual(ctx.exception.pid, os.getpid())
        release_wait_lock(path)
        self.assertFalse(path.exists())
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path.write_text(str(dead.pid), encoding="utf-8")
        again = acquire_wait_lock(self.hive)          # stale lock replaced
        self.assertEqual(again.read_text(encoding="utf-8"), str(os.getpid()))
        release_wait_lock(again)


if __name__ == "__main__":
    unittest.main()
