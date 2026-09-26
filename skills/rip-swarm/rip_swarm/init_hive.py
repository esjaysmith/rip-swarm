from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from rip_swarm.gitops import GitopsError, _git_env, init_repo

_DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "_swarm"
_GITIGNORE_ENTRIES = frozenset({"_swarm/", "_swarm"})
_FALLBACK_EMAIL = "rip-swarm@localhost"
_FALLBACK_NAME = "rip-swarm"


def init_hive(
    dest: Path,
    *,
    force: bool = False,
    template_root: Path | None = None,
    git_init: bool = True,
    branch: str = "swarm",
    remote: str = "origin",
) -> str:
    dest = Path(dest)
    template = Path(template_root) if template_root is not None else _DEFAULT_TEMPLATE
    if git_init:
        return _init_git(dest, force=force, template=template, branch=branch, remote=remote)
    return _init_copy(dest, force=force, template=template)


def _is_hive_marked(dest: Path) -> bool:
    return (dest / "PROTOCOL.md").exists() or (dest / "profiles").exists()


def _init_copy(dest: Path, *, force: bool, template: Path) -> str:
    """Copy the template into `dest` (--no-git).

    Refuse whenever `dest` already exists and `force` is not set -- the marker files
    are not a licence to clobber an unrelated directory. Under `force`, only remove a
    directory that carries the hive marker (PROTOCOL.md or profiles/); anything else
    is someone else's data and is refused with a clear message.
    """
    if dest.exists():
        if not force:
            raise FileExistsError(dest)
        if not _is_hive_marked(dest):
            raise GitopsError(
                f"{dest} exists but is not a hive (no PROTOCOL.md or profiles/); "
                "refusing to remove it -- delete it yourself or pick another path"
            )
        shutil.rmtree(dest)
    shutil.copytree(template, dest)
    return "copied"


def _assert_force_removable(dest: Path) -> None:
    """Refuse --force on a hive checkout holding work the remote does not have."""
    top = _git("-C", str(dest), "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != dest.resolve():
        return
    if _git("-C", str(dest), "status", "--porcelain", check=False).stdout.strip():
        raise GitopsError(
            f"{dest} is a hive checkout with a dirty work tree; "
            "push or discard those changes before re-running init --force"
        )
    ahead = _git("-C", str(dest), "rev-list", "@{u}..HEAD", check=False)
    if ahead.returncode == 0 and ahead.stdout.strip():
        raise GitopsError(
            f"{dest} is a hive checkout with unpushed commits; "
            "push or discard them before re-running init --force"
        )


def _init_git(
    dest: Path,
    *,
    force: bool,
    template: Path,
    branch: str,
    remote: str,
) -> str:
    if dest.exists():
        if not force:
            raise FileExistsError(dest)
        _assert_force_removable(dest)
        shutil.rmtree(dest)
    project = _project_root(dest)
    url = _remote_url(project, remote)
    if _branch_on_remote(url, branch):
        _attach(url, branch, dest)
        _ensure_gitignore(project)
        return "attached"
    _bootstrap(url, branch, template)
    _attach(url, branch, dest)
    _ensure_gitignore(project)
    return "bootstrapped"


def _project_root(dest: Path) -> Path:
    parent = dest.parent
    result = _git("-C", str(parent), "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip())


def _remote_url(project: Path, remote: str) -> str:
    return _git("-C", str(project), "remote", "get-url", remote).stdout.strip()


def _branch_on_remote(url: str, branch: str) -> bool:
    return bool(_git("ls-remote", "--heads", url, branch).stdout.strip())


def _attach(url: str, branch: str, dest: Path) -> None:
    try:
        _git("clone", "--single-branch", "-b", branch, url, str(dest))
    except GitopsError:
        if dest.exists():
            shutil.rmtree(dest)
        raise


def _bootstrap(
    url: str,
    branch: str,
    template: Path,
    *,
    name: str = _FALLBACK_NAME,
    email: str = _FALLBACK_EMAIL,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp) / "hive"
        init_repo(seed, branch)
        shutil.copytree(template, seed, dirs_exist_ok=True)
        _git("-C", str(seed), "add", "-A")
        committed = _git(
            "-C",
            str(seed),
            "-c",
            f"user.email={email}",
            "-c",
            f"user.name={name}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "init hive",
            check=False,
        )
        if committed.returncode != 0:
            detail = (committed.stderr or committed.stdout).strip() or "git commit failed"
            raise GitopsError(detail)
        pushed = _git("-C", str(seed), "push", url, branch, check=False)
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout).strip() or "git push failed"
            raise GitopsError(_push_manual_steps(url, branch, dest_hint="_swarm", detail=detail))


def _push_manual_steps(url: str, branch: str, dest_hint: str, detail: str) -> str:
    return (
        f"failed to push hive branch {branch!r} to {url}; nothing left behind.\n"
        "Manual steps:\n"
        f'  tmp=$(mktemp -d) && git init -q -b {branch} "$tmp"\n'
        '  cp -r <skill>/templates/_swarm/. "$tmp"\n'
        '  git -C "$tmp" add -A && git -C "$tmp" commit -qm "init hive"\n'
        f'  git -C "$tmp" push {url} {branch} && rm -rf "$tmp"\n'
        "  echo '_swarm/' >> .gitignore\n"
        f"  git clone --single-branch -b {branch} {url} {dest_hint}\n"
        f"git said: {detail}"
    )


def _ensure_gitignore(project: Path) -> None:
    path = project / ".gitignore"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip() in _GITIGNORE_ENTRIES:
                return
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + "_swarm/\n", encoding="utf-8")
        return
    path.write_text("_swarm/\n", encoding="utf-8")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed"
        raise GitopsError(detail)
    return result


def bootstrap_or_attach(
    url: str,
    *,
    name: str = _FALLBACK_NAME,
    email: str = _FALLBACK_EMAIL,
    branch: str = "swarm",
    template: Path | None = None,
) -> str:
    """Create the hive branch on the remote if it does not exist (spec §4.1 step 3).

    Never clones and never edits the project's .gitignore. A push that loses to
    another session's bootstrap is an attach, not an error.
    """
    template = Path(template) if template is not None else _DEFAULT_TEMPLATE
    if _branch_on_remote(url, branch):
        return "exists"
    try:
        _bootstrap(url, branch, template, name=name, email=email)
    except GitopsError:
        if _branch_on_remote(url, branch):
            return "exists"
        raise
    return "bootstrapped"


def clone_hive(url: str, dest: Path, *, name: str, email: str, branch: str = "swarm") -> None:
    """Single-branch clone of the hive with the agent's commit identity.

    Hive commits are never signed: a global commit.gpgsign=true would otherwise
    fail every publish with no terminal to ask for a passphrase. The hive
    creates no tags, so tag.gpgsign is left alone."""
    _attach(url, branch, Path(dest))
    for key, value in (
        ("user.name", name),
        ("user.email", email),
        ("commit.gpgsign", "false"),
    ):
        _git("-C", str(dest), "config", key, value)
