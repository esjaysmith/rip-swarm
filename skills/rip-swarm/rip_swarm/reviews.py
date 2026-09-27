# rip_swarm/reviews.py — a reviewed artifact's chain and its next step
# (execution proposals §5.3, §5.4). Read-only: everything comes from the board.
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rip_swarm.board import TaskView, Tombstone, artifact_of, chain, list_tombstones
from rip_swarm.fold import Expired, Holder, active_holder

VERDICTS = ("clean", "findings")
_CANNOT_BUILD = re.compile(
    r"review (?P<review>task_[0-9A-HJKMNP-TV-Z]{26}) cannot build on "
    r"(?P<sha>[0-9a-f]{7,64}): conflict"
)
_SUFFIX = re.compile(r"-(\d+)\.json$")


class ReviewsError(ValueError):
    """The board gives no next step for this task (exit 1, no NEXT line)."""


def _write_order(stone: Tombstone) -> tuple[str, int]:
    """Tombstones written in one second are `<stamp>.json`, `<stamp>-2.json`, …
    By name `-2` sorts first, so order by the stamp, then by that number."""
    match = _SUFFIX.search(stone.name)
    return (stone.stamp, int(match.group(1)) if match else 1)


def _stamp_z(stamp: str) -> str:
    """`20260926T100000Z` -> `2026-09-26T10:00:00Z`, comparable with `created_at`."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"


def _cannot_build(stone: Tombstone, review_id: str) -> re.Match | None:
    """The note of a release that says review `review_id` could not build."""
    match = _CANNOT_BUILD.fullmatch((stone.doc() or {}).get("note") or "")
    return match if match is not None and match["review"] == review_id else None


def tombstones_by_task(hive: Path) -> dict[str, list[Tombstone]]:
    out: dict[str, list[Tombstone]] = {}
    for stone in list_tombstones(hive):
        out.setdefault(stone.task_id, []).append(stone)
    for stones in out.values():
        stones.sort(key=_write_order)
    return out


def _complete_doc(stones: dict[str, list[Tombstone]], task_id: str) -> dict:
    for stone in stones.get(task_id, []):
        if stone.action == "complete":
            return stone.doc() or {}
    return {}


def _short_sha(doc: dict) -> str | None:
    ref = doc.get("result_ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    return ref.rsplit("@", 1)[-1].strip()


@dataclass(frozen=True)
class ChainState:
    artifact: TaskView
    tasks: tuple[TaskView, ...]      # its reviews and fixes, in posting order
    settled: tuple[TaskView, ...]    # of those, the completed and not rejected
    head: TaskView | None
    head_sha: str | None
    head_verdict: str | None
    rounds: int

    @property
    def ready(self) -> bool:
        """§5.3: at least `min_reviews` rounds, and the head is a clean review."""
        head = self.head
        return (
            head is not None
            and head.reviews == self.artifact.task_id
            and self.head_verdict == "clean"
            and self.rounds >= self.artifact.min_reviews
        )


def chain_state(
    hive: Path,
    board: dict[str, TaskView],
    artifact_id: str,
    stones: dict[str, list[Tombstone]] | None = None,
) -> ChainState:
    """The head is the chain task latest in posting order among those that
    are completed and not rejected, `A` itself first. A round is a review in
    the chain that is completed and not rejected."""
    stones = tombstones_by_task(hive) if stones is None else stones
    art = board[artifact_id]
    tasks = tuple(chain(board, artifact_id))
    settled = tuple(v for v in tasks if v.completed and not v.rejected)
    heads = ([art] if art.completed and not art.rejected else []) + list(settled)
    head = heads[-1] if heads else None
    doc = _complete_doc(stones, head.task_id) if head is not None else {}
    return ChainState(
        artifact=art,
        tasks=tasks,
        settled=settled,
        head=head,
        head_sha=_short_sha(doc) if head is not None else None,
        head_verdict=doc.get("verdict") if head is not None else None,
        rounds=sum(1 for v in settled if v.reviews == artifact_id),
    )


@dataclass(frozen=True)
class Step:
    next: str
    artifact: str
    rounds: int
    needed: int
    head: str | None
    sha: str | None
    chain: tuple[str, ...]
    review: str | None = None

    def line(self) -> str:
        text = (
            f"NEXT={self.next} ARTIFACT={self.artifact} ROUNDS={self.rounds}/{self.needed} "
            f"HEAD={self.head or 'none'} SHA={self.sha or 'none'} CHAIN={','.join(self.chain)}"
        )
        return f"{text} REVIEW={self.review}" if self.review else text


def next_step(hive: Path, board: dict[str, TaskView], task_id: str) -> Step:
    """§5.4's table, row by row. `task_id` is the artifact or any task in its chain."""
    artifact_id = artifact_of(board, task_id)
    if artifact_id is None:
        raise ReviewsError(f"{task_id} is not a reviewed artifact or in the chain of one")
    stones = tombstones_by_task(hive)
    st = chain_state(hive, board, artifact_id, stones)
    art = st.artifact

    def step(name: str, sha: str | None = None, review: str | None = None) -> Step:
        return Step(
            next=name,
            artifact=artifact_id,
            rounds=st.rounds,
            needed=art.min_reviews,
            head=st.head.task_id if st.head is not None else None,
            sha=sha if sha is not None else st.head_sha,
            chain=tuple(v.task_id for v in st.settled),
            review=review,
        )

    if art.accepted or art.rejected:
        return step("done")
    # An open review whose latest release says it could not build. Read from
    # the board, so a takeover master that never saw the wake or the message
    # still gets it (§5.4). The review stays open until the master rejects it,
    # so another worker may release it again with the same note: a rebase
    # counts from the earliest such release, never only from the latest.
    for view in st.tasks:
        if view.reviews != artifact_id or not view.is_open:
            continue
        released = [s for s in stones.get(view.task_id, []) if s.action == "release"]
        if not released:
            continue
        match = _cannot_build(released[-1], view.task_id)
        if match is None:
            continue
        first = next(s for s in released if _cannot_build(s, view.task_id) is not None)
        since = _stamp_z(first.stamp)
        rebased = any(v.fixes == artifact_id and v.created_at >= since for v in st.tasks)
        return step("reject-review" if rebased else "post-rebase", sha=match["sha"],
                    review=view.task_id)
    if not art.completed or any(not v.completed and not v.rejected for v in st.tasks):
        return step("wait")
    head = st.head
    if head.reviews == artifact_id:
        if st.head_verdict not in VERDICTS:
            raise ReviewsError(
                f"review {head.task_id} has no verdict clean or findings on its complete "
                "tombstone; the board was edited by hand"
            )
        if st.head_verdict == "findings":
            return step("post-revise")
        return step("merge" if st.rounds >= art.min_reviews else "post-review")
    return step("post-review")


