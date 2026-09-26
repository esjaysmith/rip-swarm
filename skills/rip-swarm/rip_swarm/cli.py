# rip_swarm/cli.py
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from rip_swarm import __version__
from rip_swarm.claim import ClaimDenied, complete, heartbeat, reject, release
from rip_swarm.gitops import (
    DirtyHive,
    GitopsError,
    NotHiveRepo,
    assert_hive_repo,
    promote_allow,
    publish,
    sync,
    upstream,
)
from rip_swarm.inbox import create_task
from rip_swarm.outbox import write_message
from rip_swarm.init_hive import init_hive
from rip_swarm.lookback import write_lookback
from rip_swarm.messages import format_messages, list_messages
from rip_swarm.orchestrator import heartbeat_orchestrator, promote, release_orchestrator
from rip_swarm.paths import resolve_hive
from rip_swarm.policy import try_claim_with_policy
from rip_swarm.profile import load_profile
from rip_swarm.registry import require_agent
from rip_swarm.status import format_status, status_report
from rip_swarm.timeutil import now_utc, parse_duration


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        result = _dispatch(args)
    except ClaimDenied as e:
        print(e, file=sys.stderr)
        return 2
    except DirtyHive as e:
        print(f"{e}\n{_dirty_hint(_hive_for_hint(args))}", file=sys.stderr)
        return 1
    except Exception as e:
        print(e, file=sys.stderr)
        return 1
    if isinstance(result, dict):
        result = _summary(args, result)
    if isinstance(result, str):
        if not result.endswith("\n"):
            result += "\n"
        sys.stdout.write(result)
    return 0


def _parser() -> argparse.ArgumentParser:
    # Shared flags without --agent (message uses --from instead).
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--hive")
    base.add_argument("--profile")
    base.add_argument("--harness")
    base.add_argument("--local", action="store_true")

    common = argparse.ArgumentParser(add_help=False, parents=[base])
    common.add_argument("--agent")

    parser = argparse.ArgumentParser(
        prog="rip-swarm",
        description="Coordinate harnesses on a git-backed hive.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the rip-swarm version")

    init_p = sub.add_parser("init", help="bootstrap or attach the swarm hive")
    init_p.add_argument("--hive")
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

    msg_p = sub.add_parser(
        "message",
        parents=[base],
        help="send an open-ended agent message (no claim required)",
    )
    msg_p.add_argument("--from", dest="from_agent", required=True)
    msg_p.add_argument("--to", required=True)
    msg_p.add_argument("--type", required=True)
    msg_p.add_argument("--body", required=True)

    sub.add_parser("status", parents=[common], help="read-only hive doctor")
    sub.add_parser(
        "sync", parents=[base], help="fetch and fast-forward the hive to origin/swarm"
    )
    msgs_p = sub.add_parser(
        "messages", parents=[base], help="read-only: list messages, oldest first"
    )
    msgs_p.add_argument("--to", help="addressed to this agent (direct, *, baton)")
    msgs_p.add_argument("--from", dest="from_agent")
    msgs_p.add_argument("--since", help="only messages after this UTC Z timestamp")
    msgs_p.add_argument("--type")
    sub.add_parser("lookback", parents=[common], help="write a lookback report")
    return parser


def _dispatch(args: argparse.Namespace) -> object:
    if args.command == "version":
        return f"rip-swarm {__version__}"
    if args.command == "init":
        dest = _init_dest(args.hive)
        status = init_hive(dest, force=args.force, git_init=not args.no_git)
        return _init_message(status, dest)

    hive = resolve_hive(args.hive)
    now = now_utc()
    if getattr(args, "local", False) and _hive_can_publish(hive):
        _gate_local_on_publishable(hive)
    if args.command == "status":
        # Resolve the profile the same way every other command does so an
        # unknown --profile behaves consistently (profile.py falls back).
        load_profile(hive, args.profile)
        return format_status(status_report(hive, now))
    if args.command == "sync":
        return _sync(hive)
    if args.command == "messages":
        found, unreadable = list_messages(
            hive,
            now=now,
            to=args.to,
            since=args.since,
            frm=args.from_agent,
            type=args.type,
        )
        return format_messages(found, unreadable)
    if args.command == "lookback":
        return _lookback(args, hive, now)
    if args.command == "inbox-add":
        return _inbox_add(args, hive, now)
    if args.command == "message":
        return _message(args, hive, now)
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


