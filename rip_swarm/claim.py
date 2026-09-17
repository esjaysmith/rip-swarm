# rip_swarm/claim.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rip_swarm.audit import append_claim_audit
from rip_swarm.ids import new_claim_id
from rip_swarm.io import ExclExistsError, atomic_write_json, excl_create_json, read_json
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import add_seconds, format_z, parse_z


class ClaimError(Exception):
    pass


class ClaimDenied(ClaimError):
    """Refusal. commit=True: op wrote audit that publish should push, then re-raise."""

    def __init__(self, message: str = "", *, commit: bool = False):
        super().__init__(message)
        self.commit = commit


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def tombstone_claim(path: Path, action: str, now: datetime) -> Path:
    dest = path.with_name(f"{path.stem}.{action}.{_stamp(now)}.json")
    path.rename(dest)
    return dest


def _read_active(hive: Path, task_id: str) -> tuple[Path, dict] | None:
    path = HivePaths(hive).claim(task_id)
    if not path.exists():
        return None
    return path, read_json(path)


def try_claim(
    hive: Path,
    task_id: str,
    agent: str,
    harness: str,
    now: datetime,
    lease_seconds: int,
    note: str | None = None,
) -> dict:
    paths = HivePaths(hive)
    if task_id != "orchestrator" and not paths.inbox_task(task_id).exists():
        raise ClaimDenied(f"no inbox task {task_id}")
    active = _read_active(hive, task_id)
    if active:
        path, doc = active
        exp = parse_z(doc["expires_at"])
        if exp > now:
            if doc["agent"] == agent:
                return doc
            raise ClaimDenied(f"held by {doc['agent']} until {doc['expires_at']}")
        tombstone_claim(path, "expired", now)
        append_claim_audit(hive, action="expired", claim_doc=doc, now=now)
    body = {
        "task_id": task_id,
        "claim_id": new_claim_id(now),
        "agent": agent,
        "harness": harness,
        "exclusive": True,
        "created_at": format_z(now),
        "expires_at": format_z(add_seconds(now, lease_seconds)),
        "note": note,
    }
    try:
        excl_create_json(paths.claim(task_id), body)
    except ExclExistsError as e:
        raise ClaimDenied(f"lost race creating claim for {task_id}") from e
    append_claim_audit(hive, action="claim", claim_doc=body, now=now)
    return body


def _require_holder(hive: Path, task_id: str, agent: str, now: datetime) -> tuple[Path, dict]:
    active = _read_active(hive, task_id)
    if not active:
        raise ClaimDenied(f"no active claim for {task_id}")
    path, doc = active
    if doc["agent"] != agent:
        raise ClaimDenied(f"held by {doc['agent']}")
    if parse_z(doc["expires_at"]) <= now:
        raise ClaimDenied("claim expired")
    return path, doc


def heartbeat(hive: Path, task_id: str, agent: str, now: datetime, lease_seconds: int) -> dict:
    path, doc = _require_holder(hive, task_id, agent, now)
    doc = dict(doc)
    doc["expires_at"] = format_z(add_seconds(now, lease_seconds))
    atomic_write_json(path, doc)
    append_claim_audit(hive, action="heartbeat", claim_doc=doc, now=now)
    return doc


def complete(
    hive: Path,
    task_id: str,
    agent: str,
    now: datetime,
    result_ref: str,
    note: str | None = None,
) -> dict:
    if task_id == "orchestrator":
        raise ClaimDenied("orchestrator cannot be completed; use release")
    if not result_ref or not result_ref.strip():
        raise ClaimDenied("complete requires result_ref")
    path, doc = _require_holder(hive, task_id, agent, now)
    doc = dict(doc)
    doc["result_ref"] = result_ref.strip()
    if note is not None:
        doc["note"] = note
    atomic_write_json(path, doc)
    tombstone_claim(path, "complete", now)
    append_claim_audit(hive, action="complete", claim_doc=doc, now=now, result_ref=doc["result_ref"])
    return doc


def release(hive: Path, task_id: str, agent: str, now: datetime, note: str | None = None) -> dict:
    path, doc = _require_holder(hive, task_id, agent, now)
    if note is not None:
        doc = dict(doc)
        doc["note"] = note
        atomic_write_json(path, doc)
    tombstone_claim(path, "release", now)
    append_claim_audit(hive, action="release", claim_doc=doc, now=now)
    return doc


def reject(hive: Path, task_id: str, agent: str, now: datetime, note: str | None = None) -> dict:
    if task_id == "orchestrator":
        raise ClaimDenied("orchestrator cannot be rejected; use release")
    path, doc = _require_holder(hive, task_id, agent, now)
    if note is not None:
        doc = dict(doc)
        doc["note"] = note
        atomic_write_json(path, doc)
    tombstone_claim(path, "reject", now)
    append_claim_audit(hive, action="reject", claim_doc=doc, now=now)
    return doc
