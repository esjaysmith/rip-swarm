from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rip_swarm.orchestrator import orchestrator_state
from rip_swarm.paths import HivePaths
from rip_swarm.registry import require_agent
from rip_swarm.timeutil import parse_z


def list_messages(
    hive: Path,
    *,
    now: datetime,
    to: str | None = None,
    since: str | None = None,
    frm: str | None = None,
    type: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Messages from the per-agent outbox files (the SoT, §6), oldest first.

    `to=AGENT` means "addressed to AGENT": direct, `*`, and `orchestrator` while AGENT
    holds an unexpired baton; the agent's own messages are left out. `since` is an
    exclusive UTC Z bound on `ts`. Read-only. Returns (messages, unreadable paths).
    """
    if to is not None:
        require_agent(hive, to)
    if since is not None:
        try:
            since_dt = parse_z(since)
        except ValueError:
            raise ValueError(f"--since must be a UTC Z timestamp, got {since!r}") from None
    addressed = _addressed_to(hive, to, now)
    found: list[dict] = []
    unreadable: list[str] = []
    for path in sorted(HivePaths(hive).agents.glob("*/outbox/*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            ts = parse_z(doc["ts"])
            sender = doc["from"]["agent"]
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(str(path.relative_to(hive)))
            continue
        if addressed is not None and (doc.get("to") not in addressed or sender == to):
            continue
        if since is not None and ts <= since_dt:
            continue
        if frm is not None and sender != frm:
            continue
        if type is not None and doc.get("type") != type:
            continue
        found.append(doc)
    found.sort(key=lambda d: (d["ts"], d.get("id", "")))
    return found, unreadable


def _addressed_to(hive: Path, agent: str | None, now: datetime) -> set[str] | None:
    if agent is None:
        return None
    targets = {agent, "*"}
    state = orchestrator_state(hive, now)
    if state["agent"] == agent and state["matches_claim"]:
        targets.add("orchestrator")
    return targets


def format_messages(found: list[dict], unreadable: list[str]) -> str:
    lines = [_line(doc) for doc in found] or ["(no messages)"]
    lines.extend(f"unreadable: {path}" for path in unreadable)
    return "\n".join(lines) + "\n"


def _line(doc: dict) -> str:
    body = doc.get("body")
    if isinstance(body, dict) and set(body) == {"text"}:
        text = str(body["text"])
    else:
        text = json.dumps(body, sort_keys=True)
    return (
        f"{doc['ts']} {doc.get('id', '?')} {doc['from']['agent']} -> {doc.get('to')} "
        f"[{doc.get('type')}] {text}"
    )


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def newest_cursor(hive: Path) -> list[str] | None:
    """`[ts, id]` of the newest message in any outbox, or None."""
    found, _ = list_messages(hive, now=_EPOCH)
    if not found:
        return None
    last = found[-1]
    return [last["ts"], last.get("id", "")]


def unread_messages(
    hive: Path, *, agent: str, cursor: list[str] | None, now: datetime
) -> list[dict]:
    """Messages addressed to `agent` that sort after `cursor` (spec §6)."""
    found, _ = list_messages(hive, now=now, to=agent)
    if not cursor:
        return found
    mark = (str(cursor[0]), str(cursor[1]))
    return [doc for doc in found if (doc["ts"], doc.get("id", "")) > mark]
