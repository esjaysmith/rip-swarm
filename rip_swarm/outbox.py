from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rip_swarm.audit import append_jsonl
from rip_swarm.ids import new_msg_id
from rip_swarm.io import excl_create_json
from rip_swarm.paths import HivePaths
from rip_swarm.registry import UnknownAgent, require_agent
from rip_swarm.timeutil import format_z

TYPES = ("task", "result", "ops", "promote", "budget_block", "note", "heartbeat")
TOPICS = ("tasks", "ops", "results")

_TO_LITERALS = frozenset({"orchestrator", "*"})
_BUDGET_BODY = frozenset({"agent", "rule", "limit", "observed"})
_PROMOTE_BODY = frozenset({"agent", "harness", "by", "reason", "claim_id"})


def default_topic(type: str) -> str:
    if type == "task":
        return "tasks"
    if type == "result":
        return "results"
    return "ops"


def write_message(
    hive: Path,
    *,
    agent: str,
    harness: str,
    type: str,
    to: str,
    body: dict,
    now: datetime,
    topic: str | None = None,
    ref: dict | None = None,
    profile: str = "default",
) -> dict:
    require_agent(hive, agent)
    if type not in TYPES:
        raise ValueError(f"unknown message type: {type!r}")
    if topic is None:
        topic = default_topic(type)
    elif topic not in TOPICS:
        raise ValueError(f"unknown message topic: {topic!r}")
    _check_to(hive, to)
    _check_body(type, body)
    doc = {
        "id": new_msg_id(now),
        "ts": format_z(now),
        "type": type,
        "topic": topic,
        "from": {"agent": agent, "harness": harness},
        "to": to,
        "ref": {"claim_id": None, "task_id": None, "in_reply_to": None} if ref is None else ref,
        "body": body,
        "profile": profile,
    }
    paths = HivePaths(hive)
    excl_create_json(paths.outbox(agent) / f"{doc['id']}.json", doc)
    append_jsonl(paths.messages_jsonl, doc)
    return doc


def _check_to(hive: Path, to: str) -> None:
    if to in _TO_LITERALS:
        return
    try:
        require_agent(hive, to)
    except UnknownAgent:
        raise ValueError(f"unknown message recipient: {to!r}") from None


def _check_body(msg_type: str, body: object) -> None:
    if not isinstance(body, dict):
        raise ValueError("message body must be an object")
    if msg_type == "budget_block" and set(body) != _BUDGET_BODY:
        raise ValueError("budget_block body must be {agent, rule, limit, observed}")
    if msg_type == "promote" and set(body) != _PROMOTE_BODY:
        raise ValueError("promote body must be {agent, harness, by, reason, claim_id}")
