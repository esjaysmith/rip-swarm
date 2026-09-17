# tests/test_init_hive.py
import subprocess
import tempfile
import unittest
from pathlib import Path
from rip_swarm.gitops import GitopsError
from rip_swarm.init_hive import init_hive

class TestInitHive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "_swarm"

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_protocol_and_profile(self):
        self.assertEqual(init_hive(self.dest, git_init=False), "copied")
        self.assertTrue((self.dest / "PROTOCOL.md").is_file())
        self.assertTrue((self.dest / "profiles" / "default.yaml").is_file())
        self.assertTrue((self.dest / "store" / "claims.jsonl").is_file())
        self.assertIn("merge=union", (self.dest / ".gitattributes").read_text(encoding="utf-8"))
        self.assertFalse((self.dest / ".git").exists())

    def _project(self, name):
        repo = Path(self.tmp.name) / name
        subprocess.check_call(["git", "init", "-b", "main", str(repo)], stdout=subprocess.DEVNULL)
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(repo), "config", k, v])
        (repo / "app.py").write_text("x\n", encoding="utf-8")
        subprocess.check_call(["git", "-C", str(repo), "add", "-A"], stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "-C", str(repo), "commit", "-qm", "seed"], stdout=subprocess.DEVNULL)
        return repo

    def _origin_and_project(self, name):
        origin = Path(self.tmp.name) / f"{name}-origin.git"
        subprocess.check_call(["git", "init", "--bare", "-b", "main", str(origin)], stdout=subprocess.DEVNULL)
        repo = self._project(name)
        subprocess.check_call(["git", "-C", str(repo), "remote", "add", "origin", str(origin)])
        subprocess.check_call(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"])
        return origin, repo

    def test_init_bootstraps_swarm_branch_and_clones_it(self):
        origin, repo = self._origin_and_project("proj")
        dest = repo / "_swarm"
        (repo / "app.py").write_text("dirty while init runs\n", encoding="utf-8")
        self.assertEqual(init_hive(dest), "bootstrapped")
        self.assertTrue((dest / ".git").is_dir())  # nested clone, not a worktree pointer
        top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=dest, text=True).strip()
        self.assertEqual(Path(top).resolve(), dest.resolve())
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=dest, text=True).strip(), "origin/swarm")
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=dest, text=True), "")
        self.assertTrue((dest / "PROTOCOL.md").is_file())
        self.assertIn("swarm", subprocess.check_output(["git", "ls-remote", "--heads", str(origin)], text=True))
        self.assertIn("_swarm/", (repo / ".gitignore").read_text(encoding="utf-8"))
        # code checkout untouched: still on main, dirty edit kept, hive not staged
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True).strip(), "main")
        self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "dirty while init runs\n")
        self.assertNotIn("_swarm", subprocess.check_output(["git", "status", "--porcelain", "--", "_swarm"], cwd=repo, text=True))
        self.assertNotIn("app.py", subprocess.check_output(["git", "ls-tree", "--name-only", "origin/swarm"], cwd=dest, text=True))

    def test_init_attaches_existing_remote_branch(self):
        origin, a = self._origin_and_project("a")
        self.assertEqual(init_hive(a / "_swarm"), "bootstrapped")
        b = Path(self.tmp.name) / "b"
        subprocess.check_call(["git", "clone", "-q", str(origin), str(b)])
        self.assertEqual(init_hive(b / "_swarm"), "attached")
        self.assertTrue((b / "_swarm" / "PROTOCOL.md").is_file())
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=b / "_swarm", text=True).strip(), "origin/swarm")
        with self.assertRaises(FileExistsError):
            init_hive(b / "_swarm")

    def test_refuse_clobber(self):
        init_hive(self.dest, git_init=False)
        with self.assertRaises(FileExistsError):
            init_hive(self.dest, force=False, git_init=False)
        (self.dest / "PROTOCOL.md").write_text("OLD\n", encoding="utf-8")
        init_hive(self.dest, force=True, git_init=False)
        self.assertNotEqual((self.dest / "PROTOCOL.md").read_text(encoding="utf-8"), "OLD\n")
        self.assertIn("rip-swarm PROTOCOL", (self.dest / "PROTOCOL.md").read_text(encoding="utf-8"))

    # --- item 5a: --no-git clobber safety ---

    def test_copy_refuses_existing_dest_without_force(self):
        self.dest.mkdir(parents=True)
        (self.dest / "important.txt").write_text("mine\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            init_hive(self.dest, git_init=False)
        self.assertTrue((self.dest / "important.txt").is_file())

    def test_copy_force_refuses_non_hive_dest(self):
        self.dest.mkdir(parents=True)
        (self.dest / "important.txt").write_text("mine\n", encoding="utf-8")
        with self.assertRaises(GitopsError) as ctx:
            init_hive(self.dest, force=True, git_init=False)
        self.assertIn("not a hive", str(ctx.exception))
        self.assertTrue((self.dest / "important.txt").is_file())

    # --- item 5b: --force must not discard a dirty / unpushed hive checkout ---

    def test_force_refuses_dirty_hive_checkout(self):
        origin, repo = self._origin_and_project("dirty")
        dest = repo / "_swarm"
        init_hive(dest)
        (dest / "PROTOCOL.md").write_text("local edit\n", encoding="utf-8")
        with self.assertRaises(GitopsError) as ctx:
            init_hive(dest, force=True)
        self.assertIn("push or discard", str(ctx.exception))
        self.assertEqual((dest / "PROTOCOL.md").read_text(encoding="utf-8"), "local edit\n")

    def test_force_refuses_unpushed_hive_commits(self):
        origin, repo = self._origin_and_project("unpushed")
        dest = repo / "_swarm"
        init_hive(dest)
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(dest), "config", k, v])
        (dest / "local.txt").write_text("unpushed\n", encoding="utf-8")
        subprocess.check_call(["git", "-C", str(dest), "add", "-A"], stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "-C", str(dest), "commit", "-qm", "local"], stdout=subprocess.DEVNULL)
        head = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaises(GitopsError) as ctx:
            init_hive(dest, force=True)
        self.assertIn("push or discard", str(ctx.exception))
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip(),
            head,
        )
        self.assertTrue((dest / "local.txt").is_file())

    def test_force_reclones_a_clean_synced_hive(self):
        origin, repo = self._origin_and_project("clean")
        dest = repo / "_swarm"
        init_hive(dest)
        (dest / "untracked-scratch.txt").unlink(missing_ok=True)
        self.assertEqual(init_hive(dest, force=True), "attached")
        self.assertTrue((dest / "PROTOCOL.md").is_file())

    # --- item 6: init error paths ---

    def test_no_origin_remote_reports_error(self):
        repo = self._project("noremote")
        with self.assertRaises(GitopsError):
            init_hive(repo / "_swarm")
        self.assertFalse((repo / "_swarm").exists())
        self.assertFalse((repo / ".gitignore").exists())

    def test_outside_a_git_repo_reports_error(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        with self.assertRaises(GitopsError):
            init_hive(plain / "_swarm")
        self.assertFalse((plain / "_swarm").exists())

    def test_failed_push_leaves_nothing_behind_and_no_gitignore(self):
        origin, repo = self._origin_and_project("nopush")
        # The remote is reachable but refuses the push (no write rights): bootstrap must
        # report the manual steps, leave no _swarm/ behind, and not touch .gitignore.
        hook = origin / "hooks" / "pre-receive"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho denied >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaises(GitopsError) as ctx:
            init_hive(repo / "_swarm")
        self.assertIn("Manual steps", str(ctx.exception))
        self.assertFalse((repo / "_swarm").exists())
        self.assertFalse((repo / ".gitignore").exists())

    def test_unreachable_remote_leaves_nothing_behind(self):
        origin, repo = self._origin_and_project("dead")
        dead = Path(self.tmp.name) / "dead.git"
        subprocess.check_call(["git", "-C", str(repo), "remote", "set-url", "origin", str(dead)])
        with self.assertRaises(GitopsError):
            init_hive(repo / "_swarm")
        self.assertFalse((repo / "_swarm").exists())
        self.assertFalse((repo / ".gitignore").exists())


if __name__ == "__main__":
    unittest.main()
