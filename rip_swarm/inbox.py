# rip_swarm/inbox.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rip_swarm.ids import new_task_id
from rip_swarm.io import excl_create_json, read_json
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import format_z, now_utc


class InboxError(ValueError):
    pass


def create_task(
    hive: Path,
    *,
    title: str,
    created_by: str,
    body: str | None = None,
    task_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    title = title.strip()
    if not title:
        raise InboxError("title is required")
    if not created_by.strip():
        raise InboxError("created_by is required")
    ts = now or now_utc()
    tid = task_id or new_task_id(ts)
    doc = {
        "id": tid,
        "title": title,
        "created_at": format_z(ts),
        "created_by": created_by,
    }
    if body is not None:
        doc["body"] = body
    excl_create_json(HivePaths(hive).inbox_task(tid), doc)
    return doc


def read_task(hive: Path, task_id: str) -> dict:
    return read_json(HivePaths(hive).inbox_task(task_id))
