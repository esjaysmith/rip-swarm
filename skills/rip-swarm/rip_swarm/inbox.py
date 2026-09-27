# rip_swarm/inbox.py
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from rip_swarm.ids import new_task_id
from rip_swarm.io import excl_create_json, read_json
from rip_swarm.paths import HivePaths
from rip_swarm.timeutil import format_z, now_utc


class InboxError(ValueError):
    pass


_TASK_ID = re.compile(r"task_[0-9A-HJKMNP-TV-Z]{26}")
_RESERVED = frozenset({"orchestrator", "CURRENT", "registry", "default"})


def validate_task_id(task_id: str) -> str:
    """Accept only a canonical `task_<ULID>` id; reject reserved literals."""
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise InboxError(f"invalid task_id {task_id!r}")
    if task_id in _RESERVED:
        raise InboxError(f"reserved task_id {task_id!r}")
    return task_id


_REPLACES = re.compile(r" \(replaces (task_[0-9A-HJKMNP-TV-Z]{26})\)$")


def _whole(n: object) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


def create_task(
    hive: Path,
    *,
    title: str,
    created_by: str,
    body: str | None = None,
    task_id: str | None = None,
    now: datetime | None = None,
    after: list[str] | None = None,
    fixes: str | None = None,
    kind: str | None = None,
    min_reviews: int | None = None,
    reviews: str | None = None,
    floors: dict[str, int] | None = None,
) -> dict:
    """Post one task. `kind`, `min_reviews` and `reviews` are the review-round
    fields of execution proposals §5.2; `floors` is the profile's `min_reviews`
    map, the floor a posted number may not go below (§5.5)."""
    title = title.strip()
    if not title:
        raise InboxError("title is required")
    if not created_by.strip():
        raise InboxError("created_by is required")
    if reviews is not None and (fixes or kind is not None or min_reviews is not None):
        raise InboxError("--reviews cannot be combined with --fixes, --min-reviews or --kind")
    if fixes and (kind is not None or min_reviews is not None):
        # §5.2: rebase, follow-up and review tasks get no kind and no min_reviews.
        raise InboxError("--fixes cannot be combined with --kind or --min-reviews")
    if min_reviews is not None and not _whole(min_reviews):
        raise InboxError(f"--min-reviews must be an integer >= 0, got {min_reviews!r}")
    deps = list(dict.fromkeys(after or []))
    for ref in [*deps, *([fixes] if fixes else []), *([reviews] if reviews else [])]:
        validate_task_id(ref)
        if not HivePaths(hive).inbox_task(ref).is_file():
            raise InboxError(f"unknown task {ref}: post it before tasks that refer to it")
    if reviews is not None and not _whole_at_least_one(read_json(HivePaths(hive).inbox_task(reviews))):
        raise InboxError(f"{reviews} is not a reviewed artifact (min_reviews >= 1)")
    for target in [ref for ref in (fixes, reviews) if ref]:
        _require_live_chain(hive, target)
    if kind is not None:
        known = floors or {}
        if kind not in known:
            names = ", ".join(sorted(known)) or "none"
            raise InboxError(f"unknown kind {kind!r}: the profile's min_reviews names {names}")
        if min_reviews is None:
            min_reviews = known[kind]
        elif min_reviews < known[kind] and not _exact_copy(hive, title, kind, min_reviews):
            raise InboxError(f"min_reviews for {kind} is at least {known[kind]} (profile)")
    ts = now or now_utc()
    tid = validate_task_id(task_id) if task_id is not None else new_task_id(ts)
    doc = {
        "id": tid,
        "title": title,
        "created_at": format_z(ts),
        "created_by": created_by,
    }
    if body is not None:
        doc["body"] = body
    if deps:
        doc["after"] = deps
    if fixes:
        doc["fixes"] = fixes
    if kind is not None:
        doc["kind"] = kind
    if min_reviews is not None:
        doc["min_reviews"] = min_reviews
    if reviews is not None:
        doc["reviews"] = reviews
    excl_create_json(HivePaths(hive).inbox_task(tid), doc)
    return doc


def _whole_at_least_one(doc: dict) -> bool:
    return _whole(doc.get("min_reviews")) and doc["min_reviews"] >= 1


def _require_live_chain(hive: Path, target: str) -> None:
    """A fix or review of a reviewed artifact that is already accepted or
    rejected would never be accepted: `reviews.py` says `done` for it and the
    master acts on nothing. Refuse it when posted. A target that is not a
    reviewed artifact keeps today's rule, so a replacement of an ordinary
    follow-up still copies `--fixes` of a rejected original."""
    paths = HivePaths(hive)
    try:
        doc = read_json(paths.inbox_task(target))
    except (OSError, ValueError):
        return
    if not _whole_at_least_one(doc):
        return
    if paths.accepted_record(target).is_file():
        raise InboxError(f"{target} is accepted; post a new task instead")
    if any(paths.claims.glob(f"{target}.reject.*.json")):
        raise InboxError(f"{target} is rejected; post a new task instead")


def _exact_copy(hive: Path, title: str, kind: str, n: int) -> bool:
    """A replacement's copy may keep its original's number below today's
    profile (§5.5): the title names `X`, `X` is rejected, and `X` has exactly
    this kind and number. Checked on the board, so nothing else passes."""
    match = _REPLACES.search(title)
    if not match:
        return False
    original = match.group(1)
    try:
        doc = read_json(HivePaths(hive).inbox_task(original))
    except (OSError, ValueError):
        return False
    rejected = any(HivePaths(hive).claims.glob(f"{original}.reject.*.json"))
    return rejected and doc.get("kind") == kind and doc.get("min_reviews") == n and _whole(n)


def read_task(hive: Path, task_id: str) -> dict:
    return read_json(HivePaths(hive).inbox_task(task_id))
