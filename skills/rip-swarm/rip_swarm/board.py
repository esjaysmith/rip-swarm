# rip_swarm/board.py — derived per-task state (spec §7.3, §7.4). Read-only.
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rip_swarm.fold import Corrupt, Expired, Holder, active_holder
from rip_swarm.inbox import InboxError, validate_task_id
from rip_swarm.io import read_json
from rip_swarm.paths import HivePaths

_TOMBSTONE = re.compile(
    r"(?P<task>[^.]+)\.(?P<action>complete|release|reject|expired)"
    r"\.(?P<stamp>\d{8}T\d{6}Z)(?:-\d+)?\.json"
)


@dataclass(frozen=True)
class Tombstone:
    name: str
    task_id: str
    action: str
    stamp: str
    path: Path

    def doc(self) -> dict | None:
        try:
            return read_json(self.path)
        except (OSError, ValueError):
            return None


def list_tombstones(hive: Path) -> list[Tombstone]:
    claims = HivePaths(hive).claims
    if not claims.is_dir():
        return []
    out: list[Tombstone] = []
    for path in sorted(claims.glob("*.json"), key=lambda p: p.name):
        match = _TOMBSTONE.fullmatch(path.name)
        if match:
            out.append(
                Tombstone(path.name, match["task"], match["action"], match["stamp"], path)
            )
    return out


def is_accepted(hive: Path, task_id: str) -> bool:
    return HivePaths(hive).accepted_record(task_id).is_file()


@dataclass(frozen=True)
class TaskView:
    task_id: str
    title: str
    created_at: str
    after: tuple[str, ...]
    fixes: str | None
    claim: str  # "none" | "live" | "expired" | "corrupt"
    holder: str | None
    completed: bool
    rejected: bool
    accepted: bool
    returns: int  # release + expired tombstones
    blocked_by: tuple[str, ...]

    @property
    def generation(self) -> int:
        return self.returns + (1 if self.claim == "expired" else 0)

    @property
    def is_open(self) -> bool:
        return (
            not self.completed
            and not self.rejected
            and not self.blocked_by
            and self.claim in ("none", "expired")
        )

    @property
    def awaiting_acceptance(self) -> bool:
        return self.completed and not self.accepted and not self.rejected

    @property
    def settled(self) -> bool:
        return self.accepted or self.rejected


def _inbox_docs(hive: Path) -> dict[str, dict]:
    inbox = HivePaths(hive).inbox
    out: dict[str, dict] = {}
    if not inbox.is_dir():
        return out
    for path in sorted(inbox.glob("task_*.json")):
        try:
            validate_task_id(path.stem)
            out[path.stem] = read_json(path)
        except (InboxError, OSError, ValueError):
            continue
    return out


def _claim_state(rec: object) -> tuple[str, str | None]:
    if isinstance(rec, Holder):
        return "live", rec.agent
    if isinstance(rec, Expired):
        return "expired", rec.agent
    if isinstance(rec, Corrupt):
        return "corrupt", None
    return "none", None


def read_board(hive: Path, now: datetime) -> dict[str, TaskView]:
    docs = _inbox_docs(hive)
    actions: dict[str, list[str]] = {}
    for stone in list_tombstones(hive):
        actions.setdefault(stone.task_id, []).append(stone.action)
    accepted = {tid for tid in docs if is_accepted(hive, tid)}
    board: dict[str, TaskView] = {}
    for tid, doc in docs.items():
        claim, holder = _claim_state(active_holder(hive, tid, now))
        acts = actions.get(tid, [])
        after = tuple(str(x) for x in (doc.get("after") or ()))
        fixes = doc.get("fixes")
        board[tid] = TaskView(
            task_id=tid,
            title=str(doc.get("title", "")),
            created_at=str(doc.get("created_at", "")),
            after=after,
            fixes=fixes if isinstance(fixes, str) else None,
            claim=claim,
            holder=holder,
            completed="complete" in acts,
            rejected="reject" in acts,
            accepted=tid in accepted,
            returns=sum(1 for a in acts if a in ("release", "expired")),
            blocked_by=tuple(dep for dep in after if dep not in accepted),
        )
    return board


def fixers(board: dict[str, TaskView], task_id: str) -> list[TaskView]:
    return [view for _tid, view in sorted(board.items()) if view.fixes == task_id]


def finished_reason(hive: Path, task_id: str) -> str | None:
    if is_accepted(hive, task_id):
        return "already accepted"
    actions = {s.action for s in list_tombstones(hive) if s.task_id == task_id}
    if "complete" in actions:
        return "already completed"
    if "reject" in actions:
        return "rejected"
    return None


def blocked_by(hive: Path, task_id: str) -> list[str]:
    path = HivePaths(hive).inbox_task(task_id)
    if not path.is_file():
        return []
    after = read_json(path).get("after") or []
    return [str(dep) for dep in after if not is_accepted(hive, str(dep))]
