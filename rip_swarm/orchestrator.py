# rip_swarm/orchestrator.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rip_swarm.audit import append_claim_audit
from rip_swarm.claim import (
    ClaimDenied,
    claim_baton,
    heartbeat,
    release,
    tombstone_claim,
)
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
    agent_rec = require_agent(hive, agent)
    by_rec = require_agent(hive, by)
    # registry.yaml is the SoT for an agent's harness: refuse a caller that
    # disagrees, then take CURRENT/body.harness from the registry entry itself.
    if harness != agent_rec["harness"]:
        raise ClaimDenied(
            f"harness mismatch for {agent}: registry says "
            f"{agent_rec['harness']!r}, got {harness!r}"
        )
    harness = agent_rec["harness"]
    if not (by in operators or (by == agent and allow_self_promote)):
        raise ClaimDenied(f"{by} cannot promote {agent}")
    # The reason is persisted into the claim file's note so heartbeat can
    # repair CURRENT.json (an untrusted mirror) from the claim (the SoT).
    claim = claim_baton(hive, agent, harness, now, lease_seconds, reason)
    if claim.get("note") != reason:
        # Re-promoting a baton this agent already holds is idempotent on the
        # claim, so refresh the note to keep it the SoT for `reason`.
        claim = dict(claim)
        claim["note"] = reason
        atomic_write_json(HivePaths(hive).claim("orchestrator"), claim)
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
    # SoT wins: repair the reason from the claim's note, never from the
    # CURRENT.json mirror, which any writer may have tampered with.
    note = claim.get("note")
    reason = note if isinstance(note, str) else ""
    repaired = {
        "agent": claim["agent"],
        "harness": claim["harness"],
        "lease_expires_at": claim["expires_at"],
        "reason": reason,
        "claim_id": claim["claim_id"],
    }
    atomic_write_json(HivePaths(hive).current, repaired)
    return repaired


def release_orchestrator(
    hive: Path,
    *,
    agent: str,
    now: datetime,
    note: str | None = None,
) -> dict:
    """Drop the baton: tombstone the claim, delete CURRENT, append audit.

    The holder may release its own baton after the lease expires - otherwise an
    expired holder can never clear CURRENT.json and the mismatch is permanent.
    Non-holders are still refused; they take over through promote instead.
    """
    claim = _read_orchestrator_claim(hive)
    if claim is None:
        raise ClaimDenied("no active claim for orchestrator")
    if claim.get("agent") != agent:
        raise ClaimDenied(f"held by {claim.get('agent')}")
    if parse_z(claim["expires_at"]) <= now:
        doc = _release_expired(hive, claim, now, note)
    else:
        doc = release(hive, "orchestrator", agent, now, note)
    HivePaths(hive).current.unlink(missing_ok=True)
    return doc


def _release_expired(hive: Path, claim: dict, now: datetime, note: str | None) -> dict:
    path = HivePaths(hive).claim("orchestrator")
    doc = dict(claim)
    if note is not None:
        doc["note"] = note
        atomic_write_json(path, doc)
    tombstone_claim(path, "release", now)
    append_claim_audit(hive, action="release", claim_doc=doc, now=now)
    return doc
