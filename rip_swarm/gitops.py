# rip_swarm/gitops.py
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied, try_claim
from rip_swarm.orchestrator import promote
from rip_swarm.timeutil import parse_z

# Isolate from the project checkout: never inherit GIT_DIR / GIT_WORK_TREE.
_GIT_UNSET = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)


class GitopsError(Exception):
    pass


class DirtyHive(GitopsError):
    pass


class NotHiveRepo(GitopsError):
    pass


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _GIT_UNSET:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    return env


def _run(hive: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(hive), *args],
        check=False,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed"
        raise GitopsError(detail)
    return result


def _out(hive: Path, *args: str) -> str:
    return _run(hive, *args).stdout.strip()


def init_repo(path: Path, branch: str) -> None:
    """Create a git repo on `branch`. Falls back when `git init -b` is unsupported."""
    path = Path(path)
    created = subprocess.run(
        ["git", "init", "-q", "-b", branch, str(path)],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if created.returncode == 0:
        return
    fallback = subprocess.run(
        ["git", "init", "-q", str(path)],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if fallback.returncode != 0:
        detail = (fallback.stderr or fallback.stdout).strip() or "git init failed"
        raise GitopsError(detail)
    _run(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")


def assert_hive_repo(hive: Path) -> None:
    hive = hive.resolve()
    result = _run(hive, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise NotHiveRepo(f"{hive} is not a git work tree")
    top = Path(result.stdout.strip()).resolve()
    if top != hive:
        raise NotHiveRepo(f"{hive} is not the hive work-tree root (toplevel={top})")


def assert_clean(hive: Path) -> None:
    if _out(hive, "status", "--porcelain"):
        raise DirtyHive("hive work-tree is dirty")


def upstream(hive: Path) -> str:
    result = _run(
        hive, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
    )
    if result.returncode != 0:
        raise GitopsError("hive branch has no upstream")
    return result.stdout.strip()


def _fetch(hive: Path) -> None:
    _run(hive, "fetch")


def _iter_porcelain_z(raw: str) -> Iterator[tuple[str, str, str | None]]:
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        item = parts[i]
        if not item:
            i += 1
            continue
        xy = item[:2]
        path = item[3:] if len(item) > 3 else ""
        extra = None
        if "R" in xy or "C" in xy:
            i += 1
            extra = parts[i] if i < len(parts) else ""
        yield xy, path, extra
        i += 1


def _untracked_relpaths(hive: Path) -> list[str]:
    raw = _run(hive, "status", "-z", "--porcelain", "-uall").stdout
    return [path for xy, path, _extra in _iter_porcelain_z(raw) if xy == "??"]


def _drop_untracked(hive: Path) -> None:
    hive = hive.resolve()
    for rel in _untracked_relpaths(hive):
        path = (hive / rel).resolve()
        try:
            path.relative_to(hive)
        except ValueError:
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _reset_upstream(hive: Path) -> None:
    # reset --hard does not remove untracked files the failed op created.
    _run(hive, "reset", "--hard", "@{u}")
    _drop_untracked(hive)


def _read_remote_claim(hive: Path, task_id: str) -> dict | None:
    if task_id == "__none__":
        return None
    up = upstream(hive)
    result = _run(hive, "show", f"{up}:claims/{task_id}.json", check=False)
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        return None
    return data


def remote_claim(hive: Path, task_id: str) -> dict | None:
    _fetch(hive)
    return _read_remote_claim(hive, task_id)


def _holder(doc: dict | None) -> str | None:
    if not doc:
        return None
    agent = doc.get("agent")
    return agent if isinstance(agent, str) else None


def _held_by_other(doc: dict | None, ours: str | None) -> bool:
    holder = _holder(doc)
    return holder is not None and ours is not None and holder != ours


def _unexpired_held_by_other(
    doc: dict | None, ours: str | None, now: datetime | None
) -> bool:
    if not _held_by_other(doc, ours) or doc is None:
        return False
    if now is None:
        return True
    expires = doc.get("expires_at")
    if not isinstance(expires, str):
        return True
    try:
        return parse_z(expires) > now
    except ValueError:
        return True


def _ff_only(hive: Path) -> None:
    behind = int(_out(hive, "rev-list", "--count", "HEAD..@{u}"))
    if behind == 0:
        return
    _run(hive, "merge", "--ff-only", "--no-edit", "@{u}")


def _commit_op(hive: Path, message: str) -> None:
    _run(hive, "add", "-u")
    untracked = _untracked_relpaths(hive)
    if untracked:
        _run(hive, "add", "--", *untracked)
    _run(hive, "commit", "-m", message)


def _push_with_retries(
    hive: Path,
    *,
    task_id: str,
    agent: str | None,
    now: datetime | None,
    max_attempts: int,
) -> None:
    for attempt in range(max_attempts):
        pushed = _run(hive, "push", check=False)
        if pushed.returncode == 0:
            return
        _fetch(hive)
        if _unexpired_held_by_other(_read_remote_claim(hive, task_id), agent, now):
            _reset_upstream(hive)
            raise ClaimDenied("lost race on remote tip")
        if attempt >= max_attempts - 1:
            _reset_upstream(hive)
            raise GitopsError("push rejected after max attempts")
        rebased = _run(hive, "rebase", "@{u}", check=False)
        if rebased.returncode != 0:
            _run(hive, "rebase", "--abort", check=False)
            _reset_upstream(hive)
            raise GitopsError("rebase onto upstream failed")
    _reset_upstream(hive)
    raise GitopsError("push rejected after max attempts")


def publish(
    hive: Path,
    *,
    task_id: str,
    op: Callable[[], dict],
    message: str,
    max_attempts: int = 5,
    agent: str | None = None,
    now: datetime | None = None,
) -> dict:
    hive = hive.resolve()
    assert_hive_repo(hive)
    assert_clean(hive)
    upstream(hive)

    if _out(hive, "rev-list", "@{u}..HEAD"):
        _fetch(hive)
        if _held_by_other(_read_remote_claim(hive, task_id), agent):
            _reset_upstream(hive)
            raise ClaimDenied("lost race on remote tip")
        raise GitopsError("unpushed hive commits; push or discard before publish")

    _fetch(hive)
    if _unexpired_held_by_other(_read_remote_claim(hive, task_id), agent, now):
        _reset_upstream(hive)
        raise ClaimDenied("lost race on remote tip")

    _ff_only(hive)
    denial: ClaimDenied | None = None
    try:
        doc = op()
    except ClaimDenied as e:
        if not e.commit:
            _reset_upstream(hive)
            raise
        denial = e
        doc = {}
    except Exception:
        _reset_upstream(hive)
        raise

    try:
        if not _out(hive, "status", "--porcelain"):
            if denial is not None:
                raise denial
            raise GitopsError("nothing to commit")
        _commit_op(hive, message)
        _push_with_retries(
            hive,
            task_id=task_id,
            agent=agent,
            now=now,
            max_attempts=max_attempts,
        )
    except ClaimDenied:
        raise
    except Exception:
        _reset_upstream(hive)
        raise
    if denial is not None:
        raise denial
    return doc


def claim_and_publish(
    hive: Path,
    *,
    task_id: str,
    agent: str,
    harness: str,
    now: datetime,
    lease_seconds: int,
    note: str | None = None,
) -> dict:
    def op() -> dict:
        return try_claim(hive, task_id, agent, harness, now, lease_seconds, note)

    return publish(
        hive,
        task_id=task_id,
        op=op,
        message=f"claim {task_id}",
        agent=agent,
        now=now,
    )


def promote_and_publish(
    hive: Path,
    *,
    agent: str,
    harness: str,
    now: datetime,
    lease_seconds: int,
    reason: str,
    allow_self_promote: bool,
    operators: list[str],
    by: str | None = None,
) -> dict:
    def op() -> dict:
        return promote(
            hive,
            agent=agent,
            harness=harness,
            now=now,
            lease_seconds=lease_seconds,
            reason=reason,
            allow_self_promote=allow_self_promote,
            operators=operators,
            by=by,
        )

    return publish(
        hive,
        task_id="orchestrator",
        op=op,
        message=f"promote {agent}",
        agent=agent,
        now=now,
    )
