# rip_swarm/waiter.py — the wait helper (spec §7)
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rip_swarm.board import TaskView, list_tombstones, read_board
from rip_swarm.claim import ClaimDenied, heartbeat
from rip_swarm.fold import Expired, Holder, active_holder
from rip_swarm.gitops import can_publish, publish_or_apply, sync
from rip_swarm.io import read_json
from rip_swarm.messages import unread_messages
from rip_swarm.orchestrator import heartbeat_orchestrator
from rip_swarm.paths import HivePaths
from rip_swarm.profile import load_profile
from rip_swarm.registry import require_agent
from rip_swarm.state import acquire_wait_lock, ensure_state, release_wait_lock, save_state
from rip_swarm.timeutil import format_z, now_utc, parse_duration, parse_z

_FINAL = ("complete", "release", "reject")


@dataclass(frozen=True)
class Wake:
    reason: str
    detail: str = ""

    def line(self) -> str:
        return f"wake {self.reason} {self.detail}".rstrip()


def held_claims(hive: Path, agent: str, now: datetime) -> list[dict]:
    """Live claims held by `agent`, the orchestrator baton included."""
    claims = HivePaths(hive).claims
    if not claims.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(claims.glob("*.json"), key=lambda p: p.name):
        if "." in path.stem:
            continue
        rec = active_holder(hive, path.stem, now)
        if isinstance(rec, Holder) and rec.agent == agent:
            out.append(
                {"task": rec.task_id, "claim_id": rec.claim_id,
                 "expires_at": format_z(rec.expires_at)}
            )
    return out


def _finished_by(hive: Path, task_id: str, claim_id: str) -> bool:
    """True when this claim ended with our own complete/release/reject."""
    for stone in list_tombstones(hive):
        if stone.task_id == task_id and stone.action in _FINAL:
            doc = stone.doc()
            if doc is not None and doc.get("claim_id") == claim_id:
                return True
    return False


def tick(
    hive: Path, agent: str, state: dict, now: datetime, *, idle_after: int
) -> Wake | None:
    """One evaluation of the local board for `agent` (no git). Records every
    event it reports in `state`, so each event is exactly one wake (§7.2)."""
    held = held_claims(hive, agent, now)
    live_ids = {h["claim_id"] for h in held}
    lost = [
        h["task"]
        for h in state.get("held", [])
        if h["claim_id"] not in live_ids and not _finished_by(hive, h["task"], h["claim_id"])
    ]
    state["held"] = [{"task": h["task"], "claim_id": h["claim_id"]} for h in held]
    if lost:
        return Wake("lease-lost", " ".join(lost))
    if unread_messages(hive, agent=agent, cursor=state.get("messages_cursor"), now=now):
        return Wake("message")
    board = read_board(hive, now)
    if any(h["task"] == "orchestrator" for h in held):
        return _master_tick(hive, board, state, now, idle_after)
    return _worker_tick(board, held, state)


def _worker_tick(board: dict[str, TaskView], held: list[dict], state: dict) -> Wake | None:
    if any(h["task"] != "orchestrator" for h in held):
        return None
    fresh: list[str] = []
    for tid, view in sorted(board.items()):
        key = f"{tid}#{view.generation}"
        if view.is_open and key not in state["seen_open"]:
            fresh.append(tid)
            state["seen_open"].append(key)
    return Wake("task-available", " ".join(fresh)) if fresh else None


def _master_tick(
    hive: Path, board: dict[str, TaskView], state: dict, now: datetime, idle_after: int
) -> Wake | None:
    for stone in list_tombstones(hive):
        if stone.task_id not in board or stone.name in state["seen_tombstones"]:
            continue
        state["seen_tombstones"].append(stone.name)
        return Wake("task-finished", f"{stone.task_id} {stone.action}")
    for tid, view in sorted(board.items()):
        if view.claim != "expired":
            continue
        key = f"{tid}#{view.generation}"
        if key not in state["seen_expiries"]:
            state["seen_expiries"].append(key)
            return Wake("task-finished", f"{tid} expired")
    if board and all(view.settled for view in board.values()):
        return Wake("all-complete")
    for tid, view in sorted(board.items()):
        if not view.is_open:
            continue
        key = f"{tid}#{view.generation}"
        if key in state["idle_reported"]:
            continue
        since = _open_since(hive, view, now)
        if since is not None and now - since >= timedelta(seconds=idle_after):
            state["idle_reported"].append(key)
            return Wake("idle-board", tid)
    return None


def _stamp_time(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _open_since(hive: Path, view: TaskView, now: datetime) -> datetime | None:
    """When the task last became open: created, returned, unblocked, or expired."""
    stamps: list[datetime] = []
    try:
        stamps.append(parse_z(view.created_at))
    except ValueError:
        pass
    for stone in list_tombstones(hive):
        if stone.task_id == view.task_id and stone.action in ("release", "expired"):
            stamps.append(_stamp_time(stone.stamp))
    for dep in view.after:
        try:
            stamps.append(parse_z(read_json(HivePaths(hive).accepted_record(dep))["at"]))
        except (OSError, ValueError, KeyError):
            pass
    rec = active_holder(hive, view.task_id, now)
    if isinstance(rec, Expired):
        stamps.append(rec.expires_at)
    return max(stamps) if stamps else None


def _heartbeat_due(hive: Path, agent: str, now: datetime, profile: dict) -> None:
    """Refresh any lease of ours with at most half of it left (§7.6). A lease
    lost in a race (ClaimDenied) is skipped: `wait` never exits 2, and the
    next tick either retries or reports lease-lost."""
    for held in held_claims(hive, agent, now):
        task = held["task"]
        key = "orchestrator_lease_ttl" if task == "orchestrator" else "worker_lease_ttl"
        ttl = parse_duration(profile[key])
        if (parse_z(held["expires_at"]) - now).total_seconds() > ttl / 2:
            continue
        if task == "orchestrator":
            def op(ttl=ttl) -> dict:
                return heartbeat_orchestrator(hive, agent=agent, now=now, lease_seconds=ttl)
        else:
            def op(task=task, ttl=ttl) -> dict:
                return heartbeat(hive, task, agent, now, ttl)
        try:
            publish_or_apply(
                hive, task_id=task, op=op, message=f"heartbeat {task}", agent=agent, now=now
            )
        except ClaimDenied:
            continue


def run_wait(
    hive: Path,
    agent: str,
    *,
    timeout: int,
    interval: int,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[Wake, list[str], list[str]]:
    clock = clock or now_utc
    sleep = sleep or time.sleep
    hive = Path(hive).resolve()
    require_agent(hive, agent)
    lock = acquire_wait_lock(hive)
    try:
        profile = load_profile(hive, None)
        idle_after = parse_duration(profile.get("idle_board_after", "10m"))
        notes: list[str] = []
        first = True
        start = clock()
        while True:
            if can_publish(hive):
                sync(hive)
            now = clock()
            _heartbeat_due(hive, agent, now, profile)
            # Re-read every iteration: a foreground `messages --new` or `release`
            # may have updated the state file while we slept.
            state, reseeded = ensure_state(hive, agent)
            if first and reseeded:
                notes.append("NOTE state re-seeded")
            first = False
            wake = tick(hive, agent, state, now, idle_after=idle_after)
            save_state(hive, state)
            if wake is None and (now - start).total_seconds() >= timeout:
                wake = Wake("timeout")
            if wake is not None:
                leases = [
                    f"held {h['task']} until {h['expires_at']}"
                    for h in held_claims(hive, agent, now)
                ]
                return wake, leases, notes
            sleep(interval)
    finally:
        release_wait_lock(lock)
