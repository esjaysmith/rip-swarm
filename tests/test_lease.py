# tests/test_lease.py — the heartbeat loop (execution proposals §3)
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, local_hive
from rip_swarm.claim import ClaimDenied, heartbeat, try_claim
from rip_swarm.cli import main
from rip_swarm.gitops import GitopsError
from rip_swarm.inbox import create_task
from rip_swarm.lease import (
    LoopRunning, acquire_loop_lock, heartbeat_loop, lease_ttl, release_loop_lock, stop_loop,
)


class Clock:
    def __init__(self, start):
        self.now, self.slept = start, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class TestHeartbeatLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))
        self.tid = create_task(self.hive, title="t", created_by="op", now=T0)["id"]
        try_claim(self.hive, self.tid, "bob", "grok", T0, 1800)
        self.beats, self.lines, self.errors = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def beat(self, now):
        self.beats.append(now)
        doc = heartbeat(self.hive, self.tid, "bob", now, 1800)
        return f"heartbeat {self.tid} until {doc['expires_at']}"

    def loop(self, clock, beat=None, max_beats=2):
        heartbeat_loop(self.hive, self.tid, "bob", ttl=1800, beat=beat or self.beat,
                       clock=clock, sleep=clock.sleep, out=self.lines.append,
                       err=self.errors.append, max_beats=max_beats)

    def test_lease_ttl_follows_the_profile(self):
        profile = {"orchestrator_lease_ttl": "10m", "worker_lease_ttl": "4m"}
        self.assertEqual(lease_ttl(profile, "orchestrator"), 600)
        self.assertEqual(lease_ttl(profile, self.tid), 240)

    def test_sleeps_until_half_the_lease_is_gone(self):
        clock = Clock(T0)
        self.loop(clock)
        self.assertEqual(clock.slept, [900, 900])
        self.assertEqual(self.beats, [T0 + timedelta(seconds=900), T0 + timedelta(seconds=1800)])
        self.assertEqual(len(self.lines), 2)

    def test_under_half_left_beats_at_once(self):
        clock = Clock(T0 + timedelta(seconds=1000))
        self.loop(clock)
        self.assertEqual(self.beats[0], T0 + timedelta(seconds=1000))
        self.assertEqual(clock.slept, [900])

    def test_a_lost_lease_ends_the_loop(self):
        clock = Clock(T0 + timedelta(seconds=1801))
        with self.assertRaisesRegex(ClaimDenied, "claim expired"):
            self.loop(clock)

    def test_other_failures_are_retried_after_30_seconds(self):
        clock = Clock(T0 + timedelta(seconds=1000))
        failures = [GitopsError("push rejected")]

        def flaky(now):
            if failures:
                raise failures.pop()
            return self.beat(now)

        self.loop(clock, beat=flaky, max_beats=1)
        self.assertEqual(clock.slept, [30])
        self.assertEqual(self.beats, [T0 + timedelta(seconds=1030)])
        self.assertIn("push rejected", self.errors[0])

    def test_a_second_loop_is_refused(self):
        lock = acquire_loop_lock(self.hive, self.tid)
        try:
            with self.assertRaises(LoopRunning):
                acquire_loop_lock(self.hive, self.tid)
        finally:
            release_loop_lock(lock)
        release_loop_lock(acquire_loop_lock(self.hive, self.tid))   # free again

    def test_stop_ends_a_running_loop(self):
        # A stand-in process whose command line carries --loop, like the real one.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--loop"])
        try:
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"stopped heartbeat loop {proc.pid} for {self.tid}")
            self.assertNotEqual(proc.wait(timeout=10), 0)
            self.assertFalse(path.exists())
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"no heartbeat loop running for {self.tid}")
        finally:
            proc.kill()
            proc.wait()

    def test_stop_never_kills_a_process_that_is_not_a_loop(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")                 # a stale, reused pid
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"no heartbeat loop running for {self.tid}")
            self.assertIsNone(proc.poll())
        finally:
            proc.kill()
            proc.wait()


class TestHeartbeatLoopCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name))
        self.tid = create_task(self.hive, title="t", created_by="op", now=T0)["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch("rip_swarm.cli.now_utc", return_value=T0), \
                mock.patch("rip_swarm.cli.signal.signal"):
            rc = main([str(a) for a in argv])
        return rc, out.getvalue().strip(), err.getvalue().strip()

    def test_loop_on_a_task_not_held_exits_2_at_once(self):
        rc, _, err = self.run_cli("heartbeat", "--hive", self.hive, "--task", self.tid,
                                  "--agent", "bob", "--loop", "--local")
        self.assertEqual(rc, 2)
        self.assertIn(f"no active claim for {self.tid}", err)
        self.assertEqual(stop_loop(self.hive, self.tid),
                         f"no heartbeat loop running for {self.tid}")   # the pid file is gone

    def test_stop_with_nothing_running(self):
        rc, out, _ = self.run_cli("heartbeat", "--hive", self.hive, "--task", self.tid,
                                  "--agent", "bob", "--stop")
        self.assertEqual((rc, out), (0, f"no heartbeat loop running for {self.tid}"))