def _sync(hive: Path) -> str:
    doc = sync(hive)
    if doc["pulled"] == 0:
        return f"hive up to date at {doc['head']}"
    plural = "" if doc["pulled"] == 1 else "s"
    return f"pulled {doc['pulled']} commit{plural}; hive at {doc['head']}"


def _summary(args: argparse.Namespace, doc: dict) -> str:
    """One line naming what a publishing helper did, so agents can quote the id."""
    cmd = args.command
    if cmd == "inbox-add":
        return f"task {doc['id']}: {doc['title']}"
    if cmd == "message":
        return f"sent {doc['id']} {doc['from']['agent']} -> {doc['to']}"
    if cmd == "promote":
        return f"promoted {doc['agent']} until {doc['lease_expires_at']}"
    if cmd in ("claim", "heartbeat"):
        verb = "claimed" if cmd == "claim" else "heartbeat"
        until = doc.get("expires_at") or doc.get("lease_expires_at")
        return f"{verb} {args.task} as {args.agent} until {until}"
    if cmd == "complete":
        return f"complete {args.task} as {args.agent} (result_ref {doc['result_ref']})"
    if cmd in ("release", "reject"):
        return f"{cmd} {args.task} as {args.agent}"
    return json.dumps(doc, sort_keys=True)


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


def _resolve_harness(hive: Path, agent: str, given: str | None) -> str:
    """The registry is the source of truth for an agent's harness (§5 trust).

    `--harness` is therefore optional: omitted, it is read from the registry entry.
    Supplying a different one is a mistake the library would refuse anyway
    (ClaimDenied), so the CLI names it here where the operator can see both values.
    """
    registered = str(require_agent(hive, agent)["harness"])
    if given is None or not str(given).strip():
        return registered
    given = str(given)
    if given != registered:
        raise ClaimDenied(
            f"--harness {given!r} does not match the registry harness "
            f"{registered!r} for agent {agent!r}; omit --harness to use the registry"
        )
    return given


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
        allow=["inbox/*.json"],
    )


def _message(args: argparse.Namespace, hive: Path, now: datetime) -> dict:
    """Open-ended agent→agent message: registry trust only; no claim gate (§6).

    Writes the sender's outbox file + messages.jsonl and publishes only those paths.
    Body is an untrusted request (`{"text": ...}`), never a command to execute.
    """
    agent = _require(args.from_agent, "--from")
    harness = _resolve_harness(hive, agent, args.harness)
    to = _require(args.to, "--to")
    msg_type = _require(args.type, "--type")
    if msg_type in ("promote", "budget_block"):
        raise ValueError(
            "--type promote/budget_block are written by the promote and claim "
            "helpers, not by message"
        )
    body = {"text": str(args.body)}
    profile = str(args.profile or "default")

    def op() -> dict:
        return write_message(
            hive,
            agent=agent,
            harness=harness,
            type=msg_type,
            to=to,
            body=body,
            now=now,
            profile=profile,
        )

    return _run_op(
        hive,
        local=args.local,
        task_id="__none__",
        message=f"message {agent}->{to} {msg_type}",
        op=op,
        agent=agent,
        now=now,
        allow=[
            f"agents/{agent}/outbox/*.json",
            "store/messages.jsonl",
        ],
    )


def _lookback(args: argparse.Namespace, hive: Path, now: datetime) -> str:
    profile = load_profile(hive, args.profile)

    def op() -> dict:
        path = write_lookback(hive, now, profile)
        return {"path": str(path)}

    if args.local or not _hive_can_publish(hive):
        return str(write_lookback(hive, now, profile))
    doc = _run_op(
        hive,
        local=False,
        task_id="__none__",
        message="lookback",
        op=op,
        now=now,
        allow=[f"{_lookback_dir(profile)}/*.md"],
    )
    return str(doc["path"])


