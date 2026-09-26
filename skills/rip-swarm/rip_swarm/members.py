# rip_swarm/members.py — session membership files (spec §3)
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from rip_swarm.ids import new_ulid
from rip_swarm.io import ExclExistsError, excl_create_json, read_json, write_json_to_new_path
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import format_z

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LEFT = re.compile(r"member\.left\.\d{8}T\d{6}Z(?:-\d+)?\.json")


class MemberError(ValueError):
    pass


class MemberExists(MemberError):
    pass


def short_prefix(harness: str) -> str:
    """`claude-code` -> `claude`. Never `op`, never empty (spec §3.2)."""
    head = str(harness).split("-", 1)[0].lower()
    short = re.sub(r"[^a-z0-9_]", "", head).lstrip("_")[:48]
    if not short or short == "op":
        return "agent"
    return short


def has_left(hive: Path, agent_id: str) -> bool:
    folder = HivePaths(hive).agents / agent_id
    if not folder.is_dir():
        return False
    return any(_LEFT.fullmatch(p.name) for p in folder.iterdir())


def member_ids(hive: Path) -> list[str]:
    """Every id with a member file, readable or not, active or left."""
    agents = HivePaths(hive).agents
    if not agents.is_dir():
        return []
    return sorted(p.parent.name for p in agents.glob("*/member.json"))


def list_members(hive: Path) -> list[dict]:
    """Readable member bodies plus `left`. Unreadable files are skipped here
    (their ids still count in `member_ids`, so they are never reallocated)."""
    out: list[dict] = []
    for agent_id in member_ids(hive):
        if _AGENT_ID.fullmatch(agent_id) is None:
            continue
        try:
            doc = read_json(HivePaths(hive).member(agent_id))
        except (OSError, ValueError):
            continue
        harness = doc.get("harness")
        if doc.get("id") != agent_id or not isinstance(harness, str) or not harness:
            continue
        out.append({**doc, "left": has_left(hive, agent_id)})
    return out


def next_member_id(hive: Path, harness: str, reserved: Iterable[str] = ()) -> str:
    """`<short>-<n>`: 1 + the integer max over member files and `reserved` ids."""
    short = short_prefix(harness)
    pattern = re.compile(rf"{re.escape(short)}-(\d+)")
    top = 0
    for agent_id in [*member_ids(hive), *reserved]:
        match = pattern.fullmatch(agent_id)
        if match:
            top = max(top, int(match.group(1)))
    return f"{short}-{top + 1}"


def create_member(hive: Path, *, agent_id: str, harness: str, now: datetime) -> dict:
    """Create-only `agents/<id>/member.json`. `session` is random so two sessions
    racing for one id always conflict in git instead of merging silently."""
    if _AGENT_ID.fullmatch(agent_id) is None:
        raise MemberError(f"invalid member id {agent_id!r}")
    doc = {
        "id": agent_id,
        "harness": harness,
        "role": "worker",
        "joined_at": format_z(now),
        "session": new_ulid(now),
    }
    try:
        excl_create_json(HivePaths(hive).member(agent_id), doc)
    except ExclExistsError as e:
        raise MemberExists(f"member {agent_id} already exists") from e
    return doc


def _stamp(now: datetime) -> str:
    return format_z(now).replace("-", "").replace(":", "")


def write_left(hive: Path, agent_id: str, now: datetime) -> dict:
    """Tombstone a member: `member.left.<stamp>.json` beside `member.json`."""
    path = HivePaths(hive).member(agent_id)
    if not path.is_file():
        raise MemberError(f"no member file for {agent_id}")
    doc = {**read_json(path), "left_at": format_z(now)}
    base = f"member.left.{_stamp(now)}"
    n = 1
    while True:
        name = f"{base}.json" if n == 1 else f"{base}-{n}.json"
        if write_json_to_new_path(path.with_name(name), doc):
            return doc
        n += 1

