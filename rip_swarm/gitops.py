# rip_swarm/gitops.py
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied
from rip_swarm.orchestrator import promote
from rip_swarm.policy import try_claim_with_policy
from rip_swarm.profile import load_profile
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
    """Remove untracked paths a failed op left behind, without following symlinks.

    Containment is decided on the *parent* directory, so a symlink whose target lives
    outside the hive is still dropped (resolving the link itself would place it outside
    the hive and leave it behind forever, keeping the hive permanently DirtyHive).
    Symlinks are unlinked before any is_file / is_dir test, so the link is removed and
    whatever it points at -- e.g. a file in the project checkout -- is never touched.
    """
    hive = hive.resolve()
    for rel in _untracked_relpaths(hive):
        raw = hive / rel.rstrip("/")
        try:
            path = raw.parent.resolve() / raw.name
        except OSError:
            continue
        try:
            path.relative_to(hive)
        except ValueError:
            continue
        if path.is_symlink():
            path.unlink()
        elif path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _reset_upstream(hive: Path) -> None:
    # reset --hard does not remove untracked files the failed op created.
    _run(hive, "reset", "--hard", "@{u}")
    _drop_untracked(hive)


class _UnreadableClaim:
    """Sentinel: a claim file exists on the remote tip but cannot be parsed.

    Fail closed -- an unparseable claim is treated as held by someone else, so a
    corrupt remote tip denies the claim instead of crashing every publish.
    """

    __slots__ = ()


UNREADABLE_CLAIM = _UnreadableClaim()


def _read_remote_claim(hive: Path, task_id: str) -> dict | _UnreadableClaim | None:
    if task_id == "__none__":
        return None
    up = upstream(hive)
    result = _run(hive, "show", f"{up}:claims/{task_id}.json", check=False)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        return UNREADABLE_CLAIM
    if not isinstance(data, dict):
        return UNREADABLE_CLAIM
    return data


def _deny_if_unreadable(doc: object, task_id: str) -> None:
    if isinstance(doc, _UnreadableClaim):
        raise ClaimDenied(f"unreadable claim on remote tip for {task_id}")


def remote_claim(hive: Path, task_id: str) -> dict | None:
    _fetch(hive)
    doc = _read_remote_claim(hive, task_id)
    return doc if isinstance(doc, dict) else None


def _holder(doc: object) -> str | None:
    if not isinstance(doc, dict) or not doc:
        return None
    agent = doc.get("agent")
    return agent if isinstance(agent, str) else None


def _held_by_other(doc: object, ours: str | None) -> bool:
    holder = _holder(doc)
    return holder is not None and ours is not None and holder != ours


def _unexpired_held_by_other(
    doc: object, ours: str | None, now: datetime | None
) -> bool:
    if not _held_by_other(doc, ours) or not isinstance(doc, dict):
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


def default_allow(task_id: str, agent: str | None) -> list[str]:
    """Paths an ordinary claim-lifecycle op is allowed to write (§8.5).

    Hive-relative glob patterns. `claims/<task_id>.*.json` covers the tombstones
    `_finalize` / `tombstone_claim` write next to the active claim path.
    """
    allow = ["store/claims.jsonl", "store/messages.jsonl"]
    if task_id and task_id != "__none__":
        tid = _glob_quote(task_id)
        allow += [
            f"claims/{tid}.json",
            f"claims/{tid}.*.json",
        ]
    if task_id == "orchestrator":
        allow.append("orchestrator/CURRENT.json")
    if agent:
        allow.append(f"agents/{_glob_quote(agent)}/outbox/*.json")
    return allow


def promote_allow(agent: str, by: str | None) -> list[str]:
    """Paths an `orchestrator.promote` op is allowed to write (§8.5, C1).

    `promote` writes the audit message to the *`by`* agent's outbox, not the
    promoted agent's, so `default_allow("orchestrator", agent)` alone is too
    narrow whenever an operator promotes someone else (`by != agent`). Add the
    `by` outbox pattern too, skipping the duplicate when they're the same id.
    """
    allow = default_allow("orchestrator", agent)
    if by and by != agent:
        allow.append(f"agents/{_glob_quote(by)}/outbox/*.json")
    return allow


