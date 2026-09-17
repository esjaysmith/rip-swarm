# tests/test_init_hive.py
import subprocess
import tempfile
import unittest
from pathlib import Path
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

if __name__ == "__main__":
    unittest.main()
