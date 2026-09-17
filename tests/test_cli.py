# tests/test_cli.py
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from rip_swarm.cli import main

ROOT = Path(__file__).resolve().parents[1]


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name) / "_swarm"

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_inbox_claim_status_local(self):
        self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        self.assertEqual(
            main(["inbox-add", "--hive", str(self.hive), "--title", "X", "--created-by", "op", "--local"]),
            0,
        )
        inbox = list((self.hive / "inbox").glob("task_*.json"))
        self.assertEqual(len(inbox), 1)
        task_id = inbox[0].stem
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n", encoding="utf-8"
        )
        self.assertEqual(
            main([
                "claim", "--hive", str(self.hive), "--task", task_id,
                "--agent", "alice", "--harness", "claude-code", "--local",
            ]),
            0,
        )
        self.assertTrue((self.hive / "claims" / f"{task_id}.json").is_file())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["status", "--hive", str(self.hive)]), 0)
        self.assertIn(task_id, buf.getvalue())

    def _seed_alice_bob(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: bob\n  harness: codex\n  role: worker\n"
            "- id: op\n  harness: claude-code\n  role: operator\n",
            encoding="utf-8",
        )

    def _add(self, title="X"):
        before = {p.stem for p in (self.hive / "inbox").glob("task_*.json")}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["inbox-add", "--hive", str(self.hive), "--title", title, "--created-by", "op", "--local"]),
                0,
            )
        after = {p.stem for p in (self.hive / "inbox").glob("task_*.json")}
        return (after - before).pop()

    def test_claim_denied_and_unknown_agent(self):
        self._seed_alice_bob()
        task_id = self._add()
        self.assertEqual(
            main([
                "claim", "--hive", str(self.hive), "--task", task_id,
                "--agent", "alice", "--harness", "claude-code", "--local",
            ]),
            0,
        )
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(
                main([
                    "claim", "--hive", str(self.hive), "--task", task_id,
                    "--agent", "bob", "--harness", "codex", "--local",
                ]),
                2,
            )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                main([
                    "claim", "--hive", str(self.hive), "--task", task_id,
                    "--agent", "nobody", "--harness", "x", "--local",
                ]),
                1,
            )

    def test_heartbeat_complete_release_reject_local(self):
        self._seed_alice_bob()
        t1 = self._add("one")
        self.assertEqual(
            main([
                "claim", "--hive", str(self.hive), "--task", t1,
                "--agent", "alice", "--harness", "claude-code", "--local",
            ]),
            0,
        )
        self.assertEqual(
            main([
                "heartbeat", "--hive", str(self.hive), "--task", t1,
                "--agent", "alice", "--local",
            ]),
            0,
        )
        self.assertEqual(
            main([
                "complete", "--hive", str(self.hive), "--task", t1,
                "--agent", "alice", "--result-ref", "path/out", "--local",
            ]),
            0,
        )
        self.assertFalse((self.hive / "claims" / f"{t1}.json").exists())
        t2 = self._add("two")
        self.assertEqual(
            main([
                "claim", "--hive", str(self.hive), "--task", t2,
                "--agent", "alice", "--harness", "claude-code", "--local",
            ]),
            0,
        )
        self.assertEqual(
            main(["release", "--hive", str(self.hive), "--task", t2, "--agent", "alice", "--local"]),
            0,
        )
        t3 = self._add("three")
        self.assertEqual(
            main([
                "claim", "--hive", str(self.hive), "--task", t3,
                "--agent", "alice", "--harness", "claude-code", "--local",
            ]),
            0,
        )
        self.assertEqual(
            main(["reject", "--hive", str(self.hive), "--task", t3, "--agent", "alice", "--local"]),
            0,
        )

    def test_promote_denied_without_self_promote(self):
        self._seed_alice_bob()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                main([
                    "promote", "--hive", str(self.hive), "--agent", "alice",
                    "--harness", "claude-code", "--reason", "x", "--local",
                ]),
                2,
            )

    def test_promote_and_lookback_local(self):
        self._seed_alice_bob()
        text = (self.hive / "profiles" / "default.yaml").read_text(encoding="utf-8")
        (self.hive / "profiles" / "default.yaml").write_text(
            text.replace("allow_self_promote: false", "allow_self_promote: true"),
            encoding="utf-8",
        )
        self.assertEqual(
            main([
                "promote", "--hive", str(self.hive), "--agent", "alice",
                "--harness", "claude-code", "--reason", "operator designated", "--local",
            ]),
            0,
        )
        self.assertTrue((self.hive / "claims" / "orchestrator.json").is_file())
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").is_file())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["lookback", "--hive", str(self.hive)]), 0)
        self.assertTrue(list((self.hive / "lookback").glob("*.md")))

    def test_worktree_without_upstream_errors_unless_local(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        subprocess.check_call(
            ["git", "init", "-q", "-b", "swarm", str(self.hive)],
            stdout=subprocess.DEVNULL,
        )
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "inbox-add", "--hive", str(self.hive),
                "--title", "X", "--created-by", "op",
            ])
        self.assertEqual(rc, 1)
        msg = err.getvalue()
        self.assertIn("upstream", msg.lower())
        self.assertIn("--local", msg)
        self.assertEqual(
            main([
                "inbox-add", "--hive", str(self.hive),
                "--title", "X", "--created-by", "op", "--local",
            ]),
            0,
        )

    def test_publish_when_hive_has_upstream(self):
        origin = Path(self.tmp.name) / "origin.git"
        subprocess.check_call(
            ["git", "init", "--bare", "-q", "-b", "swarm", str(origin)],
            stdout=subprocess.DEVNULL,
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        subprocess.check_call(
            ["git", "init", "-q", "-b", "swarm", str(self.hive)],
            stdout=subprocess.DEVNULL,
        )
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(self.hive), "config", k, v])
        subprocess.check_call(["git", "-C", str(self.hive), "add", "-A"], stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "-C", str(self.hive), "commit", "-qm", "seed"])
        subprocess.check_call(["git", "-C", str(self.hive), "remote", "add", "origin", str(origin)])
        subprocess.check_call(
            ["git", "-C", str(self.hive), "push", "-q", "-u", "origin", "swarm"],
            stdout=subprocess.DEVNULL,
        )
        self.assertEqual(
            main(["inbox-add", "--hive", str(self.hive), "--title", "Pub", "--created-by", "op"]),
            0,
        )
        shown = subprocess.check_output(
            ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "swarm"],
            text=True,
        )
        self.assertIn("inbox/", shown)

    def test_skill_md_name_and_scripts_shim(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: rip-swarm", text)
        self.assertNotIn("PYTHONPATH=.", text)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        help_run = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "status.py"), "-h"],
            cwd=str(other),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("--hive", help_run.stdout)
        claim_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "claim.py"), "--help"],
            cwd=str(other),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(claim_help.returncode, 0)
        self.assertIn("--task", claim_help.stdout)
        hb_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "claim.py"), "heartbeat", "-h"],
            cwd=str(other),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(hb_help.returncode, 0)
        complete_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "claim.py"), "complete", "-h"],
            cwd=str(other),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(complete_help.returncode, 0)
        self.assertIn("result-ref", complete_help.stdout)


if __name__ == "__main__":
    unittest.main()