def _glob_quote(literal: str) -> str:
    """Escape glob metacharacters so an id is matched literally, never as a pattern."""
    return "".join("[" + ch + "]" if ch in "*?[]" else ch for ch in literal)


def _match_allow(rel: str, patterns: tuple[str, ...]) -> bool:
    """Whole-path glob match: `*` never crosses a `/`, unlike PurePath.match."""
    segments = rel.split("/")
    for pattern in patterns:
        parts = pattern.split("/")
        if len(parts) != len(segments):
            continue
        if all(fnmatch.fnmatchcase(s, p) for s, p in zip(segments, parts)):
            return True
    return False


def _changed_relpaths(hive: Path) -> list[str]:
    """Every path the op touched, relative to the hive root.

    The tree was clean before `op` ran, so `git status` *is* the write set. A rename
    reports both sides; both must be inside the allowlist.
    """
    raw = _run(hive, "status", "-z", "--porcelain", "-uall").stdout
    out: list[str] = []
    for _xy, path, extra in _iter_porcelain_z(raw):
        for item in (path, extra):
            if item:
                out.append(item.rstrip("/"))
    return sorted(set(out))


def _commit_op(hive: Path, message: str, allow: tuple[str, ...]) -> None:
    """Stage exactly the paths the op wrote, and only if all of them are allowed.

    Spec §8.5 says "commit only the paths written": a generic `git add -A` would
    sweep any scratch file, secret, or leftover living in the hive into permanent
    hive history (no force-push allowed to undo it). Anything outside `allow` is a
    bug in the op, so the whole op is refused -- its own writes are not committed
    either, because a half-trusted commit is worse than none.
    """
    changed = _changed_relpaths(hive)
    unexpected = [rel for rel in changed if not _match_allow(rel, allow)]
    if unexpected:
        raise GitopsError(
            "op wrote paths outside its allowlist: " + ", ".join(unexpected)
        )
    if not changed:
        raise GitopsError("nothing to commit")
    _run(hive, "add", "--", *changed)
    _run(hive, "commit", "-m", message)


def _rebase_conflicts_claim(hive: Path, task_id: str) -> bool:
    """True when the in-progress rebase is conflicted on `claims/<task_id>.json`.

    An add/add or content conflict on a claim file means someone else's claim reached
    the remote tip first (§8.3: claim files never union-merge, a conflict means you
    lost), so the caller converts it into ClaimDenied rather than a generic error.
    """
    if task_id == "__none__":
        return False
    result = _run(hive, "diff", "--name-only", "--diff-filter=U", "-z", check=False)
    if result.returncode != 0:
        return False
    names = [n for n in result.stdout.split("\0") if n]
    return f"claims/{task_id}.json" in names


def _push_with_retries(
    hive: Path,
    *,
    task_id: str,
    agent: str,
    now: datetime,
    max_attempts: int,
) -> None:
    for attempt in range(max_attempts):
        pushed = _run(hive, "push", check=False)
        if pushed.returncode == 0:
            return
        _fetch(hive)
        remote = _read_remote_claim(hive, task_id)
        if isinstance(remote, _UnreadableClaim):
            _reset_upstream(hive)
            raise ClaimDenied(f"unreadable claim on remote tip for {task_id}")
        if _unexpired_held_by_other(remote, agent, now):
            _reset_upstream(hive)
            raise ClaimDenied("lost race on remote tip")
        if attempt >= max_attempts - 1:
            _reset_upstream(hive)
            raise GitopsError("push rejected after max attempts")
        rebased = _run(hive, "rebase", "@{u}", check=False)
        if rebased.returncode != 0:
            lost = _rebase_conflicts_claim(hive, task_id)
            _run(hive, "rebase", "--abort", check=False)
            _reset_upstream(hive)
            if lost:
                # `remote` passed `_unexpired_held_by_other` above, so a foreign holder
                # here is an *expired* one: stealable, not a lost race. The op closure has
                # already run against a reset tree, so publish cannot retry it itself --
                # it says so and the caller re-runs.
                if _held_by_other(remote, agent):
                    raise ClaimDenied(
                        "expired claim reached the remote tip first; retry to steal it"
                    )
                raise ClaimDenied("lost race on remote tip")
            raise GitopsError("rebase onto upstream failed")
    _reset_upstream(hive)
    raise GitopsError("push rejected after max attempts")


