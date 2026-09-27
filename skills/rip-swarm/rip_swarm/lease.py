# rip_swarm/lease.py — keep one lease alive while the agent works outside `wait`
# (execution proposals §3)
from __future__ import annotations

import os
import signal
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied
from rip_swarm.fold import Holder, active_holder
from rip_swarm.state import _alive, _read_pid, state_dir
from rip_swarm.timeutil import parse_duration

RETRY_SECONDS = 30


class LoopRunning(Exception):
    def __init__(self, task_id: str, pid: int):
        super().__init__(f"heartbeat loop already running for {task_id} (pid {pid})")
        self.pid = pid


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
) -> None:
    """Heartbeat `task_id` each time half of its lease is gone, until killed.

    The lease is re-read before every sleep, so a heartbeat from another helper
    in between only postpones the next one. `ClaimDenied` (the lease is gone)
    ends the loop. Any other failure, such as a push that did not go through,
    is printed and retried after RETRY_SECONDS while the lease is still alive.
    `max_beats` bounds the loop for tests only."""
    beats = 0
    while max_beats is None or beats < max_beats:
        now = clock()
        rec = active_holder(hive, task_id, now)
        if isinstance(rec, Holder) and rec.agent == agent:
            wait = (rec.expires_at - now).total_seconds() - ttl / 2
            if wait > 0:
                sleep(wait)
                continue
        try:
            line = beat(clock())
        except ClaimDenied:
            raise
        except Exception as e:
            err(f"heartbeat {task_id} failed, retrying in {RETRY_SECONDS}s: {e}")
            sleep(RETRY_SECONDS)
            continue
        beats += 1
        out(line)


def _pid_path(hive: Path, task_id: str) -> Path:
    return state_dir(hive) / f"rip-swarm-heartbeat-{task_id}.pid"


def _is_loop(pid: int) -> bool:
    """A stale pid file may name a pid the system has since reused. Where
    /proc exists, only a process started with --loop is ours to stop."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return b"--loop" in cmdline.split(b"\0")


def acquire_loop_lock(hive: Path, task_id: str) -> Path:
    path = _pid_path(hive, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid = _read_pid(path)
            if pid is not None and _alive(pid) and (pid == os.getpid() or _is_loop(pid)):
                raise LoopRunning(task_id, pid) from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return path
    raise LoopRunning(task_id, _read_pid(path) or 0)


def release_loop_lock(path: Path) -> None:
    if _read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)


def stop_loop(hive: Path, task_id: str) -> str:
    path = _pid_path(hive, task_id)
    pid = _read_pid(path)
    if pid is None or not _alive(pid) or not _is_loop(pid):
        path.unlink(missing_ok=True)
        return f"no heartbeat loop running for {task_id}"
    os.kill(pid, signal.SIGTERM)
    path.unlink(missing_ok=True)
    return f"stopped heartbeat loop {pid} for {task_id}"
