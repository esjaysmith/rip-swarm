# rip_swarm/cli.py
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from rip_swarm.claim import ClaimDenied, complete, heartbeat, reject, release
from rip_swarm.gitops import GitopsError, NotHiveRepo, assert_hive_repo, publish, upstream
from rip_swarm.inbox import create_task
from rip_swarm.init_hive import init_hive
from rip_swarm.lookback import write_lookback
from rip_swarm.orchestrator import heartbeat_orchestrator, promote, release_orchestrator
from rip_swarm.paths import resolve_hive
from rip_swarm.policy import try_claim_with_policy
from rip_swarm.profile import load_profile
from rip_swarm.status import format_status, status_report
from rip_swarm.timeutil import now_utc, parse_duration


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        result = _dispatch(args)
    except ClaimDenied as e:
        print(e, file=sys.stderr)
        return 2
    except Exception as e:
        print(e, file=sys.stderr)
        return 1
    if isinstance(result, str):
        if not result.endswith("\n"):
            result += "\n"
        sys.stdout.write(result)
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hive")
    common.add_argument("--profile")
    common.add_argument("--agent")
    common.add_argument("--harness")
    common.add_argument("--local", action="store_true")

    parser = argparse.ArgumentParser(
        prog="rip-swarm",
        description="Coordinate harnesses on a git-backed hive.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", parents=[common], help="bootstrap or attach the swarm hive")
    init_p.add_argument("--force", action="store_true")
    init_p.add_argument("--no-git", action="store_true")

    inbox_p = sub.add_parser("inbox-add", parents=[common], help="create an inbox task")
    inbox_p.add_argument("--title", required=True)
    inbox_p.add_argument("--created-by", required=True)
    inbox_p.add_argument("--body")

    claim_p = sub.add_parser("claim", parents=[common], help="claim a task")
    claim_p.add_argument("--task", required=True)
    claim_p.add_argument("--note")

    hb_p = sub.add_parser("heartbeat", parents=[common], help="extend a held claim")
    hb_p.add_argument("--task", required=True)

    complete_p = sub.add_parser("complete", parents=[common], help="complete a held claim")
    complete_p.add_argument("--task", required=True)
    complete_p.add_argument("--result-ref", required=True)
    complete_p.add_argument("--note")

    for name, help_text in (
        ("release", "release a held claim"),
        ("reject", "reject a held claim"),
    ):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--task", required=True)
        p.add_argument("--note")

    promo = sub.add_parser("promote", parents=[common], help="promote an orchestrator")
    promo.add_argument("--by")
    promo.add_argument("--reason", default="")

    sub.add_parser("status", parents=[common], help="read-only hive doctor")
    sub.add_parser("lookback", parents=[common], help="write a lookback report")
    return parser


def _dispatch(args: argparse.Namespace) -> object:
    if args.command == "init":
        dest = _init_dest(args.hive)
        return init_hive(dest, force=args.force, git_init=not args.no_git)

    hive = resolve_hive(args.hive)
    now = now_utc()
    if args.command == "status":
        return format_status(status_report(hive, now))
    if args.command == "lookback":
        path = write_lookback(hive, now, load_profile(hive, args.profile))
        return str(path)
    if args.command == "inbox-add":
        return _inbox_add(args, hive, now)
    if args.command == "complete":
        return _complete(args, hive, now)
    if args.command == "release":
        return _release(args, hive, now)
    if args.command == "reject":
        return _reject(args, hive, now)
    profile = load_profile(hive, args.profile)
    if args.command == "claim":
        return _claim(args, hive, now, profile)
    if args.command == "heartbeat":
        return _heartbeat(args, hive, now, profile)
    if args.command == "promote":
        return _promote(args, hive, now, profile)
    raise ValueError(f"unknown command: {args.command}")


