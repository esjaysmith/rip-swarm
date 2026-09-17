from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied, try_claim
from rip_swarm.fold import Holder, active_holder, open_claim_count
from rip_swarm.outbox import write_message
from rip_swarm.registry import require_agent
from rip_swarm.timeutil import parse_duration


def try_claim_with_policy(
    hive: Path,
    *,
    task_id: str,
    agent: str,
    harness: str,
    now: datetime,
    profile: dict,
    note: str | None = None,
) -> dict:
    require_agent(hive, agent)
    # allow_preempt is reserved; v0 never preempts unexpired claims.
    if task_id == "orchestrator":
        lease_seconds = parse_duration(profile["orchestrator_lease_ttl"])
    else:
        lease_seconds = parse_duration(profile["worker_lease_ttl"])
        limit = profile["budget"]["max_claims_open_per_agent"]
        observed = open_claim_count(hive, agent, now)
        rec = active_holder(hive, task_id, now)
        already_holder = isinstance(rec, Holder) and rec.agent == agent
        if observed >= limit and not already_holder:
            write_message(
                hive,
                agent=agent,
                harness=harness,
                type="budget_block",
                to="orchestrator",
                body={
                    "agent": agent,
                    "rule": "max_claims_open_per_agent",
                    "limit": limit,
                    "observed": observed,
                },
                now=now,
            )
            raise ClaimDenied(
                f"max_claims_open_per_agent: {observed} >= {limit}"
            )
    return try_claim(hive, task_id, agent, harness, now, lease_seconds, note)
