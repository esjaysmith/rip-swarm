from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rip_swarm.io import read_json
from rip_swarm.paths import HivePaths
from rip_swarm.profile import DEFAULT_PROFILE
from rip_swarm.status import status_report

_TOMBSTONE = frozenset({"expired", "complete", "release", "reject"})


def write_lookback(hive: Path, now: datetime, profile: dict | None = None) -> Path:
    hive = Path(hive)
    cfg = _lookback_cfg(profile)
    write_dir = _resolve_write_dir(hive, str(cfg["write_dir"]))
    write_dir.mkdir(parents=True, exist_ok=True)
    day = _day(now)
    path = _next_report_path(write_dir, day)
    report = status_report(hive, now)
    path.write_text(_render(hive, day, report, cfg), encoding="utf-8")
    return path


def _resolve_write_dir(hive: Path, write_dir: str) -> Path:
    """`write_dir` is relative to the hive root (section 9) and must stay inside it.

    A profile is operator-owned but still a config file: an absolute path or a
    `../` escape would let a lookback run write anywhere on the machine.
    """
    candidate = Path(write_dir)
    if candidate.is_absolute():
        raise ValueError(f"lookback write_dir must be relative to the hive: {write_dir!r}")
    root = hive.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"lookback write_dir escapes the hive: {write_dir!r}")
    return resolved


def _lookback_cfg(profile: dict | None) -> dict:
    cfg = dict(DEFAULT_PROFILE["lookback"])
    if isinstance(profile, dict) and isinstance(profile.get("lookback"), dict):
        cfg.update(profile["lookback"])
    cfg["write_dir"] = cfg.get("write_dir") or "lookback/"
    min_n = cfg.get("min_messages_before_run")
    cfg["min_messages_before_run"] = 20 if min_n is None else int(min_n)
    return cfg


def _day(now: datetime) -> str:
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%d")


def _next_report_path(write_dir: Path, day: str) -> Path:
    candidate = write_dir / f"{day}.md"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = write_dir / f"{day}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def _render(hive: Path, day: str, report: dict, cfg: dict) -> str:
    lines = [f"# Lookback {day}", ""]
    n_messages = _message_line_count(hive)
    min_n = cfg["min_messages_before_run"]
    if n_messages < min_n:
        lines.append(
            f"Not enough traffic: {n_messages} messages.jsonl lines (min {min_n})."
        )
        lines.append("")
    doubles = _double_claim_bullets(hive)
    _add_section(lines, "Double claims", doubles)
    _add_section(lines, "Expired leases", list(report["expired_claim_files"]))
    _add_section(lines, "CURRENT vs orchestrator claim", _mismatch_bullets(report))
    _add_section(lines, "Inbox with no claim", list(report["inbox_without_claim"]))
    _add_section(lines, "JSONL parse errors", _jsonl_bullets(report))
    _add_section(
        lines,
        "Suggested PROTOCOL/profile diffs",
        _suggestion_bullets(report, doubles),
    )
    return "\n".join(lines).rstrip() + "\n"


def _add_section(lines: list[str], title: str, items: list[str]) -> None:
    lines.append(f"## {title}")
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("None.")
    lines.append("")


def _mismatch_bullets(report: dict) -> list[str]:
    if not report["current_mismatch"]:
        return []
    orch = report["orchestrator"]
    return [
        (
            f"current_mismatch agent={orch['agent']} "
            f"matches_claim={orch['matches_claim']} expired={orch['expired']}"
        )
    ]


def _jsonl_bullets(report: dict) -> list[str]:
    return [
        f"{err['path']}:{err['line']}: {err['error']}"
        for err in report["jsonl_parse_errors"]
    ]


def _suggestion_bullets(report: dict, doubles: list[str]) -> list[str]:
    items: list[str] = []
    expired = report["expired_claim_files"]
    orch_expired = report["orchestrator"]["expired"] or "orchestrator" in expired
    worker_n = sum(1 for task_id in expired if task_id != "orchestrator")
    if orch_expired:
        items.append(
            "raise orchestrator_lease_ttl to 1h — orchestrator lease expired"
        )
    if worker_n:
        times = "once" if worker_n == 1 else f"{worker_n} times"
        items.append(f"raise worker_lease_ttl — expired {times}")
    if report["current_mismatch"]:
        items.append(
            "keep CURRENT.json in lockstep with the last promote claim"
        )
    if doubles:
        items.append(
            "tombstone the prior claim before another agent claims the same task"
        )
    return items


def _message_line_count(hive: Path) -> int:
    path = HivePaths(hive).messages_jsonl
    if not path.is_file():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def _double_claim_bullets(hive: Path) -> list[str]:
    paths = HivePaths(hive)
    by_task: dict[str, list[dict]] = {}
    for row in _iter_jsonl_objects(paths.claims_jsonl):
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        by_task.setdefault(task_id, []).append(row)
    bullets: list[str] = []
    for task_id, rows in sorted(by_task.items()):
        agents = _double_claim_agents(rows, _file_holder_agent(paths.claim(task_id)))
        if agents:
            bullets.append(f"{task_id} agents={','.join(sorted(agents))}")
    return bullets


def _double_claim_agents(rows: list[dict], file_agent: str | None) -> set[str]:
    involved: set[str] = set()
    epoch: set[str] = set()

    def flush(*, current: bool) -> None:
        if len(epoch) > 1:
            involved.update(epoch)
        if current and file_agent and any(agent != file_agent for agent in epoch):
            involved.update(epoch)
            involved.add(file_agent)

    for row in rows:
        action = row.get("action")
        if action in _TOMBSTONE:
            flush(current=False)
            epoch = set()
            continue
        if action != "claim":
            continue
        agent = row.get("agent")
        if isinstance(agent, str) and agent:
            epoch.add(agent)
    flush(current=True)
    return involved


def _file_holder_agent(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        doc = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    agent = doc.get("agent")
    return agent if isinstance(agent, str) else None


def _iter_jsonl_objects(path: Path):
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                yield data
