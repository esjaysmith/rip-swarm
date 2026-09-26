# rip_swarm/join.py — join and leave (spec §4, §5)
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rip_swarm import __version__
from rip_swarm.board import read_board
from rip_swarm.claim import ClaimDenied, release
from rip_swarm.fold import Expired, Holder, active_holder
from rip_swarm.gitops import (
    GitopsError,
    _glob_quote,
    can_publish,
    promote_and_publish,
    publish_or_apply,
    register_member,
    remote_claim,
    sync,
)
from rip_swarm.ids import new_ulid
from rip_swarm.init_hive import bootstrap_or_attach, clone_hive
from rip_swarm.members import has_left, write_left
from rip_swarm.orchestrator import release_orchestrator
from rip_swarm.profile import load_profile
from rip_swarm.project import (
    INTEGRATION,
    Project,
    branch_exists,
    ensure_excluded,
    ensure_worktree,
    identity,
    is_merged,
    main_is_dirty,
    remove_worktree,
    resolve_project,
    worktree_is_clean,
)
from rip_swarm.registry import UnknownAgent, require_agent
from rip_swarm.state import save_state, seed_state
from rip_swarm.timeutil import format_z, parse_duration

MASTER_MIGRATION = (
    "this hive predates the operator entry, so a master cannot be seated.\n"
    "Publish this operator edit once from any hive clone, then re-run join:\n"
    "  agents/registry.yaml:   add  - id: op / harness: human / role: operator\n"
    "  profiles/default.yaml:  set  operators: [op]\n"
    "  git -C <hive> add agents/registry.yaml profiles/default.yaml\n"
    "  git -C <hive> commit -m 'seat op' && git -C <hive> push"
)
HEAD_NOTE = "based on HEAD (no rip-swarm/integration yet)"
DIRTY_NOTE = "MAIN has uncommitted changes; they are not on this branch"


@dataclass
class JoinResult:
    agent: str
    role: str
    harness: str
    hive: Path
    worktree: Path
    branch: str
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"VERSION={__version__}",
            f"AGENT={self.agent}",
            f"ROLE={self.role}",
            f"HARNESS={self.harness}",
            f"HIVE={self.hive}",
            f"WORKTREE={self.worktree}",
            f"BRANCH={self.branch}",
            f"INTEGRATION={INTEGRATION}",
        ]
        return out + [f"NOTE={note}" for note in self.notes]


def join(
    start: Path,
    *,
    role: str,
    harness: str,
    now: datetime,
    template: Path | None = None,
) -> JoinResult:
    if role not in ("worker", "master"):
        raise ValueError(f"--role must be worker or master, got {role!r}")
    project = resolve_project(start)
    name, email = identity(project)
    bootstrap_or_attach(project.origin_url, name=name, email=email, template=template)
    project.hives.mkdir(parents=True, exist_ok=True)
    pending = project.hives / f"pending-{new_ulid(now)}"
    clone_hive(project.origin_url, pending, name=name, email=email)
    try:
        if role == "master":
            _preflight_master(pending, now)
        member = register_member(pending, harness=harness, now=now)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    agent = member["id"]
    hive = project.hives / f"hive-{agent}"
    pending.rename(hive)
    try:
        result = _setup_role(project, hive, agent=agent, role=role, harness=harness, now=now)
        ensure_excluded(project)
        save_state(hive, seed_state(hive, agent))
    except ClaimDenied:
        holder = _baton_holder(hive)
        _undo(hive, agent, now)
        raise ClaimDenied(f"another master holds the baton: {holder}") from None
    except BaseException:
        _undo(hive, agent, now)
        raise
    if main_is_dirty(project):
        result.notes.append(DIRTY_NOTE)
    return result


def _preflight_master(hive: Path, now: datetime) -> None:
    try:
        require_agent(hive, "op")
    except UnknownAgent:
        raise GitopsError(MASTER_MIGRATION) from None
    operators = [str(x) for x in (load_profile(hive, None).get("operators") or [])]
    if "op" not in operators:
        raise GitopsError(MASTER_MIGRATION)
    rec = active_holder(hive, "orchestrator", now)
    if isinstance(rec, Holder):
        raise ClaimDenied(
            f"another master holds the baton: {rec.agent} until {format_z(rec.expires_at)}"
        )


