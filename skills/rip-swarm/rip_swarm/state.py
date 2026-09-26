# rip_swarm/state.py — per-agent local state and the wait lock (spec §7.1, §7.5)
from __future__ import annotations

import json
import os
from pathlib import Path

from rip_swarm.board import is_accepted, list_tombstones
from rip_swarm.io import atomic_write_json
from rip_swarm.messages import newest_cursor

STATE_FILE = "rip-swarm-state.json"
LOCK_FILE = "rip-swarm-wait.lock"


class WaitRunning(Exception):
    def __init__(self, pid: int):
        super().__init__(f"wait already running (pid {pid})")
        self.pid = pid


def state_dir(hive: Path) -> Path:
    """Inside the clone's `.git` (never committed); a plain dir for --no-git hives."""
    git_dir = Path(hive) / ".git"
    return git_dir if git_dir.is_dir() else Path(hive) / ".rip-swarm-local"


def _empty(agent: str) -> dict:
    return {
        "agent": agent,
        "messages_cursor": None,
        "seen_open": [],
        "seen_tombstones": [],
        "seen_expiries": [],
        "held": [],
        "idle_reported": [],
    }


def seed_state(hive: Path, agent: str) -> dict:
    """Old messages and settled tombstones are seen; bare completes are not, and
    the open board is not (spec §7.5)."""
    state = _empty(agent)
    state["messages_cursor"] = newest_cursor(hive)
    stones = list_tombstones(hive)
    rejected = {s.task_id for s in stones if s.action == "reject"}
    for stone in stones:
        if stone.action == "complete" and not (
            is_accepted(hive, stone.task_id) or stone.task_id in rejected
        ):
            continue
        state["seen_tombstones"].append(stone.name)
    return state


def load_state(hive: Path, agent: str) -> dict | None:
    path = state_dir(hive) / STATE_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("agent") != agent:
        return None
    state = _empty(agent)
    state.update({key: doc[key] for key in state if key in doc})
    return state


def save_state(hive: Path, state: dict) -> None:
    atomic_write_json(state_dir(hive) / STATE_FILE, state)


def ensure_state(hive: Path, agent: str) -> tuple[dict, bool]:
    state = load_state(hive, agent)
    if state is not None:
        return state, False
    state = seed_state(hive, agent)
    save_state(hive, state)
    return state, True


def mark_seen_open(hive: Path, agent: str, key: str) -> None:
    state = load_state(hive, agent)
    if state is None or key in state["seen_open"]:
        return
    state["seen_open"].append(key)
    save_state(hive, state)


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_wait_lock(hive: Path) -> Path:
    path = state_dir(hive) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid = _read_pid(path)
            if pid is not None and _alive(pid):
                raise WaitRunning(pid) from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return path
    raise WaitRunning(_read_pid(path) or 0)


def release_wait_lock(path: Path) -> None:
    if _read_pid(path) == os.getpid():
        path.unlink(missing_ok=True)