def _lookback_dir(profile: dict) -> str:
    """The profile's lookback `write_dir`, as a hive-relative glob prefix."""
    cfg = profile.get("lookback") if isinstance(profile, dict) else None
    raw = cfg.get("write_dir") if isinstance(cfg, dict) else None
    return str(raw or "lookback/").strip("/") or "lookback"


def _claim(args: argparse.Namespace, hive: Path, now: datetime, profile: dict) -> dict:
    task_id = _require(args.task, "--task")
    agent = _require(args.agent, "--agent")
    harness = _resolve_harness(hive, agent, args.harness)

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
    # --harness is optional; when given it must match the registry (§5 trust).
    _resolve_harness(hive, agent, args.harness)
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
    # --harness is optional; when given it must match the registry (§5 trust).
    _resolve_harness(hive, agent, args.harness)
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
    # --harness is optional; when given it must match the registry (§5 trust).
    _resolve_harness(hive, agent, args.harness)
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
    # --harness is optional; when given it must match the registry (§5 trust).
    _resolve_harness(hive, agent, args.harness)

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
    harness = _resolve_harness(hive, agent, args.harness)
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
        allow=promote_allow(agent, args.by or agent),
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
    allow: list[str] | None = None,
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
            allow=allow,
        )
    except GitopsError as e:
        if captured.get("doc") is not None and "nothing to commit" in str(e).lower():
            return captured["doc"]
        raise


def _init_message(status: str, dest: Path) -> str:
    if status == "bootstrapped":
        return f"bootstrapped hive at {dest} (created origin/swarm)"
    if status == "attached":
        return f"attached hive at {dest} from origin/swarm"
    if status == "copied":
        return f"copied template to {dest}"
    return f"{status} hive at {dest}"


def _hive_for_hint(args: argparse.Namespace) -> Path:
    """Best-effort hive path for an error hint; never raises over the real error."""
    try:
        return resolve_hive(getattr(args, "hive", None))
    except Exception:
        return Path(getattr(args, "hive", None) or "_swarm")


def _dirty_hint(hive: Path) -> str:
    return (
        "The hive work-tree has uncommitted changes, so nothing can be published.\n"
        f"Inspect them with:  git -C {hive} status\n"
        "then commit them or discard them (git -C "
        f"{hive} checkout -- . ; git -C {hive} clean -fd) and retry."
    )


ALLOW_LOCAL_ENV = "RIP_SWARM_ALLOW_LOCAL"


def _gate_local_on_publishable(hive: Path) -> None:
    """Refuse --local on a hive that could publish, unless explicitly opted in.

    --local writes hive files without committing, so on a publishable hive it leaves
    a dirty work-tree that makes every later publish fail with DirtyHive. That is a
    test/debug affordance, not an operator workflow, so it takes a deliberate
    environment opt-in; the warning still fires when it is allowed.
    """
    if os.environ.get(ALLOW_LOCAL_ENV) != "1":
        raise GitopsError(
            f"refusing --local on a publishable hive at {hive}.\n"
            "--local skips git, so the writes stay uncommitted and the dirty "
            "work-tree blocks every later publish.\n"
            "Drop --local to publish normally, or, if you really want the "
            f"uncommitted writes, set {ALLOW_LOCAL_ENV}=1 to allow it."
        )
    print(
        f"warning: --local skips git on a publishable hive at {hive}.\n"
        "It leaves the work-tree dirty, which blocks every later publish.\n"
        f"Inspect with:  git -C {hive} status\n"
        "then commit or discard those changes before publishing again.",
        file=sys.stderr,
    )


def _hive_can_publish(hive: Path) -> bool:
    try:
        assert_hive_repo(hive)
        upstream(hive)
    except (NotHiveRepo, GitopsError):
        return False
    return True


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
