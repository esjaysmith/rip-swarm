# rip_swarm/lease.py — keep one lease alive while the agent works outside `wait`
# (execution proposals §3)
from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied
from rip_swarm.fold import Holder, active_holder
from rip_swarm.state import _alive, _read_pid, create_pid_file, state_dir
from rip_swarm.timeutil import parse_duration

RETRY_SECONDS = 30
STOP_TIMEOUT = 60.0


class LoopRunning(Exception):
    def __init__(self, task_id: str, pid: int):
        super().__init__(f"heartbeat loop already running for {task_id} (pid {pid})")
        self.pid = pid


class LoopStopTimeout(Exception):
    def __init__(self, task_id: str, pid: int, timeout: float):
        super().__init__(
            f"heartbeat loop {pid} for {task_id} did not stop within {timeout:g}s; "
            "it may still be publishing"
        )


class LoopStop:
    """The loop's SIGTERM handler (from `--stop`). While a beat runs, the
    signal is only recorded and the loop ends right after the beat, so a
    publish is never cut short (a SystemExit would skip its reset). Outside a
    beat it ends the loop at once."""

    def __init__(self) -> None:
        self.in_beat = False
        self.requested = False

    def __call__(self, signum: int, frame: object) -> None:
        self.requested = True
        if not self.in_beat:
            raise SystemExit(0)


def lease_ttl(profile: dict, task_id: str) -> int:
    key = "orchestrator_lease_ttl" if task_id == "orchestrator" else "worker_lease_ttl"
    return parse_duration(profile[key])


def heartbeat_loop(
    hive: Path,
    task_id: str,
    agent: str,
    *,
    ttl: int,
    beat: Callable[[datetime], str],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    out: Callable[[str], None],
    err: Callable[[str], None],
    max_beats: int | None = None,
    stop: LoopStop | None = None,
) -> None:
    """Heartbeat `task_id` each time half of its lease is gone, until killed.

    The lease is re-read before every sleep, so a heartbeat from another helper
    in between only postpones the next one. `ClaimDenied` (the lease is gone)
    ends the loop. Any other failure, such as a push that did not go through,
    is printed and retried after RETRY_SECONDS while the lease is still alive.
    `stop`, when given, is the SIGTERM handler: a stop that arrives during a
    beat ends the loop after that beat. `max_beats` bounds the loop for tests
    only."""
    beats = 0
    while max_beats is None or beats < max_beats:
        now = clock()
        rec = active_holder(hive, task_id, now)
        if isinstance(rec, Holder) and rec.agent == agent:
            wait = (rec.expires_at - now).total_seconds() - ttl / 2
            if wait > 0:
                sleep(wait)
                continue
        if stop is not None:
            stop.in_beat = True
        try:
            line = beat(clock())
        except ClaimDenied:
            raise
        except Exception as e:
            err(f"heartbeat {task_id} failed, retrying in {RETRY_SECONDS}s: {e}")
            if stop is not None and stop.requested:
                return
            sleep(RETRY_SECONDS)
            continue
        finally:
            if stop is not None:
                stop.in_beat = False
        beats += 1
        out(line)
        if stop is not None and stop.requested:
            return


def _pid_path(hive: Path, task_id: str) -> Path:
    return state_dir(hive) / f"rip-swarm-heartbeat-{task_id}.pid"


def _is_loop(pid: int, task_id: str) -> bool:
    """A stale pid file may name a pid the system has since reused, even by
    another task's loop. Where /proc exists, only a process started with
    --loop and with --task `task_id` is ours to stop."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    args, task = cmdline.split(b"\0"), task_id.encode()
    ours = b"--task=" + task in args or any(
        flag == b"--task" and value == task for flag, value in zip(args, args[1:]))
    return b"--loop" in args and ours


def acquire_loop_lock(hive: Path, task_id: str) -> Path:
    path = _pid_path(hive, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        if create_pid_file(path):                  # never seen without its pid
            return path
        pid = _read_pid(path)
        if pid is not None and _alive(pid) and (pid == os.getpid() or _is_loop(pid, task_id)):
            raise LoopRunning(task_id, pid)
        path.unlink(missing_ok=True)
    raise LoopRunning(task_id, _read_pid(path) or 0)


def release_loop_lock(path: Path) -> None:
    if _read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)


def _exited(pid: int) -> bool:
    """Gone, or a zombie its parent has not reaped yet: either way it runs
    no more code."""
    if not _alive(pid):
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[:1] == ["Z"]


def stop_loop(hive: Path, task_id: str, *, timeout: float = STOP_TIMEOUT) -> str:
    """SIGTERM the loop and return only once it has exited, so the caller's
    next hive write never meets the loop's publish half done. A beat in
    progress finishes first (see LoopStop)."""
    path = _pid_path(hive, task_id)
    pid = _read_pid(path)
    if pid is None or not _alive(pid) or not _is_loop(pid, task_id):
        path.unlink(missing_ok=True)
        return f"no heartbeat loop running for {task_id}"
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while not _exited(pid):
        if time.monotonic() >= deadline:
            raise LoopStopTimeout(task_id, pid, timeout)
        time.sleep(0.1)
    if _read_pid(path) == pid:                     # killed before its own cleanup
        path.unlink(missing_ok=True)
    return f"stopped heartbeat loop {pid} for {task_id}"
