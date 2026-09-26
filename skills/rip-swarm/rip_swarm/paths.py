from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class HivePathError(ValueError):
    pass


def resolve_hive(
    explicit: str | None = None,
    env: dict | None = None,
    cwd: Path | None = None,
) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    e = os.environ if env is None else env
    val = e.get("RIP_SWARM_HIVE")
    if val:
        return Path(val).expanduser().resolve()
    default = (cwd or Path.cwd()) / "_swarm"
    if default.is_dir():
        return default.resolve()
    raise HivePathError("no ./_swarm here: set RIP_SWARM_HIVE or pass --hive")


@dataclass(frozen=True)
class HivePaths:
    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def claims(self) -> Path:
        return self.root / "claims"

    @property
    def store(self) -> Path:
        return self.root / "store"

    @property
    def agents(self) -> Path:
        return self.root / "agents"

    @property
    def orchestrator(self) -> Path:
        return self.root / "orchestrator"

    @property
    def lookback(self) -> Path:
        return self.root / "lookback"

    @property
    def accepted(self) -> Path:
        return self.root / "accepted"

    def accepted_record(self, task_id: str) -> Path:
        return self.accepted / f"{task_id}.json"

    def member(self, agent_id: str) -> Path:
        return self.agents / agent_id / "member.json"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    @property
    def protocol(self) -> Path:
        return self.root / "PROTOCOL.md"

    @property
    def registry(self) -> Path:
        return self.root / "agents" / "registry.yaml"

    @property
    def messages_jsonl(self) -> Path:
        return self.root / "store" / "messages.jsonl"

    @property
    def claims_jsonl(self) -> Path:
        return self.root / "store" / "claims.jsonl"

    @property
    def current(self) -> Path:
        return self.root / "orchestrator" / "CURRENT.json"

    def inbox_task(self, task_id: str) -> Path:
        return self.inbox / f"{task_id}.json"

    def claim(self, task_id: str) -> Path:
        return self.claims / f"{task_id}.json"

    def outbox(self, agent_id: str) -> Path:
        return self.agents / agent_id / "outbox"
