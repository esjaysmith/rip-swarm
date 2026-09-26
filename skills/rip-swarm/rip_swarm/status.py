from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from rip_swarm.board import read_board
from rip_swarm.fold import Corrupt, Expired, Holder, active_holder
from rip_swarm.members import list_members
from rip_swarm.orchestrator import orchestrator_state
from rip_swarm.paths import HivePaths
from rip_swarm.registry import known_agents
from rip_swarm.timeutil import format_z

_ACTIVE_JSON = re.compile(r"[^.]+\.json")


def status_report(hive: Path, now: datetime) -> dict:
    state = orchestrator_state(hive, now)
    holders, expired_ids, claim_agents, corrupt = _scan_claims(hive, now)
    known = known_agents(hive)
    # The baton is reported under "orchestrator"; don't double-list it as work.
    task_holders = [rec for rec in holders if rec.task_id != "orchestrator"]
    board = read_board(hive, now)
    activity = _last_activity(hive)
    members = list_members(hive)
    return {
        "orchestrator": {
            "agent": state["agent"],
            "matches_claim": state["matches_claim"],
            "expired": state["expired"],
        },
        "active_claims": [
            {
                "task_id": rec.task_id,
                "agent": rec.agent,
                "expires_at": format_z(rec.expires_at),
            }
            for rec in task_holders
        ],
        "inbox_without_claim": [
            tid
            for tid in _inbox_without_claim(hive, {rec.task_id for rec in holders})
            if tid not in board or not (board[tid].rejected or board[tid].blocked_by)
        ],
        "expired_claim_files": expired_ids,
        "corrupt_claims": [
            {"task_id": rec.task_id, "path": _rel(hive, rec.path), "error": rec.error}
            for rec in corrupt
        ],
        "jsonl_parse_errors": _jsonl_parse_errors(hive),
        "unknown_agents": sorted(agent for agent in claim_agents if agent not in known),
        "current_mismatch": not state["matches_claim"],
        "titles": _task_titles(hive),
        "members": [
            {"id": m["id"], "harness": m["harness"], "joined_at": m.get("joined_at"),
             "last_activity": activity.get(m["id"])}
            for m in members if not m["left"]
        ],
        "left_members": [m["id"] for m in members if m["left"]],
        "blocked": [
            {"task_id": tid, "waiting_on": list(view.blocked_by)}
            for tid, view in sorted(board.items())
            if view.blocked_by and not view.rejected and not view.completed
        ],
        "awaiting_acceptance": sorted(t for t, v in board.items() if v.awaiting_acceptance),
        "fixes": {tid: view.fixes for tid, view in sorted(board.items()) if view.fixes},
    }


def format_status(report: dict) -> str:
    orch = report["orchestrator"]
    lines = [
        (
            f"orchestrator: agent={orch['agent']} "
            f"matches_claim={orch['matches_claim']} expired={orch['expired']}"
        ),
        f"current_mismatch: {report['current_mismatch']}",
        "active_claims:",
    ]
    titles = report.get("titles") or {}
    fixes = report.get("fixes") or {}
    if report["active_claims"]:
        for rec in report["active_claims"]:
            tid = rec["task_id"]
            line = _titled(
                f"  {tid} agent={rec['agent']} expires_at={rec['expires_at']}",
                titles.get(tid),
            )
            if tid in fixes:
                line += f" (fixes {fixes[tid]})"
            lines.append(line)
    else:
        lines.append("  (none)")
    _section(
        lines,
        "inbox_without_claim",
        [
            _titled(tid, titles.get(tid)) + (f" (fixes {fixes[tid]})" if tid in fixes else "")
            for tid in report["inbox_without_claim"]
        ],
    )
    _section(lines, "expired_claim_files", report["expired_claim_files"])
    lines.append("jsonl_parse_errors:")
    if report["jsonl_parse_errors"]:
        for err in report["jsonl_parse_errors"]:
            lines.append(f"  {err['path']}:{err['line']}: {err['error']}")
    else:
        lines.append("  (none)")
    _section(lines, "unknown_agents", report["unknown_agents"])
    lines.append("corrupt_claims:")
    if report.get("corrupt_claims"):
        for rec in report["corrupt_claims"]:
            lines.append(f"  {rec['task_id']} ({rec['path']}): {rec['error']}")
    else:
        lines.append("  (none)")
    _section(
        lines,
        "blocked",
        [
            f"{b['task_id']} waiting on {', '.join(b['waiting_on'])}"
            + (f" (fixes {fixes[b['task_id']]})" if b["task_id"] in fixes else "")
            for b in report.get("blocked", [])
        ],
    )
    _section(
        lines,
        "awaiting_acceptance",
        [
            _titled(tid, titles.get(tid)) + (f" (fixes {fixes[tid]})" if tid in fixes else "")
            for tid in report.get("awaiting_acceptance", [])
        ],
    )
    _section(
        lines,
        "members",
        [
            f"{m['id']} harness={m['harness']} joined_at={m['joined_at']} "
            f"last_activity={m['last_activity']}"
            for m in report.get("members", [])
        ],
    )
    _section(lines, "left_members", report.get("left_members", []))
    return "\n".join(lines) + "\n"


