from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from rip_swarm.fold import Corrupt, Expired, Holder, active_holder
from rip_swarm.orchestrator import orchestrator_state
from rip_swarm.paths import HivePaths
from rip_swarm.registry import load_registry
from rip_swarm.timeutil import format_z

_ACTIVE_JSON = re.compile(r"[^.]+\.json")


def status_report(hive: Path, now: datetime) -> dict:
    state = orchestrator_state(hive, now)
    holders, expired_ids, claim_agents, corrupt = _scan_claims(hive, now)
    known = {agent["id"] for agent in load_registry(hive)}
    # The baton is reported under "orchestrator"; don't double-list it as work.
    task_holders = [rec for rec in holders if rec.task_id != "orchestrator"]
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
        "inbox_without_claim": _inbox_without_claim(
            hive, {rec.task_id for rec in holders}
        ),
        "expired_claim_files": expired_ids,
        "corrupt_claims": [
            {"task_id": rec.task_id, "path": _rel(hive, rec.path), "error": rec.error}
            for rec in corrupt
        ],
        "jsonl_parse_errors": _jsonl_parse_errors(hive),
        "unknown_agents": sorted(agent for agent in claim_agents if agent not in known),
        "current_mismatch": not state["matches_claim"],
        "titles": _task_titles(hive),
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
    if report["active_claims"]:
        for rec in report["active_claims"]:
            lines.append(
                _titled(
                    f"  {rec['task_id']} agent={rec['agent']} expires_at={rec['expires_at']}",
                    titles.get(rec["task_id"]),
                )
            )
    else:
        lines.append("  (none)")
    _section(
        lines,
        "inbox_without_claim",
        [_titled(tid, titles.get(tid)) for tid in report["inbox_without_claim"]],
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


def _rel(hive: Path, path: Path) -> str:
    try:
        return path.relative_to(hive).as_posix()
    except ValueError:
        return path.as_posix()
