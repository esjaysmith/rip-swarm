# rip_swarm/acceptance.py — acceptance records and master reject (spec §7.4)
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from rip_swarm.audit import append_claim_audit
from rip_swarm.board import is_accepted, read_board
from rip_swarm.claim import ClaimDenied, _tombstone_candidates, tombstone_claim
from rip_swarm.fold import Corrupt, Expired, Holder, active_holder
from rip_swarm.ids import new_claim_id
from rip_swarm.inbox import InboxError, validate_task_id
from rip_swarm.io import ExclExistsError, excl_create_json, read_json, write_json_to_new_path
from rip_swarm.paths import HivePaths
from rip_swarm.registry import require_agent
from rip_swarm.timeutil import format_z

_SHA = re.compile(r"[0-9a-f]{7,64}")


def holds_baton(hive: Path, agent: str, now: datetime) -> bool:
    rec = active_holder(hive, "orchestrator", now)
    return isinstance(rec, Holder) and rec.agent == agent


def _require_master(hive: Path, agent: str, now: datetime) -> dict:
    rec = require_agent(hive, agent)
    if not holds_baton(hive, agent, now):
        raise ClaimDenied(f"{agent} does not hold a live orchestrator baton")
    return rec


def _require_task(hive: Path, task_id: str) -> None:
    try:
        validate_task_id(task_id)
    except InboxError as e:
        raise ClaimDenied(str(e)) from e
    if not HivePaths(hive).inbox_task(task_id).is_file():
        raise ClaimDenied(f"no inbox task {task_id}")


def accept_task(
    hive: Path,
    *,
    agent: str,
    task_id: str,
    integration_sha: str,
    via: Iterable[str] = (),
    now: datetime,
) -> dict:
    """Write the create-only `accepted/<T>.json`. Idempotent: an existing record
    is `{"task_id": T, "already": True}` and nothing is written."""
    _require_task(hive, task_id)
    _require_master(hive, agent, now)
    if is_accepted(hive, task_id):
        return {"task_id": task_id, "already": True}
    if not _SHA.fullmatch(integration_sha or ""):
        raise ValueError(f"--integration-sha must be a hex commit id, got {integration_sha!r}")
    via = list(dict.fromkeys(via))
    if via:
        for fixer in via:
            _require_task(hive, fixer)
            if not is_accepted(hive, fixer):
                raise ClaimDenied(f"via task {fixer} is not accepted")
    elif not read_board(hive, now)[task_id].completed:
        raise ClaimDenied(f"{task_id} has no complete tombstone")
    doc = {
        "task_id": task_id,
        "by": agent,
        "at": format_z(now),
        "integration_sha": integration_sha,
        "via": via,
    }
    try:
        excl_create_json(HivePaths(hive).accepted_record(task_id), doc)
    except ExclExistsError:
        return {"task_id": task_id, "already": True}
    return doc


def master_reject(
    hive: Path, *, agent: str, task_id: str, note: str | None, now: datetime
) -> dict:
    """The baton holder drops a task that has no live claim (spec §7.4)."""
    _require_task(hive, task_id)
    rec = _require_master(hive, agent, now)
    view = read_board(hive, now)[task_id]
    if view.rejected:
        return {"task_id": task_id, "already": True}
    if view.accepted:
        raise ClaimDenied(f"{task_id} is already accepted; it cannot be rejected")
    held = active_holder(hive, task_id, now)
    if isinstance(held, Holder):
        raise ClaimDenied(
            f"{task_id} is held by {held.agent} until {format_z(held.expires_at)}; "
            "the holder must release it or it must expire first"
        )
    if isinstance(held, Corrupt):
        raise ClaimDenied(f"{task_id} has a corrupt claim file: {held.error}")
    path = HivePaths(hive).claim(task_id)
    if isinstance(held, Expired):
        old = read_json(path)
        tombstone_claim(path, "expired", now)
        append_claim_audit(hive, action="expired", claim_doc=old, now=now)
    body = {
        "task_id": task_id,
        "claim_id": new_claim_id(now),
        "agent": agent,
        "harness": rec["harness"],
        "action": "reject",
        "note": note,
        "at": format_z(now),
        "expires_at": format_z(now),
    }
    for dest in _tombstone_candidates(path, "reject", now):
        if write_json_to_new_path(dest, body):
            break
    append_claim_audit(hive, action="reject", claim_doc=body, now=now)
    return body
