# tests/test_gitops.py
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from rip_swarm.claim import ClaimDenied
from rip_swarm.gitops import DirtyHive, NotHiveRepo, claim_and_publish, publish
from rip_swarm.inbox import create_task
from rip_swarm.io import read_json
from rip_swarm.outbox import write_message

T0 = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)

def _git(cwd, *args):
    subprocess.check_call(["git", *args], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _config(repo):
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")

REGISTRY = "- id: alice\n  harness: claude-code\n  role: worker\n- id: bob\n  harness: codex\n  role: worker\n"

class TestGitops(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        subprocess.check_call(["git", "init", "--bare", "-b", "main", str(self.origin)], stdout=subprocess.DEVNULL)
        # Project clone A: seed `main` with a code file; bootstrap the orphan `swarm` branch on the
        # origin from a temp dir (as init does); clone it single-branch into a/_swarm.
        self.a = self.root / "a"
        subprocess.check_call(["git", "clone", str(self.origin), str(self.a)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _config(self.a)
        _git(self.a, "checkout", "-b", "main")
        (self.a / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.a / ".gitignore").write_text("_swarm/\n", encoding="utf-8")
        _git(self.a, "add", "-A")
        _git(self.a, "commit", "-m", "seed project")
        _git(self.a, "push", "-u", "origin", "main")
        seed = self.root / "seed"
        subprocess.check_call(["git", "init", "-q", "-b", "swarm", str(seed)])
        _config(seed)
        (seed / ".gitattributes").write_text("store/*.jsonl merge=union\n", encoding="utf-8")
        (seed / "agents").mkdir()
        (seed / "agents" / "registry.yaml").write_text(REGISTRY, encoding="utf-8")
        self.task = create_task(seed, title="T", created_by="op", now=T0)
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "seed hive")
        _git(seed, "push", str(self.origin), "swarm")
        def attach(project):
            subprocess.check_call(["git", "clone", "-q", "--single-branch", "-b", "swarm", str(self.origin), str(project / "_swarm")])
            _config(project / "_swarm")
            return project / "_swarm"
        self.ha = attach(self.a)
        # Project clone B attaches the existing hive branch the same way.
        self.b = self.root / "b"
        subprocess.check_call(["git", "clone", str(self.origin), str(self.b)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _config(self.b)
        self.hb = attach(self.b)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nested_clone_is_hive_root_and_code_branch_ignores_it(self):
        top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=self.ha, text=True).strip()
        self.assertEqual(Path(top).resolve(), self.ha.resolve())
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.ha, text=True).strip(), "swarm")
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=self.ha, text=True).strip(), "origin/swarm")
        self.assertTrue((self.ha / ".git").is_dir())  # nested clone, not a worktree pointer file
        self.assertNotIn("_swarm", subprocess.check_output(["git", "status", "--porcelain"], cwd=self.a, text=True))
        # single-branch: the hive clone does not carry the code branch
        self.assertNotIn("main", subprocess.check_output(["git", "branch", "-r"], cwd=self.ha, text=True))

    def test_first_push_wins_second_must_not_work(self):
        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )
        with self.assertRaises(ClaimDenied):
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=T0, lease_seconds=900,
            )
        # B's hive is left on the remote tip, holding alice's claim, with a clean hive tree.
        tip = read_json(self.hb / "claims" / f"{self.task['id']}.json")
        self.assertEqual(tip["agent"], "alice")
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True), "")

    def test_lost_race_after_local_excl_success(self):
        # B creates its claim locally before A's push lands; B's push is rejected; B must lose.
        from rip_swarm.claim import try_claim
        try_claim(self.hb, self.task["id"], "bob", "codex", T0, 900)
        _git(self.hb, "add", "-A")
        _git(self.hb, "commit", "-m", "bob local claim")
        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )
        # B now has an unpushed local commit. publish sees HEAD ahead of @{u}, fetches, finds alice's
        # claim on the remote tip, resets B's hive to the remote tip and reports the lost race.
        with self.assertRaises(ClaimDenied):
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=T0, lease_seconds=900,
            )
        self.assertEqual(read_json(self.hb / "claims" / f"{self.task['id']}.json")["agent"], "alice")

    def test_dirty_code_tree_neither_blocks_nor_is_touched(self):
        # The agent is mid-edit on the code branch; hive publish must still work, and a lost race
        # on the hive must not reset the code checkout.
        (self.b / "app.py").write_text("print('work in progress')\n", encoding="utf-8")
        (self.b / "untracked.txt").write_text("keep me\n", encoding="utf-8")
        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )
        with self.assertRaises(ClaimDenied):
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=T0, lease_seconds=900,
            )
        self.assertEqual((self.b / "app.py").read_text(encoding="utf-8"), "print('work in progress')\n")
        self.assertTrue((self.b / "untracked.txt").exists())
        # and a heartbeat from A publishes fine while A's code tree is dirty too
        (self.a / "app.py").write_text("dirty\n", encoding="utf-8")
        from rip_swarm.claim import heartbeat
        publish(self.ha, task_id=self.task["id"], message="heartbeat",
                op=lambda: heartbeat(self.ha, self.task["id"], "alice", T0, 900))

    def test_concurrent_audit_appends_union_merge(self):
        # Both hives append to messages.jsonl; second push must rebase cleanly via merge=union.
        def op_a():
            return write_message(self.ha, agent="alice", harness="claude-code", type="ops", to="*", body={"text": "a"}, now=T0)
        def op_b():
            return write_message(self.hb, agent="bob", harness="codex", type="ops", to="*", body={"text": "b"}, now=T0)
        publish(self.ha, task_id="__none__", op=op_a, message="msg a")
        publish(self.hb, task_id="__none__", op=op_b, message="msg b")
        _git(self.ha, "pull", "--rebase")
        lines = (self.ha / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual({json.loads(l)["from"]["agent"] for l in lines}, {"alice", "bob"})

    def test_refuses_non_root_and_dirty(self):
        # A plain subdirectory of the project (not its own checkout) is not a hive root.
        plain = self.a / "not_a_hive"
        plain.mkdir()
        with self.assertRaises(NotHiveRepo):
            claim_and_publish(plain, task_id="x", agent="alice", harness="h", now=T0, lease_seconds=1)
        (self.ha / "scratch.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(DirtyHive):
            claim_and_publish(self.ha, task_id=self.task["id"], agent="alice", harness="h", now=T0, lease_seconds=1)

if __name__ == "__main__":
    unittest.main()
