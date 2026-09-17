from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import format_z


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj) + "\n").encode("utf-8")
    with path.open("ab") as f:
        os.write(f.fileno(), line)
        f.flush()
        os.fsync(f.fileno())


def append_claim_audit(
    hive: Path,
    *,
    action: str,
    claim_doc: dict,
    now: datetime,
    result_ref: str | None = None,
) -> None:
    if result_ref is None:
        result_ref = claim_doc.get("result_ref")
    row = {
        "claim_id": claim_doc["claim_id"],
        "ts": format_z(now),
        "action": action,
        "task_id": claim_doc["task_id"],
        "agent": claim_doc["agent"],
        "harness": claim_doc["harness"],
        "expires_at": claim_doc["expires_at"],
        "result_ref": result_ref,
        "note": claim_doc.get("note"),
    }
    append_jsonl(HivePaths(hive).claims_jsonl, row)
