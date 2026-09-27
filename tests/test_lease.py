# tests/test_lease.py — the heartbeat loop (execution proposals §3)
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
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
    PARENT_CHECK_SECONDS, LoopRunning, LoopStop, LoopStopTimeout, acquire_loop_lock,
    heartbeat_loop, lease_ttl, release_loop_lock, session_ancestry, stop_loop,
)
from rip_swarm.state import state_dir
from rip_swarm.timeutil import now_utc


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

    def test_a_loop_whose_session_is_gone_ends(self):
        """Final review I2: a loop reparented because its session died would
        keep the lease alive forever. It checks its parent on every wake-up,
        sleeps at most PARENT_CHECK_SECONDS at a time, and ends with a line
        saying why."""
        clock, parents = Clock(T0), iter([100, 100, 100, 100, 1])
        heartbeat_loop(self.hive, self.tid, "bob", ttl=1800, beat=self.beat, clock=clock,
                       sleep=clock.sleep, out=self.lines.append, err=self.errors.append,
                       parent=lambda: next(parents))
        self.assertEqual(clock.slept, [PARENT_CHECK_SECONDS] * 3)
        self.assertEqual(self.beats, [])
        self.assertEqual(self.lines, [f"heartbeat loop for {self.tid} ended: the session that "
                                      "started it is gone (its parent process changed); "
                                      "the lease now runs out on its own"])

    def test_a_watched_loop_sleeps_in_slices_until_the_beat_is_due(self):
        clock = Clock(T0)
        heartbeat_loop(self.hive, self.tid, "bob", ttl=1800, beat=self.beat, clock=clock,
                       sleep=clock.sleep, out=self.lines.append, err=self.errors.append,
                       parent=lambda: 100, max_beats=1)
        self.assertLessEqual(max(clock.slept), 30)
        self.assertEqual(sum(clock.slept), 900)
        self.assertEqual(self.beats, [T0 + timedelta(seconds=900)])

    def test_session_ancestry_starts_at_the_parent(self):
        chain = session_ancestry()
        self.assertEqual(chain[0], os.getppid())
        if Path("/proc/self/stat").exists():
            self.assertIn(chain[-1], (0, 1))                   # up to init

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
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "--task", self.tid, "--loop"])
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

    def test_a_stop_during_a_beat_lets_the_beat_finish(self):
        # Grok review 1, M2: SIGTERM mid-publish must not cut the beat short;
        # the loop ends right after it. Outside a beat it ends the loop at once.
        clock, stop = Clock(T0 + timedelta(seconds=1000)), LoopStop()

        def beat(now):
            stop(signal.SIGTERM, None)                         # --stop arrives mid-beat
            return self.beat(now)

        heartbeat_loop(self.hive, self.tid, "bob", ttl=1800, beat=beat, clock=clock,
                       sleep=clock.sleep, out=self.lines.append, err=self.errors.append,
                       stop=stop)                              # no max_beats: the stop ends it
        self.assertEqual(self.beats, [T0 + timedelta(seconds=1000)])
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(clock.slept, [])
        with self.assertRaises(SystemExit) as cm:
            LoopStop()(signal.SIGTERM, None)                   # while it sleeps
        self.assertEqual(cm.exception.code, 0)

    def test_a_loop_lock_is_never_seen_without_its_pid(self):
        # Grok review 1, m1: a second --loop that looks while the first creates
        # its lock must find the pid, never an empty file it would remove.
        path = state_dir(self.hive) / f"rip-swarm-heartbeat-{self.tid}.pid"
        seen, real_open, real_link = [], os.open, os.link

        def racer():
            if path.exists() and not seen:
                seen.append("started")
                try:
                    release_loop_lock(acquire_loop_lock(self.hive, self.tid))
                except LoopRunning:
                    seen[0] = "refused"

        def spy_open(*a, **k):
            fd = real_open(*a, **k)
            racer()
            return fd

        def spy_link(*a, **k):
            real_link(*a, **k)
            racer()

        with mock.patch("os.open", spy_open), mock.patch("os.link", spy_link):
            lock = acquire_loop_lock(self.hive, self.tid)
        self.assertEqual(seen, ["refused"])
        self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))
        release_loop_lock(lock)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir() if "heartbeat" in p.name), [])

    def test_stop_reports_a_loop_that_does_not_exit(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", code, "--task", self.tid, "--loop"])
        try:
            time.sleep(0.5)                                    # the handler is installed
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")
            with self.assertRaisesRegex(LoopStopTimeout,
                                        f"heartbeat loop {proc.pid} for {self.tid} did not stop"):
                stop_loop(self.hive, self.tid, timeout=0.5)
            self.assertIsNone(proc.poll())
            self.assertTrue(path.exists())                     # it still runs: the lock stays
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

    def test_stop_never_kills_the_loop_of_another_task(self):
        # Grok review 2, n1: a stale pid reused by another task's loop is not ours.
        other = create_task(self.hive, title="u", created_by="op", now=T0)["id"]
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "--task", other, "--loop"])
        try:
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")                 # a stale, reused pid
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"no heartbeat loop running for {self.tid}")
            self.assertIsNone(proc.poll())
            path = acquire_loop_lock(self.hive, self.tid)                    # nor does it block ours
            path.write_text(str(proc.pid), encoding="utf-8")
            release_loop_lock(acquire_loop_lock(self.hive, self.tid))
        finally:
            proc.kill()
            proc.wait()

    def test_a_task_given_with_an_equals_sign_is_its_loop(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 f"--task={self.tid}", "--loop"])
        try:
            path = acquire_loop_lock(self.hive, self.tid)
            path.write_text(str(proc.pid), encoding="utf-8")
            self.assertEqual(stop_loop(self.hive, self.tid),
                             f"stopped heartbeat loop {proc.pid} for {self.tid}")
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

    def test_stop_waits_for_a_loop_mid_beat_to_finish_and_exit(self):
        # Grok review 1, M2: `--stop` sent mid-publish returns only once the
        # loop process has exited, after the beat it was in has finished.
        tid = create_task(self.hive, title="u", created_by="op", now=T0)["id"]
        try_claim(self.hive, tid, "bob", "grok", now_utc(), 60)   # under half left: beats at once
        marks = Path(self.tmp.name) / "marks"
        marks.mkdir()
        child = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "import rip_swarm.cli as cli\n"
            "marks, real = Path(sys.argv[1]), cli._heartbeat\n"
            "def slow(*a):\n"
            "    (marks / 'start').write_text('')\n"
            "    time.sleep(2)\n"
            "    out = real(*a)\n"
            "    (marks / 'end').write_text('')\n"
            "    return out\n"
            "cli._heartbeat = slow\n"
            "sys.exit(cli.main(sys.argv[2:]))\n"
        )
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "skills" / "rip-swarm"))
        proc = subprocess.Popen(
            [sys.executable, "-c", child, str(marks), "heartbeat", "--hive", str(self.hive),
             "--task", tid, "--agent", "bob", "--loop", "--local"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not (marks / "start").exists():
                if proc.poll() is not None:
                    self.fail(proc.communicate())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.assertEqual(stop_loop(self.hive, tid), f"stopped heartbeat loop {proc.pid} for {tid}")
            self.assertIsNotNone(proc.poll())                  # exited before stop_loop returned
            self.assertTrue((marks / "end").exists())          # the beat finished
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn(f"heartbeat {tid}", out)
            self.assertEqual(stop_loop(self.hive, tid), f"no heartbeat loop running for {tid}")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def test_a_loop_ends_when_the_session_that_started_it_dies(self):
        """Final review I2, end to end: a harness runs the loop through a shell
        of its own, and that shell outlives the harness when it is killed
        hard. The loop still notices (its ancestry changed), releases its
        lock and exits 0."""
        if not Path("/proc/self/stat").exists():
            self.skipTest("needs /proc")
        tid = create_task(self.hive, title="u", created_by="op", now=T0)["id"]
        try_claim(self.hive, tid, "bob", "grok", now_utc(), 3600)   # no beat due for 30 min
        work = Path(self.tmp.name)
        loop = (
            "import sys\n"
            "import rip_swarm.lease as lease\n"
            "lease.PARENT_CHECK_SECONDS = 0.1\n"
            "import rip_swarm.cli as cli\n"
            "sys.exit(cli.main(sys.argv[1:]))\n"
        )
        shell = (f"{sys.executable} -c '{loop}' heartbeat --hive '{self.hive}' --task {tid} "
                 f"--agent bob --loop --local >'{work}/out' 2>&1; echo $? >'{work}/rc'")
        session = ("import subprocess, sys, time\n"
                   "subprocess.Popen(['/bin/sh', '-c', sys.argv[1]])\n"
                   "time.sleep(60)\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "skills" / "rip-swarm"))
        harness = subprocess.Popen([sys.executable, "-c", session, shell], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid_file = state_dir(self.hive) / f"rip-swarm-heartbeat-{tid}.pid"
        try:
            deadline = time.monotonic() + 20
            while not pid_file.exists():
                self.assertLess(time.monotonic(), deadline, "the loop never started")
                time.sleep(0.05)
            time.sleep(0.5)                                    # it has recorded its session
            self.assertFalse((work / "rc").exists())           # still running
            harness.kill()                                     # the session dies hard
            harness.wait()
            while not (work / "rc").exists():
                self.assertLess(time.monotonic(), deadline + 20, "the loop kept running")
                time.sleep(0.05)
            time.sleep(0.2)
            self.assertEqual((work / "rc").read_text().strip(), "0")
            self.assertIn(f"heartbeat loop for {tid} ended: the session that started it is gone",
                          (work / "out").read_text())
            self.assertFalse(pid_file.exists())
        finally:
            if harness.poll() is None:
                harness.kill()
                harness.wait()
            try:                                               # never leak a loop on failure
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def test_stop_with_nothing_running(self):
        rc, out, _ = self.run_cli("heartbeat", "--hive", self.hive, "--task", self.tid,
                                  "--agent", "bob", "--stop")
        self.assertEqual((rc, out), (0, f"no heartbeat loop running for {self.tid}"))
