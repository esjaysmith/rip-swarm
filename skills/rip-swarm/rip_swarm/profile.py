from __future__ import annotations

import copy
import os
from pathlib import Path

from rip_swarm.paths import HivePaths
from rip_swarm.simpleyaml import load_yaml

DEFAULT_PROFILE = {
    "name": "default",
    "reviews_required_per_plan": 1,
    "orchestrator_lease_ttl": "30m",
    "worker_lease_ttl": "15m",
    "idle_board_after": "10m",
    "allow_self_promote": False,
    "allow_preempt": False,
    "operators": [],
    "budget": {
        "max_claims_open_per_agent": 1,
        "spend_requires_operator": True,
    },
    "lookback": {
        "min_messages_before_run": 20,
        "write_dir": "lookback/",
    },
    "slash": {
        "enabled": ["lookback", "status"],
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_profile(hive: Path, name: str | None, env: dict | None = None) -> dict:
    """Resolve a profile: explicit name -> RIP_SWARM_PROFILE -> `default`.

    DEFAULT_PROFILE is always the base (pillar D), so a partial or missing
    `profiles/default.yaml` and a missing site file still yield a complete
    profile instead of raising.
    """
    e = os.environ if env is None else env
    resolved = name or e.get("RIP_SWARM_PROFILE") or "default"
    profiles = HivePaths(hive).profiles
    result = deep_merge(DEFAULT_PROFILE, _read_profile(profiles / "default.yaml"))
    if resolved != "default":
        result = deep_merge(result, _read_profile(profiles / f"{resolved}.yaml"))
    return result


def _read_profile(path: Path) -> dict:
    """Read one profile file. A missing file contributes nothing."""
    if not path.is_file():
        return {}
    doc = load_yaml(path.read_text(encoding="utf-8"))
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ValueError(f"profile must be a mapping: {path}")
    return doc
