from __future__ import annotations

from pathlib import Path

from rip_swarm import members
from rip_swarm.paths import HivePaths
from rip_swarm.simpleyaml import load_yaml

_ROLES = frozenset({"operator", "orchestrator", "worker", "observer"})
_RESERVED = frozenset({"orchestrator", "*"})


class RegistryError(ValueError):
    pass


class UnknownAgent(RegistryError):
    pass


def _yaml_agents(hive: Path) -> list[dict]:
    path = HivePaths(hive).registry
    if not path.is_file():
        return []
    doc = load_yaml(path.read_text(encoding="utf-8"))
    if doc is None:
        return []
    if not isinstance(doc, list):
        raise RegistryError("registry must be a list of agents")
    agents = [_check_agent(item) for item in doc]
    seen: set[str] = set()
    for agent in agents:
        if agent["id"] in seen:
            raise RegistryError(f"duplicate agent id: {agent['id']!r}")
        seen.add(agent["id"])
    return agents


def yaml_agent_ids(hive: Path) -> list[str]:
    return [agent["id"] for agent in _yaml_agents(hive)]


def load_registry(hive: Path) -> list[dict]:
    """`registry.yaml` entries plus active members (spec §3.1).

    A member whose id is also in `registry.yaml` is skipped: the operator's entry
    wins, and the merged list never has a duplicate id.
    """
    agents = _yaml_agents(hive)
    taken = {agent["id"] for agent in agents}
    for member in members.list_members(hive):
        if member["left"] or member["id"] in taken or member["id"] in _RESERVED:
            continue
        agents.append({"id": member["id"], "harness": member["harness"], "role": "worker"})
        taken.add(member["id"])
    return agents


def known_agents(hive: Path) -> set[str]:
    """Every id that ever belonged to the hive: registry.yaml plus all members."""
    return set(yaml_agent_ids(hive)) | set(members.member_ids(hive))


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
        or members._AGENT_ID.fullmatch(agent_id) is None
    ):
        raise RegistryError(f"invalid agent id: {agent_id!r}")
    role = item.get("role")
    if role not in _ROLES:
        raise RegistryError(f"invalid agent role: {role!r}")
    harness = item.get("harness")
    if not isinstance(harness, str) or not harness:
        raise RegistryError("agent harness is required")
    return item
