# rip_swarm/inbox.py
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from rip_swarm.ids import new_task_id
from rip_swarm.io import excl_create_json, read_json
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import format_z, now_utc


class InboxError(ValueError):
    pass


_TASK_ID = re.compile(r"task_[0-9A-HJKMNP-TV-Z]{26}")
_RESERVED = frozenset({"orchestrator", "CURRENT", "registry", "default"})


def validate_task_id(task_id: str) -> str:
    """Accept only a canonical `task_<ULID>` id; reject reserved literals."""
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise InboxError(f"invalid task_id {task_id!r}")
    if task_id in _RESERVED:
        raise InboxError(f"reserved task_id {task_id!r}")
    return task_id


def create_task(
    hive: Path,
    *,
    title: str,
    created_by: str,
    body: str | None = None,
    task_id: str | None = None,
    now: datetime | None = None,
    after: list[str] | None = None,
    fixes: str | None = None,
) -> dict:
    title = title.strip()
    if not title:
        raise InboxError("title is required")
    if not created_by.strip():
        raise InboxError("created_by is required")
    deps = list(dict.fromkeys(after or []))
    for ref in [*deps, *([fixes] if fixes else [])]:
        validate_task_id(ref)
        if not HivePaths(hive).inbox_task(ref).is_file():
            raise InboxError(f"unknown task {ref}: post it before tasks that refer to it")
    ts = now or now_utc()
    tid = validate_task_id(task_id) if task_id is not None else new_task_id(ts)
    doc = {
        "id": tid,
        "title": title,
        "created_at": format_z(ts),
        "created_by": created_by,
    }
    if body is not None:
        doc["body"] = body
    if deps:
        doc["after"] = deps
    if fixes:
        doc["fixes"] = fixes
    excl_create_json(HivePaths(hive).inbox_task(tid), doc)
    return doc


def read_task(hive: Path, task_id: str) -> dict:
    return read_json(HivePaths(hive).inbox_task(task_id))