def _titled(line: str, title: str | None) -> str:
    return f"{line} {title}" if title else line


def _task_titles(hive: Path) -> dict[str, str]:
    """Inbox titles for display only; an unreadable task file just shows no title."""
    inbox = HivePaths(hive).inbox
    titles: dict[str, str] = {}
    if not inbox.is_dir():
        return titles
    for path in inbox.glob("*.json"):
        try:
            title = json.loads(path.read_text(encoding="utf-8")).get("title")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(title, str) and title.strip():
            titles[path.stem] = " ".join(title.split())
    return titles


def _section(lines: list[str], title: str, items: list[str]) -> None:
    lines.append(f"{title}:")
    if items:
        lines.extend(f"  {item}" for item in items)
    else:
        lines.append("  (none)")


def _scan_claims(
    hive: Path, now: datetime
) -> tuple[list[Holder], list[str], set[str], list[Corrupt]]:
    claims_dir = HivePaths(hive).claims
    holders: list[Holder] = []
    expired_ids: list[str] = []
    agents: set[str] = set()
    corrupt: list[Corrupt] = []
    if not claims_dir.is_dir():
        return holders, expired_ids, agents, corrupt
    for path in sorted(claims_dir.glob("*.json"), key=lambda p: p.name):
        if _ACTIVE_JSON.fullmatch(path.name) is None:
            continue
        rec = active_holder(hive, path.stem, now)
        if isinstance(rec, Holder):
            holders.append(rec)
            agents.add(rec.agent)
        elif isinstance(rec, Expired):
            expired_ids.append(rec.task_id)
            agents.add(rec.agent)
        elif isinstance(rec, Corrupt):
            corrupt.append(rec)
    holders.sort(key=lambda rec: rec.task_id)
    expired_ids.sort()
    corrupt.sort(key=lambda rec: rec.task_id)
    return holders, expired_ids, agents, corrupt


def _completed_task_ids(hive: Path) -> set[str]:
    """Tasks with a `complete` tombstone are done, not unattended. A `reject`
    tombstone means nobody took the work, so the task stays listed."""
    claims = HivePaths(hive).claims
    if not claims.is_dir():
        return set()
    return {path.name.split(".", 1)[0] for path in claims.glob("*.complete.*.json")}


def _inbox_without_claim(hive: Path, held: set[str]) -> list[str]:
    inbox = HivePaths(hive).inbox
    if not inbox.is_dir():
        return []
    settled = held | _completed_task_ids(hive)
    missing: list[str] = []
    for path in inbox.glob("*.json"):
        if _ACTIVE_JSON.fullmatch(path.name) is None:
            continue
        if path.stem not in settled:
            missing.append(path.stem)
    missing.sort()
    return missing


def _jsonl_parse_errors(hive: Path) -> list[dict]:
    paths = HivePaths(hive)
    errors: list[dict] = []
    for path in (paths.messages_jsonl, paths.claims_jsonl):
        errors.extend(_scan_jsonl(hive, path))
    return errors


def _scan_jsonl(hive: Path, path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rel = _rel(hive, path)
    errors: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append({"path": rel, "line": line_no, "error": str(e)})
                continue
            if not isinstance(data, dict):
                errors.append(
                    {"path": rel, "line": line_no, "error": "not a JSON object"}
                )
    return errors


def _last_activity(hive: Path) -> dict[str, str]:
    """Newest `ts` per agent across messages.jsonl (`from.agent`) and claims.jsonl (`agent`)."""
    paths = HivePaths(hive)
    newest: dict[str, str] = {}
    for path, key in ((paths.messages_jsonl, "from"), (paths.claims_jsonl, "agent")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            who = row.get(key)
            agent = who.get("agent") if isinstance(who, dict) else who
            ts = row.get("ts")
            if isinstance(agent, str) and isinstance(ts, str) and ts > newest.get(agent, ""):
                newest[agent] = ts
    return newest


def _rel(hive: Path, path: Path) -> str:
    try:
        return path.relative_to(hive).as_posix()
    except ValueError:
        return path.as_posix()
