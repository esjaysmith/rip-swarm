# rip_swarm/project.py — the project repository side of join/leave (spec §4)
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rip_swarm.gitops import GitopsError, run_git
from rip_swarm.init_hive import _FALLBACK_EMAIL, _FALLBACK_NAME

FALLBACK_NAME = _FALLBACK_NAME
FALLBACK_EMAIL = _FALLBACK_EMAIL
INTEGRATION = "rip-swarm/integration"


class ProjectError(GitopsError):
    pass


@dataclass(frozen=True)
class Project:
    main: Path
    common_dir: Path
    origin_url: str

    @property
    def worktrees(self) -> Path:
        return self.main / ".worktrees"

    @property
    def hives(self) -> Path:
        return self.common_dir / "rip-swarm"


def _git(where: Path, *args: str, check: bool = True):
    return run_git("-C", str(where), *args, check=check)


def resolve_project(start: Path) -> Project:
    """MAIN is the parent of the absolute common dir: the same answer from a
    subdirectory, a linked worktree, or any process cwd (spec §4.1 step 1)."""
    start = Path(start)
    got = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if got.returncode != 0:
        raise ProjectError(f"{start} is not inside a git work tree; run from inside the project")
    if _git(start, "rev-parse", "--is-bare-repository").stdout.strip() == "true":
        raise ProjectError(f"{start} is a bare repository; run from inside the project")
    common = Path(got.stdout.strip()).resolve()
    main = common.parent
    url = _git(main, "remote", "get-url", "origin", check=False)
    if url.returncode != 0:
        raise ProjectError(f"no 'origin' remote in {main}; add one with git remote add origin <url>")
    blocked = _git(main, "show-ref", "--verify", "--quiet", "refs/heads/rip-swarm", check=False)
    if blocked.returncode == 0:
        raise ProjectError(
            "a local branch named 'rip-swarm' blocks the rip-swarm/* branches; "
            "rename or delete it: git branch -m rip-swarm <new-name>"
        )
    return Project(main=main, common_dir=common, origin_url=url.stdout.strip())


def identity(project: Project) -> tuple[str, str]:
    name = _git(project.main, "config", "user.name", check=False).stdout.strip()
    email = _git(project.main, "config", "user.email", check=False).stdout.strip()
    return name or FALLBACK_NAME, email or FALLBACK_EMAIL


def branch_exists(project: Project, branch: str) -> bool:
    got = _git(project.main, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return got.returncode == 0


def _worktree_paths(project: Project) -> set[Path]:
    listing = _git(project.main, "worktree", "list", "--porcelain").stdout
    return {
        Path(line[len("worktree "):]).resolve()
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }


def ensure_worktree(project: Project, path: Path, branch: str, base: str) -> str:
    path = Path(path)
    if path.exists():
        if path.resolve() in _worktree_paths(project):
            return "reused"
        raise ProjectError(f"{path} exists but is not a worktree of this repo; not touching it")
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(project, branch):
        _git(project.main, "worktree", "add", str(path), branch)
    else:
        _git(project.main, "worktree", "add", "-b", branch, str(path), base)
    return "created"


def ensure_excluded(project: Project) -> None:
    """`.worktrees/` in the repo's own exclude file; no tracked file changes."""
    path = project.common_dir / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.strip() in (".worktrees/", ".worktrees", "/.worktrees/") for line in text.splitlines()):
        return
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + ".worktrees/\n", encoding="utf-8")


def main_is_dirty(project: Project) -> bool:
    return bool(_git(project.main, "status", "--porcelain").stdout.strip())


def worktree_is_clean(path: Path) -> bool:
    return not _git(Path(path), "status", "--porcelain").stdout.strip()


def is_merged(project: Project, branch: str, into: str) -> bool:
    if not (branch_exists(project, branch) and branch_exists(project, into)):
        return False
    got = _git(project.main, "merge-base", "--is-ancestor", branch, into, check=False)
    return got.returncode == 0


def remove_worktree(project: Project, path: Path) -> None:
    _git(project.main, "worktree", "remove", str(path))
