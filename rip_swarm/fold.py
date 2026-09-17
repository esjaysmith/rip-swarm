from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rip_swarm.io import read_json
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import parse_z

_ACTIVE_NAME = re.compile(r"[^.]+\.json")


class Free:
    pass


@dataclass(frozen=True)
class Holder:
    task_id: str
    agent: str
    claim_id: str
    expires_at: datetime
    path: Path


@dataclass(frozen=True)
class Expired:
    task_id: str
    agent: str
    claim_id: str
    expires_at: datetime
    path: Path


@dataclass(frozen=True)
class Corrupt:
    """An active claim path that cannot be folded: bad JSON, empty, missing
    required fields, or a body whose task_id disagrees with its filename."""

    task_id: str
    path: Path
    error: str


def _from_doc(task_id: str, path: Path, doc: dict, now: datetime) -> Holder | Expired:
    expires_at = parse_z(doc["expires_at"])
    cls = Expired if expires_at <= now else Holder
    return cls(
        task_id=task_id,
        agent=doc["agent"],
        claim_id=doc.get("claim_id", ""),
        expires_at=expires_at,
        path=path,
    )


def _fold_path(task_id: str, path: Path, now: datetime) -> Holder | Expired | Corrupt:
    try:
        doc = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return Corrupt(task_id=task_id, path=path, error=f"{type(e).__name__}: {e}")
    body_id = doc.get("task_id")
    if body_id is not None and body_id != task_id:
        return Corrupt(
            task_id=task_id,
            path=path,
            error=f"task_id mismatch: file {task_id!r} vs body {body_id!r}",
        )
    for field in ("expires_at", "agent"):
        if not isinstance(doc.get(field), str) or not doc[field]:
            return Corrupt(task_id=task_id, path=path, error=f"missing {field}")
    try:
        return _from_doc(task_id, path, doc, now)
    except (ValueError, TypeError) as e:
        return Corrupt(task_id=task_id, path=path, error=f"{type(e).__name__}: {e}")


def active_holder(
    hive: Path, task_id: str, now: datetime
) -> Holder | Free | Expired | Corrupt:
    path = HivePaths(hive).claim(task_id)
    if not path.exists():
        return Free()
    return _fold_path(task_id, path, now)


def _active_paths(hive: Path) -> list[Path]:
    claims_dir = HivePaths(hive).claims
    if not claims_dir.is_dir():
        return []
    return sorted(
        (p for p in claims_dir.glob("*.json") if _ACTIVE_NAME.fullmatch(p.name)),
        key=lambda p: p.name,
    )


def active_set(hive: Path, now: datetime) -> dict[str, Holder]:
    """Live holders keyed by the *filename* stem - the filename is the lock."""
    out: dict[str, Holder] = {}
    for path in _active_paths(hive):
        rec = _fold_path(path.stem, path, now)
        if isinstance(rec, Holder):
            out[rec.task_id] = rec
    return out


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def corrupt_claims(hive: Path) -> list[Corrupt]:
    """Every active claim path that cannot be folded, sorted by task_id.

    Corruption is a property of the file, not of the clock, so the reference
    time is irrelevant here - any aware datetime folds the same set.
    """
    out: list[Corrupt] = []
    for path in _active_paths(hive):
        rec = _fold_path(path.stem, path, _EPOCH)
        if isinstance(rec, Corrupt):
            out.append(rec)
    return out


def open_claim_count(hive: Path, agent: str, now: datetime) -> int:
    return sum(
        1
        for task_id, holder in active_set(hive, now).items()
        if holder.agent == agent and task_id != "orchestrator"
    )
