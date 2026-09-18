# tests/test_message.py — open-ended message surface (CLI + publish allowlist)
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rip_swarm.cli import main

ROOT = Path(__file__).resolve().parents[1]


class TestMessageSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name) / "_swarm"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--hive", str(self.hive), "--no-git"]), 0)
        (self.hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: bob\n  harness: codex\n  role: worker\n",
            encoding="utf-8",
        )

    def test_unregistered_from_refused(self):
        self._seed()
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "message", "--hive", str(self.hive),
                "--from", "ghost", "--to", "bob",
                "--type", "note", "--body", "hi", "--local",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("ghost", err.getvalue())
        # Template ships an empty messages.jsonl; refuse must leave it empty.
        self.assertEqual(
            (self.hive / "store" / "messages.jsonl").read_text(encoding="utf-8"), ""
        )
        self.assertFalse((self.hive / "agents" / "ghost").exists())
        self.assertFalse((self.hive / "agents" / "alice" / "outbox").exists())

    def test_a_to_b_without_a_claim(self):
        self._seed()
        # No inbox, no claims — messaging must still work.
        self.assertEqual(list((self.hive / "inbox").glob("task_*.json")), [])
        self.assertEqual(list((self.hive / "claims").glob("*.json")), [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main([
                "message", "--hive", str(self.hive),
                "--from", "alice", "--to", "bob",
                "--type", "note", "--body", "need a review", "--local",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(list((self.hive / "claims").glob("*.json")), [])
        outboxes = list((self.hive / "agents" / "alice" / "outbox").glob("msg_*.json"))
        self.assertEqual(len(outboxes), 1)
        doc = json.loads(outboxes[0].read_text(encoding="utf-8"))
        self.assertEqual(doc["from"]["agent"], "alice")
        self.assertEqual(doc["to"], "bob")
        self.assertEqual(doc["type"], "note")
        self.assertEqual(doc["body"], {"text": "need a review"})
        lines = (self.hive / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], doc["id"])
        # Never wrote into bob's outbox (no shared mailbox).
        self.assertFalse((self.hive / "agents" / "bob" / "outbox").exists())

    def test_a_to_star_without_a_claim(self):
        self._seed()
        with redirect_stdout(io.StringIO()):
            rc = main([
                "message", "--hive", str(self.hive),
                "--from", "alice", "--to", "*",
                "--type", "ops", "--body", "standup", "--local",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(list((self.hive / "claims").glob("*.json")), [])
        doc = json.loads(
            next((self.hive / "agents" / "alice" / "outbox").glob("msg_*.json")).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(doc["to"], "*")
        self.assertEqual(doc["type"], "ops")

    def test_unknown_to_refused(self):
        self._seed()
        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "message", "--hive", str(self.hive),
                "--from", "alice", "--to", "nobody",
                "--type", "note", "--body", "x", "--local",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("nobody", err.getvalue())
        self.assertEqual(
            (self.hive / "store" / "messages.jsonl").read_text(encoding="utf-8"), ""
        )
        self.assertFalse((self.hive / "agents" / "alice" / "outbox").exists())

    def test_script_shim_works_from_other_cwd(self):
        self._seed()
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        help_run = subprocess.run(
            [__import__("sys").executable, str(ROOT / "scripts" / "message.py"), "-h"],
            cwd=str(other),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("--from", help_run.stdout)
        self.assertIn("--to", help_run.stdout)


class TestMessagePublishAllowlist(unittest.TestCase):
    """Publish path: only outbox + messages.jsonl; unrelated paths stay local."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

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

    def _project_clone(self, name: str, origin: Path) -> Path:
        work = Path(self.tmp.name) / name
        subprocess.check_call(
            ["git", "clone", "-q", str(origin), str(work)], stdout=subprocess.DEVNULL
        )
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(work), "config", k, v])
        return work

    def test_publish_does_not_drag_unrelated_paths(self):
        origin = self._bare_project_origin()
        work = self._project_clone("work", origin)
        hive = work / "_swarm"
        cwd = os.getcwd()
        os.chdir(str(work))
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init", "--hive", str(hive)]), 0)
        finally:
            os.chdir(cwd)
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            subprocess.check_call(["git", "-C", str(hive), "config", k, v])
        (hive / "agents" / "registry.yaml").write_text(
            "- id: alice\n  harness: claude-code\n  role: worker\n"
            "- id: bob\n  harness: codex\n  role: worker\n",
            encoding="utf-8",
        )
        subprocess.check_call(["git", "-C", str(hive), "add", "-A"], stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "-C", str(hive), "commit", "-qm", "agents"])
        subprocess.check_call(
            ["git", "-C", str(hive), "push", "-q", "origin", "HEAD"], stdout=subprocess.DEVNULL
        )

        # Pre-existing scratch must not ride along with a message publish.
        (hive / "UNRELATED_LEAK.txt").write_text("secret\n", encoding="utf-8")
        # Dirty tree blocks publish — discard? Spec: dirty refuses. So plant after
        # clean, inside the op? The CLI can't inject a leak mid-op. Instead: leave
        # the leak untracked, then... publish requires clean tree first.
        # So: remove leak, publish clean message, then separately verify allowlist
        # via a controlled publish that also writes a leak (library-level).
        (hive / "UNRELATED_LEAK.txt").unlink()

        with redirect_stdout(io.StringIO()):
            rc = main([
                "message", "--hive", str(hive),
                "--from", "alice", "--to", "bob",
                "--type", "note", "--body", "shipped",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(hive), "status", "--porcelain"], text=True),
            "",
        )
        listed = subprocess.check_output(
            ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "swarm"],
            text=True,
        )
        self.assertIn("agents/alice/outbox/", listed)
        self.assertIn("store/messages.jsonl", listed)
        self.assertNotIn("UNRELATED_LEAK.txt", listed)
        # Messaging must not create claim SoT files (template may keep claims/.gitkeep).
        claim_json = [ln for ln in listed.splitlines() if ln.startswith("claims/") and ln.endswith(".json")]
        self.assertEqual(claim_json, [])

        # Adversarial: wrap write_message so an unrelated path appears mid-op;
        # publish must refuse and leave the remote without the leak.
        from rip_swarm.gitops import GitopsError, publish
        from rip_swarm.outbox import write_message
        from rip_swarm.timeutil import now_utc

        now = now_utc()

        def leaky_op():
            doc = write_message(
                hive,
                agent="alice",
                harness="claude-code",
                type="note",
                to="*",
                body={"text": "leak-test"},
                now=now,
            )
            (hive / "UNRELATED_LEAK.txt").write_text("secret\n", encoding="utf-8")
            return doc

        with self.assertRaises(GitopsError) as ctx:
            publish(
                hive,
                task_id="__none__",
                op=leaky_op,
                message="message alice->* note",
                agent="alice",
                now=now,
                allow=[
                    "agents/alice/outbox/*.json",
                    "store/messages.jsonl",
                ],
            )
        self.assertIn("UNRELATED_LEAK.txt", str(ctx.exception))
        self.assertFalse((hive / "UNRELATED_LEAK.txt").exists())
        listed2 = subprocess.check_output(
            ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "swarm"],
            text=True,
        )
        self.assertNotIn("UNRELATED_LEAK.txt", listed2)
        # Remote still has only the first successful message's outbox entries
        # (the refused op rolled back).
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(hive), "status", "--porcelain"], text=True),
            "",
        )


if __name__ == "__main__":
    unittest.main()