def _setup_role(
    project: Project, hive: Path, *, agent: str, role: str, harness: str, now: datetime
) -> JoinResult:
    notes: list[str] = []
    if role == "worker":
        branch = f"rip-swarm/{agent}"
        base = INTEGRATION if branch_exists(project, INTEGRATION) else "HEAD"
        if base == "HEAD":
            notes.append(HEAD_NOTE)
        worktree = project.worktrees / agent
        ensure_worktree(project, worktree, branch, base)
        return JoinResult(agent, "worker", harness, hive, worktree.resolve(), branch, notes)
    profile = load_profile(hive, None)
    promote_and_publish(
        hive,
        agent=agent,
        harness=harness,
        now=now,
        lease_seconds=parse_duration(profile["orchestrator_lease_ttl"]),
        reason="joined as master",
        allow_self_promote=False,
        operators=[str(x) for x in (profile.get("operators") or [])],
        by="op",
    )
    if not branch_exists(project, INTEGRATION):
        notes.append(HEAD_NOTE)
    worktree = project.worktrees / "integration"
    ensure_worktree(project, worktree, INTEGRATION, "HEAD")
    return JoinResult(agent, "master", harness, hive, worktree.resolve(), INTEGRATION, notes)


def _baton_holder(hive: Path) -> str:
    try:
        doc = remote_claim(hive, "orchestrator")
    except GitopsError:
        doc = None
    if not doc:
        return "unknown holder"
    return f"{doc.get('agent')} until {doc.get('expires_at')}"


def _undo(hive: Path, agent: str, now: datetime) -> None:
    """Same order as leave (spec §4.1 step 7): baton, member, clone."""
    try:
        leave(hive, agent, now)
    except Exception:
        shutil.rmtree(hive, ignore_errors=True)


def leave(hive: Path, agent: str, now: datetime) -> list[str]:
    """Release claims and baton, tombstone the member, tidy the worktree,
    remove the clone (spec §5). Steps 2-3 failing keeps the clone so leave can
    be re-run; step 4 failing is a note."""
    hive = Path(hive)
    if not hive.exists():
        return ["already left"]
    if can_publish(hive):
        sync(hive)
    if has_left(hive, agent):
        shutil.rmtree(hive, ignore_errors=True)
        return ["already left"]
    lines: list[str] = []
    for tid, view in sorted(read_board(hive, now).items()):
        if view.claim == "live" and view.holder == agent:
            publish_or_apply(
                hive, task_id=tid, message=f"release {tid}", agent=agent, now=now,
                op=lambda tid=tid: release(hive, tid, agent, now, "left the swarm"),
            )
            lines.append(f"released {tid}")
    baton = active_holder(hive, "orchestrator", now)
    if isinstance(baton, (Holder, Expired)) and baton.agent == agent:
        publish_or_apply(
            hive, task_id="orchestrator", message="release orchestrator", agent=agent, now=now,
            op=lambda: release_orchestrator(hive, agent=agent, now=now, note="left the swarm"),
        )
        lines.append("released orchestrator")
    publish_or_apply(
        hive, task_id="__none__", message=f"leave {agent}", agent=agent, now=now,
        op=lambda: write_left(hive, agent, now),
        allow=[f"agents/{_glob_quote(agent)}/member.left.*.json"],
    )
    lines.append(f"left as {agent}")
    lines += _tidy_worktree(hive, agent)
    shutil.rmtree(hive, ignore_errors=True)
    lines.append("removed hive clone")
    return lines


def _project_of(hive: Path) -> Project | None:
    """`<main>/.git/rip-swarm/hive-<id>` → the project; None for other layouts."""
    if hive.parent.name != "rip-swarm" or not hive.name.startswith("hive-"):
        return None
    try:
        return resolve_project(hive.parent.parent.parent)
    except GitopsError:
        return None


def _tidy_worktree(hive: Path, agent: str) -> list[str]:
    project = _project_of(hive)
    if project is None:
        return []
    path = (project.worktrees / agent).resolve()
    if not path.exists():
        return []
    try:
        if not worktree_is_clean(path):
            return [f"kept {path}: uncommitted changes"]
        if not is_merged(project, f"rip-swarm/{agent}", INTEGRATION):
            return [f"kept {path}: rip-swarm/{agent} is not merged into {INTEGRATION}"]
        remove_worktree(project, path)
        return [f"removed {path}"]
    except GitopsError as e:
        return [f"kept {path}: {e}"]
