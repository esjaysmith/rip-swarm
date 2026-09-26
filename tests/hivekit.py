# tests/hivekit.py — shared fixtures for git-backed hive tests (not a test module)
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

T0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)

REGISTRY = (
    "- id: op\n  harness: human\n  role: operator\n"
    "- id: alice\n  harness: claude-code\n  role: worker\n"
    "- id: bob\n  harness: grok\n  role: worker\n"
)


def git(cwd, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr or result.stdout}")
    return result.stdout.strip()


def config(repo) -> None:
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        git(repo, "config", key, value)


def local_hive(root: Path, registry: str = REGISTRY) -> Path:
    """A --no-git hive copied from the template, with `registry` written."""
    from rip_swarm.init_hive import init_hive

    hive = Path(root) / "_swarm"
    init_hive(hive, git_init=False)
    (hive / "agents" / "registry.yaml").write_text(registry, encoding="utf-8")
    return hive


def make_project(root: Path, name: str = "proj") -> tuple[Path, Path]:
    """A bare origin (default branch main) and a project repo with one pushed commit."""
    root = Path(root)
    origin = root / f"{name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = root / name
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    config(repo)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "-u", "origin", "main")
    return origin, repo


def seed_remote_hive(root: Path, origin: Path, registry: str = REGISTRY) -> None:
    """Push a template hive with `registry` to origin/swarm."""
    from rip_swarm.init_hive import _DEFAULT_TEMPLATE

    seed = Path(root) / "seed-hive"
    subprocess.run(["git", "init", "-q", "-b", "swarm", str(seed)], check=True)
    shutil.copytree(_DEFAULT_TEMPLATE, seed, dirs_exist_ok=True)
    (seed / "agents" / "registry.yaml").write_text(registry, encoding="utf-8")
    config(seed)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed hive")
    git(seed, "push", "-q", str(origin), "swarm")


def clone_hive(root: Path, origin: Path, name: str) -> Path:
    dest = Path(root) / name
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "-b", "swarm", str(origin), str(dest)],
        check=True,
    )
    config(dest)
    return dest


def remote_files(origin: Path) -> list[str]:
    result = subprocess.run(
        ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "swarm"],
        capture_output=True, text=True,
    )
    return result.stdout.split() if result.returncode == 0 else []


def remote_show(origin: Path, path: str) -> str | None:
    result = subprocess.run(
        ["git", "--git-dir", str(origin), "show", f"swarm:{path}"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None

