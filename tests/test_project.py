# tests/test_project.py — project repo helpers for join (spec §4.1)
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rip_swarm.init_hive as ih
from hivekit import git, make_project
from rip_swarm.init_hive import bootstrap_or_attach, clone_hive
from rip_swarm.project import (
    FALLBACK_EMAIL,
    FALLBACK_NAME,
    ProjectError,
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


class TestProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin, self.repo = make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_from_a_subdir_and_a_linked_worktree_with_cwd_elsewhere(self):
        sub = self.repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        linked = self.root / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "other", str(linked))
        elsewhere = self.root / "elsewhere"
        subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
        before = os.getcwd()
        os.chdir(elsewhere)
        try:
            for start in (self.repo, sub, linked):
                project = resolve_project(start)
                self.assertEqual(project.main, self.repo.resolve())
                self.assertEqual(project.common_dir, (self.repo / ".git").resolve())
                self.assertEqual(project.origin_url, str(self.origin))
        finally:
            os.chdir(before)

    def test_refusals(self):
        with self.assertRaises(ProjectError) as ctx:
            resolve_project(self.root)
        self.assertIn("run from inside the project", str(ctx.exception))
        git(self.repo, "remote", "remove", "origin")
        with self.assertRaises(ProjectError) as ctx:
            resolve_project(self.repo)
        self.assertIn("origin", str(ctx.exception))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "branch", "rip-swarm")
        with self.assertRaises(ProjectError) as ctx:
            resolve_project(self.repo)
        self.assertIn("git branch -m rip-swarm", str(ctx.exception))

    def test_identity_falls_back_when_unset(self):
        self.assertEqual(identity(resolve_project(self.repo)), ("Test", "test@example.com"))
        git(self.repo, "config", "--unset", "user.name")
        git(self.repo, "config", "--unset", "user.email")
        empty = self.root / "empty-gitconfig"
        empty.write_text("", encoding="utf-8")
        env = {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(identity(resolve_project(self.repo)), (FALLBACK_NAME, FALLBACK_EMAIL))

    def test_worktrees_and_exclude(self):
        project = resolve_project(self.repo)
        path = project.worktrees / "claude-1"
        self.assertEqual(ensure_worktree(project, path, "rip-swarm/claude-1", "HEAD"), "created")
        self.assertTrue(branch_exists(project, "rip-swarm/claude-1"))
        self.assertEqual(ensure_worktree(project, path, "rip-swarm/claude-1", "HEAD"), "reused")
        foreign = project.worktrees / "grok-1"
        foreign.mkdir()
        with self.assertRaises(ProjectError):
            ensure_worktree(project, foreign, "rip-swarm/grok-1", "HEAD")
        self.assertTrue(foreign.is_dir())
        ensure_excluded(project)
        ensure_excluded(project)
        exclude = (project.common_dir / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count(".worktrees/"), 1)
        foreign.rmdir()
        self.assertFalse(main_is_dirty(project))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(worktree_is_clean(path))
        (path / "new.txt").write_text("x\n", encoding="utf-8")
        self.assertFalse(worktree_is_clean(path))
        (path / "new.txt").unlink()
        ensure_worktree(project, project.worktrees / "integration", "rip-swarm/integration", "HEAD")
        self.assertTrue(is_merged(project, "rip-swarm/claude-1", "rip-swarm/integration"))
        self.assertFalse(is_merged(project, "rip-swarm/claude-1", "rip-swarm/nope"))
        remove_worktree(project, path)
        self.assertFalse(path.exists())

    def test_bootstrap_or_attach(self):
        self.assertEqual(bootstrap_or_attach(str(self.origin), name="N", email="n@x"), "bootstrapped")
        self.assertEqual(bootstrap_or_attach(str(self.origin), name="N", email="n@x"), "exists")
        author = subprocess.run(
            ["git", "--git-dir", str(self.origin), "log", "-1", "--format=%ae", "swarm"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(author, "n@x")

    def test_bootstrap_race_becomes_attach(self):
        bootstrap_or_attach(str(self.origin), name="N", email="n@x")
        # Our check saw no branch; another session pushed first; our push is rejected.
        with mock.patch.object(ih, "_branch_on_remote", side_effect=[False, True]):
            self.assertEqual(
                bootstrap_or_attach(str(self.origin), name="Other", email="o@x"), "exists"
            )

    def test_clone_hive_sets_identity(self):
        bootstrap_or_attach(str(self.origin), name="N", email="n@x")
        dest = self.root / "clone"
        clone_hive(str(self.origin), dest, name="Who", email="who@x")
        self.assertEqual(git(dest, "config", "user.email"), "who@x")
        self.assertEqual(git(dest, "rev-parse", "--abbrev-ref", "@{u}"), "origin/swarm")


if __name__ == "__main__":
    unittest.main()