def authors(
    hive: Path,
    board: dict[str, TaskView],
    artifact_id: str,
    stones: dict[str, list[Tombstone]] | None = None,
) -> set[str]:
    """§5.3: every agent that completed `A` or a fix of `A`."""
    stones = tombstones_by_task(hive) if stones is None else stones
    ids = [artifact_id, *(v.task_id for v in board.values() if v.fixes == artifact_id)]
    out: set[str] = set()
    for tid in ids:
        for stone in stones.get(tid, []):
            agent = (stone.doc() or {}).get("agent") if stone.action == "complete" else None
            if isinstance(agent, str):
                out.add(agent)
    return out


def reviewers(
    hive: Path,
    board: dict[str, TaskView],
    artifact_id: str,
    now: datetime,
    stones: dict[str, list[Tombstone]] | None = None,
) -> set[str]:
    """§5.3: every agent that has claimed a review of `A`: it holds a claim on
    one (live, or expired and not yet stolen) or has a tombstone on one. A
    master's reject is not a claim: its tombstone carries `"action": "reject"`
    and is skipped."""
    stones = tombstones_by_task(hive) if stones is None else stones
    out: set[str] = set()
    for view in board.values():
        if view.reviews != artifact_id:
            continue
        for stone in stones.get(view.task_id, []):
            doc = stone.doc() or {}
            if doc.get("action") == "reject":
                continue
            if isinstance(doc.get("agent"), str):
                out.add(doc["agent"])
        rec = active_holder(hive, view.task_id, now)
        if isinstance(rec, (Holder, Expired)):
            out.add(rec.agent)
    return out
