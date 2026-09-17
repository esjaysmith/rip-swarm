# rip_swarm/orchestrator.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied, heartbeat, release, try_claim
from rip_swarm.io import atomic_write_json, read_json
from rip_swarm.outbox import write_message
from rip_swarm.paths import HivePaths
from rip_swarm.registry import require_agent
from rip_swarm.timeutil import parse_z


def read_current(hive: Path) -> dict | None:
    path = HivePaths(hive).current
    if not path.exists():
        return None
    return read_json(path)


def _read_orchestrator_claim(hive: Path) -> dict | None:
    path = HivePaths(hive).claim("orchestrator")
    if not path.exists():
        return None
    return read_json(path)


def orchestrator_state(hive: Path, now: datetime) -> dict:
    current = read_current(hive)
    claim = _read_orchestrator_claim(hive)
    present = current is not None
    if claim is not None:
        expired = parse_z(claim["expires_at"]) <= now
    elif current is not None and current.get("lease_expires_at"):
        expired = parse_z(current["lease_expires_at"]) <= now
    else:
        expired = False
    if current is None and claim is None:
        matches_claim = True
        agent = None
    elif current is not None and claim is not None:
        matches_claim = (
            current.get("agent") == claim.get("agent")
            and current.get("claim_id") == claim.get("claim_id")
            and not expired
        )
        agent = current.get("agent")
    else:
        matches_claim = False
        agent = (current or claim).get("agent")
    return {
        "agent": agent,
        "matches_claim": matches_claim,
        "expired": expired,
        "present": present,
    }


def current_matches_claim(hive: Path, now: datetime) -> bool:
    return orchestrator_state(hive, now)["matches_claim"]


def promote(
    hive: Path,
    *,
    agent: str,
    harness: str,
    now: datetime,
    lease_seconds: int,
    reason: str,
    allow_self_promote: bool,
    operators: list[str],
    by: str | None = None,
) -> dict:
    by = by or agent
    require_agent(hive, agent)
    by_rec = require_agent(hive, by)
    if not (by in operators or (by == agent and allow_self_promote)):
        raise ClaimDenied(f"{by} cannot promote {agent}")
    claim = try_claim(hive, "orchestrator", agent, harness, now, lease_seconds)
    current = {
        "agent": agent,
        "harness": harness,
        "lease_expires_at": claim["expires_at"],
        "reason": reason,
        "claim_id": claim["claim_id"],
    }
    atomic_write_json(HivePaths(hive).current, current)
    write_message(
        hive,
        agent=by,
        harness=by_rec["harness"],
        type="promote",
        to="*",
        body={
            "agent": agent,
            "harness": harness,
            "by": by,
            "reason": reason,
            "claim_id": claim["claim_id"],
        },
        now=now,
    )
    return current


def heartbeat_orchestrator(
    hive: Path,
    *,
    agent: str,
    now: datetime,
    lease_seconds: int,
) -> dict:
    claim = heartbeat(hive, "orchestrator", agent, now, lease_seconds)
    current = read_current(hive)
    if current is None:
        current = {
            "agent": claim["agent"],
            "harness": claim["harness"],
            "reason": "",
            "claim_id": claim["claim_id"],
        }
    else:
        current = dict(current)
    current["lease_expires_at"] = claim["expires_at"]
    atomic_write_json(HivePaths(hive).current, current)
    return current


def release_orchestrator(
    hive: Path,
    *,
    agent: str,
    now: datetime,
    note: str | None = None,
) -> dict:
    doc = release(hive, "orchestrator", agent, now, note)
    HivePaths(hive).current.unlink(missing_ok=True)
    return doc