def _init_dest(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    val = os.environ.get("RIP_SWARM_HIVE")
    if val:
        return Path(val).expanduser().resolve()
    return (Path.cwd() / "_swarm").resolve()


def _require(value: str | None, flag: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{flag} is required")
    return str(value)


def _inbox_add(args: argparse.Namespace, hive: Path, now: datetime) -> dict:
    def op() -> dict:
        return create_task(
            hive,
            title=args.title,
            created_by=args.created_by,
            body=args.body,
            now=now,
        )

    return _run_op(
        hive,
        local=args.local,
        task_id="__none__",
        message=f"inbox-add {args.title}",
        op=op,
        now=now,
    )


def _claim(args: argparse.Namespace, hive: Path, now: datetime, profile: dict) -> dict:
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")
    harness = _require(args.harness, "--harness")

    def op() -> dict:
        return try_claim_with_policy(
            hive,
            task_id=task_id,
            agent=agent,
            harness=harness,
            now=now,
            profile=profile,
            note=args.note,
        )

    return _run_op(
        hive,
        local=args.local,
        task_id=task_id,
        message=f"claim {task_id}",
        op=op,
        agent=agent,
        now=now,
    )


def _heartbeat(args: argparse.Namespace, hive: Path, now: datetime, profile: dict) -> dict:
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")
    if task_id == "orchestrator":
        lease = parse_duration(profile["orchestrator_lease_ttl"])

        def op() -> dict:
            return heartbeat_orchestrator(
                hive, agent=agent, now=now, lease_seconds=lease
            )

    else:
        lease = parse_duration(profile["worker_lease_ttl"])

        def op() -> dict:
            return heartbeat(hive, task_id, agent, now, lease)

    return _run_op(
        hive,
        local=args.local,
        task_id=task_id,
        message=f"heartbeat {task_id}",
        op=op,
        agent=agent,
        now=now,
    )


def _complete(args: argparse.Namespace, hive: Path, now: datetime) -> dict:
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")
    result_ref = _require(args.result_ref, "--result-ref")

    def op() -> dict:
        return complete(hive, task_id, agent, now, result_ref, args.note)

    return _run_op(
        hive,
        local=args.local,
        task_id=task_id,
        message=f"complete {task_id}",
        op=op,
        agent=agent,
        now=now,
    )


def _release(args: argparse.Namespace, hive: Path, now: datetime) -> dict:
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")
    if task_id == "orchestrator":

        def op() -> dict:
            return release_orchestrator(hive, agent=agent, now=now, note=args.note)

    else:

        def op() -> dict:
            return release(hive, task_id, agent, now, args.note)

    return _run_op(
        hive,
        local=args.local,
        task_id=task_id,
        message=f"release {task_id}",
        op=op,
        agent=agent,
        now=now,
    )


def _reject(args: argparse.Namespace, hive: Path, now: datetime) -> dict:
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")

    def op() -> dict:
        return reject(hive, task_id, agent, now, args.note)

    return _run_op(
        hive,
        local=args.local,
        task_id=task_id,
        message=f"reject {task_id}",
        op=op,
        agent=agent,
        now=now,
    )


def _promote(args: argparse.Namespace, hive: Path, now: datetime, profile: dict) -> dict:
    agent = _require(args.agent, "--agent")
    harness = _require(args.harness, "--harness")
    operators = profile.get("operators") or []
    if not isinstance(operators, list):
        operators = []
    operators = [str(item) for item in operators]
    lease = parse_duration(profile["orchestrator_lease_ttl"])

    def op() -> dict:
        return promote(
            hive,
            agent=agent,
            harness=harness,
            now=now,
            lease_seconds=lease,
            reason=args.reason or "",
            allow_self_promote=bool(profile.get("allow_self_promote")),
            operators=operators,
            by=args.by,
        )

    return _run_op(
        hive,
        local=args.local,
        task_id="orchestrator",
        message=f"promote {agent}",
        op=op,
        agent=agent,
        now=now,
    )


def _run_op(
    hive: Path,
    *,
    local: bool,
    task_id: str,
    message: str,
    op: Callable[[], dict],
    agent: str | None = None,
    now: datetime | None = None,
) -> dict:
    if local:
        return op()
    _require_publishable(hive)
    captured: dict[str, dict] = {}

    def wrapped() -> dict:
        captured["doc"] = op()
        return captured["doc"]

    try:
        return publish(
            hive,
            task_id=task_id,
            op=wrapped,
            message=message,
            agent=agent,
            now=now,
        )
    except GitopsError as e:
        if captured.get("doc") is not None and "nothing to commit" in str(e).lower():
            return captured["doc"]
        raise


def _require_publishable(hive: Path) -> None:
    try:
        assert_hive_repo(hive)
    except NotHiveRepo as e:
        raise GitopsError(f"{e}\nOr pass --local to skip git.") from e
    try:
        upstream(hive)
    except GitopsError as e:
        raise GitopsError(
            "hive branch has no upstream; cannot publish.\n"
            "Set upstream and push (never force-push), then retry:\n"
            f"  git -C {hive} push -u origin HEAD\n"
            "Or pass --local to skip git."
        ) from e