def publish(
    hive: Path,
    *,
    task_id: str,
    op: Callable[[], dict],
    message: str,
    agent: str,
    now: datetime,
    max_attempts: int = 5,
    allow: Iterable[str] | None = None,
) -> dict:
    """Run `op` in the hive and push the resulting commit (§8.5).

    `agent` and `now` are required: without them the remote-tip check cannot tell our
    own claim from a foreign one, nor a live lease from an expired one, so the race
    protection silently degrades and a lost race surfaces as a rebase failure.

    `allow` is the set of hive-relative glob patterns this op may write; `None` means
    the claim-lifecycle default for `task_id` / `agent` (`default_allow`). Callers
    that write elsewhere -- lookback reports, new inbox tasks -- pass their own
    patterns. Writing outside the allowlist aborts the publish, reverts the tree and
    raises GitopsError: see `_commit_op`.

    Unpushed local commits (rule): publish never `reset --hard`s over a commit the
    remote does not have. When HEAD is ahead of `@{u}` publish refuses without
    touching history -- ClaimDenied("lost race on remote tip") if the remote tip
    carries an *unexpired foreign* claim on `task_id` (expiry-aware, so an expired
    foreign claim stays stealable), ClaimDenied("unreadable claim ...") if that claim
    is corrupt, and otherwise GitopsError("unpushed hive commits; push or discard
    before publish"). Either way HEAD is unchanged and the operator decides whether to
    push or discard. `reset --hard` remains in use only on paths where every local
    commit is already on the remote.
    """
    hive = hive.resolve()
    patterns = tuple(default_allow(task_id, agent) if allow is None else allow)
    assert_hive_repo(hive)
    assert_clean(hive)
    upstream(hive)

    if _out(hive, "rev-list", "@{u}..HEAD"):
        _fetch(hive)
        remote = _read_remote_claim(hive, task_id)
        _deny_if_unreadable(remote, task_id)
        if _unexpired_held_by_other(remote, agent, now):
            raise ClaimDenied("lost race on remote tip")
        raise GitopsError("unpushed hive commits; push or discard before publish")

    _fetch(hive)
    remote = _read_remote_claim(hive, task_id)
    if isinstance(remote, _UnreadableClaim):
        _reset_upstream(hive)
        raise ClaimDenied(f"unreadable claim on remote tip for {task_id}")
    if _unexpired_held_by_other(remote, agent, now):
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
        _commit_op(hive, message, patterns)
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
    lease_seconds: int | None = None,
    note: str | None = None,
    profile: dict | None = None,
) -> dict:
    """Claim `task_id` under the hive's policy, then publish (§5 trust + budget).

    Routed through `try_claim_with_policy`, not raw `try_claim`: the registry check
    and `max_claims_open_per_agent` are part of the claim contract, so a harness that
    imports this helper cannot claim as an unregistered id or past its cap. The lease
    comes from the profile's `worker_lease_ttl` unless `lease_seconds` overrides it.
    """
    cfg = load_profile(hive, None) if profile is None else profile
    if lease_seconds is not None:
        cfg = {**cfg, "worker_lease_ttl": f"{int(lease_seconds)}s"}

    def op() -> dict:
        return try_claim_with_policy(
            hive,
            task_id=task_id,
            agent=agent,
            harness=harness,
            now=now,
            profile=cfg,
            note=note,
        )

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
        allow=promote_allow(agent, by),
    )
