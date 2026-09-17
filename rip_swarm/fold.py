from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rip_swarm.io import read_json
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import parse_z

_ACTIVE_NAME = re.compile(r"^[^.]+\.json$")


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


def _from_doc(path: Path, doc: dict, now: datetime) -> Holder | Expired:
    expires_at = parse_z(doc["expires_at"])
    cls = Expired if expires_at <= now else Holder
    return cls(
        task_id=doc["task_id"],
        agent=doc["agent"],
        claim_id=doc["claim_id"],
        expires_at=expires_at,
        path=path,
    )


def active_holder(hive: Path, task_id: str, now: datetime) -> Holder | Free | Expired:
    path = HivePaths(hive).claim(task_id)
    if not path.exists():
        return Free()
    return _from_doc(path, read_json(path), now)


def active_set(hive: Path, now: datetime) -> dict[str, Holder]:
    claims_dir = HivePaths(hive).claims
    if not claims_dir.is_dir():
        return {}
    out: dict[str, Holder] = {}
    for path in claims_dir.glob("*.json"):
        if not _ACTIVE_NAME.match(path.name):
            continue
        rec = active_holder(hive, path.stem, now)
        if isinstance(rec, Holder):
            out[rec.task_id] = rec
    return out


def open_claim_count(hive: Path, agent: str, now: datetime) -> int:
    return sum(
        1
        for task_id, holder in active_set(hive, now).items()
        if holder.agent == agent and task_id != "orchestrator"
    )
