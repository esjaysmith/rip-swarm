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
        with redirect_stdout(io.StringIO()):
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

    def test_claim_orchestrator_denied(self):
        self._seed_alice_bob()
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "claim", "--hive", str(self.hive), "--task", "orchestrator",
                "--agent", "alice", "--harness", "claude-code", "--local",
            ])
        self.assertIn(rc, (1, 2))
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())
        self.assertFalse((self.hive / "orchestrator" / "CURRENT.json").exists())

    def test_complete_reject_orchestrator_denied(self):
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
        with redirect_stderr(io.StringIO()):
            self.assertIn(
                main([
                    "complete", "--hive", str(self.hive), "--task", "orchestrator",
                    "--agent", "alice", "--result-ref", "x", "--local",
                ]),
                (1, 2),
            )
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertTrue((self.hive / "claims" / "orchestrator.json").exists())
        with redirect_stderr(io.StringIO()):
            self.assertIn(
                main([
                    "reject", "--hive", str(self.hive), "--task", "orchestrator",
                    "--agent", "alice", "--local",
                ]),
                (1, 2),
            )
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").exists())
        self.assertTrue((self.hive / "claims" / "orchestrator.json").exists())

    def _hive_with_upstream(self) -> None:
        origin = Path(self.tmp.name) / "origin.git"
        subprocess.check_call(
            ["git", "init", "--bare", "-q", "-b", "swarm", str(origin)],
            stdout=subprocess.DEVNULL,
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: bob\n  harness: codex\n  role: worker\n"
            "- id: op\n  harness: claude-code\n  role: operator\n",
            encoding="utf-8",
        )
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

    def test_lookback_then_claim_with_upstream(self):
        self._hive_with_upstream()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["lookback", "--hive", str(self.hive)]), 0)
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.hive), "status", "--porcelain"], text=True
            ),
            "",
        )
        self.assertTrue(list((self.hive / "lookback").glob("*.md")))
        self.assertEqual(
            main(["inbox-add", "--hive", str(self.hive), "--title", "Pub", "--created-by", "op"]),
            0,
        )
        inbox = list((self.hive / "inbox").glob("task_*.json"))
        self.assertEqual(len(inbox), 1)
        task_id = inbox[0].stem
        self.assertEqual(
            main([
                "claim", "--hive", str(self.hive), "--task", task_id,
                "--agent", "alice", "--harness", "claude-code",
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

    def test_init_says_what_it_did(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        out = buf.getvalue()
        self.assertIn(str(self.hive), out)
        self.assertNotEqual(out.strip(), "copied")

    def test_init_rejects_runtime_flags(self):
        for flag in ("--local", "--agent", "--harness", "--profile"):
            extra = [flag] if flag == "--local" else [flag, "x"]
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                main(["init", "--hive", str(self.hive), "--no-git", *extra])
            self.assertEqual(cm.exception.code, 2)

    def test_status_with_unknown_profile_falls_back(self):
        self._seed_alice_bob()
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = main(["status", "--hive", str(self.hive), "--profile", "nope"])
        self.assertEqual(rc, 0)

    def test_promote_by_operator_in_profile(self):
        self._seed_alice_bob()
        path = self.hive / "profiles" / "default.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("operators: []", "operators: [op]"), encoding="utf-8")
        self.assertEqual(
            main([
                "promote", "--hive", str(self.hive), "--agent", "alice",
                "--harness", "claude-code", "--by", "op",
                "--reason", "operator designated", "--local",
            ]),
            0,
        )
        self.assertTrue((self.hive / "claims" / "orchestrator.json").is_file())
        self.assertTrue((self.hive / "orchestrator" / "CURRENT.json").is_file())

    def test_release_orchestrator_via_cli(self):
        self._seed_alice_bob()
        path = self.hive / "profiles" / "default.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("operators: []", "operators: [op]"),
            encoding="utf-8",
        )
        self.assertEqual(
            main([
                "promote", "--hive", str(self.hive), "--agent", "alice",
                "--harness", "claude-code", "--by", "op", "--local",
            ]),
            0,
        )
        self.assertEqual(
            main([
                "release", "--hive", str(self.hive), "--task", "orchestrator",
                "--agent", "alice", "--local",
            ]),
            0,
        )
        self.assertFalse((self.hive / "claims" / "orchestrator.json").exists())
        self.assertFalse((self.hive / "orchestrator" / "CURRENT.json").exists())

    def test_local_on_publishable_hive_warns(self):
        self._hive_with_upstream()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main([
                "inbox-add", "--hive", str(self.hive), "--title", "L",
                "--created-by", "op", "--local",
            ])
        self.assertEqual(rc, 0)
        msg = err.getvalue()
        self.assertIn("--local", msg)
        self.assertIn(f"git -C {self.hive} status", msg)

    def test_dirty_hive_error_names_recovery(self):
        self._hive_with_upstream()
        (self.hive / "stray.txt").write_text("dirt\n", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "inbox-add", "--hive", str(self.hive), "--title", "D", "--created-by", "op",
            ])
        self.assertEqual(rc, 1)
        msg = err.getvalue()
        self.assertIn("dirty", msg.lower())
        self.assertIn(f"git -C {self.hive} status", msg)

    # --- git-backed CLI flows ---

    def _project_clone(self, name: str, origin: Path) -> Path:
        work = Path(self.tmp.name) / name
        subprocess.check_call(
            ["git", "clone", "-q", str(origin), str(work)], stdout=subprocess.DEVNULL
        )
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(work), "config", k, v])
        return work

    def _bare_project_origin(self) -> Path:
        origin = Path(self.tmp.name) / "project.git"
        subprocess.check_call(
            ["git", "init", "--bare", "-q", "-b", "main", str(origin)],
            stdout=subprocess.DEVNULL,
        )
        seed = Path(self.tmp.name) / "seed"
        subprocess.check_call(
            ["git", "init", "-q", "-b", "main", str(seed)], stdout=subprocess.DEVNULL
        )
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(seed), "config", k, v])
        subprocess.check_call(["git", "-C", str(seed), "remote", "add", "origin", str(origin)])
        (seed / "README.md").write_text("project\n", encoding="utf-8")
        subprocess.check_call(["git", "-C", str(seed), "add", "-A"], stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "-C", str(seed), "commit", "-qm", "seed"])
        subprocess.check_call(
            ["git", "-C", str(seed), "push", "-q", "origin", "main"], stdout=subprocess.DEVNULL
        )
        return origin

    def _register_agents(self, hive: Path) -> None:
        (hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: bob\n  harness: codex\n  role: worker\n"
            "- id: op\n  harness: claude-code\n  role: operator\n",
            encoding="utf-8",
        )
        subprocess.check_call(["git", "-C", str(hive), "add", "-A"], stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "-C", str(hive), "commit", "-qm", "agents"])
        subprocess.check_call(
            ["git", "-C", str(hive), "push", "-q", "origin", "HEAD"], stdout=subprocess.DEVNULL
        )

    def _cli_init_in(self, work: Path, hive: Path) -> None:
        cwd = os.getcwd()
        os.chdir(str(work))
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init", "--hive", str(hive)]), 0)
        finally:
            os.chdir(cwd)
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(hive), "config", k, v])

    def test_cli_init_bootstraps_then_publishes(self):
        origin = self._bare_project_origin()
        work = self._project_clone("work", origin)
        hive = work / "_swarm"
        self._cli_init_in(work, hive)
        self.assertTrue((hive / "PROTOCOL.md").is_file())
        self._register_agents(hive)
        self.assertEqual(
            main(["inbox-add", "--hive", str(hive), "--title", "Pub", "--created-by", "op"]),
            0,
        )
        task_id = next(iter((hive / "inbox").glob("task_*.json"))).stem
        self.assertEqual(
            main([
                "claim", "--hive", str(hive), "--task", task_id,
                "--agent", "alice", "--harness", "claude-code",
            ]),
            0,
        )
        listed = subprocess.check_output(
            ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "swarm"],
            text=True,
        )
        self.assertIn(f"claims/{task_id}.json", listed)
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(hive), "status", "--porcelain"], text=True
            ),
            "",
        )

    def test_cli_two_clone_lost_race(self):
        origin = self._bare_project_origin()
        work_a = self._project_clone("work_a", origin)
        hive_a = work_a / "_swarm"
        self._cli_init_in(work_a, hive_a)
        self._register_agents(hive_a)
        self.assertEqual(
            main(["inbox-add", "--hive", str(hive_a), "--title", "Race", "--created-by", "op"]),
            0,
        )
        task_id = next(iter((hive_a / "inbox").glob("task_*.json"))).stem

        work_b = self._project_clone("work_b", origin)
        hive_b = work_b / "_swarm"
        self._cli_init_in(work_b, hive_b)
        self.assertEqual(
            main([
                "claim", "--hive", str(hive_a), "--task", task_id,
                "--agent", "alice", "--harness", "claude-code",
            ]),
            0,
        )
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "claim", "--hive", str(hive_b), "--task", task_id,
                "--agent", "bob", "--harness", "codex",
            ])
        self.assertEqual(rc, 2)
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(hive_b), "status", "--porcelain"], text=True
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
