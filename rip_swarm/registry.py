from __future__ import annotations

import re
from pathlib import Path

from rip_swarm.paths import HivePaths
from rip_swarm.simpleyaml import load_yaml

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ROLES = frozenset({"operator", "orchestrator", "worker", "observer"})
_RESERVED = frozenset({"orchestrator", "*"})


class RegistryError(ValueError):
    pass


class UnknownAgent(RegistryError):
    pass


def load_registry(hive: Path) -> list[dict]:
    path = HivePaths(hive).registry
    if not path.is_file():
        return []
    doc = load_yaml(path.read_text(encoding="utf-8"))
    if doc is None:
        return []
    if not isinstance(doc, list):
        raise RegistryError("registry must be a list of agents")
    return [_check_agent(item) for item in doc]


def require_agent(hive: Path, agent_id: str) -> dict:
    for agent in load_registry(hive):
        if agent["id"] == agent_id:
            return agent
    raise UnknownAgent(f"unknown agent: {agent_id}")


def _check_agent(item: object) -> dict:
    if not isinstance(item, dict):
        raise RegistryError("registry entry must be a mapping")
    agent_id = item.get("id")
    if (
        not isinstance(agent_id, str)
        or agent_id in _RESERVED
        or _AGENT_ID.fullmatch(agent_id) is None
    ):
        raise RegistryError(f"invalid agent id: {agent_id!r}")
    role = item.get("role")
    if role not in _ROLES:
        raise RegistryError(f"invalid agent role: {role!r}")
    harness = item.get("harness")
    if not isinstance(harness, str) or not harness:
        raise RegistryError("agent harness is required")
    return item
