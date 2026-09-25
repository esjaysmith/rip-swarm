# tests/test_read_surface.py — sync, messages reader, success output, status titles
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from rip_swarm.cli import main

REGISTRY = (
    "- id: alice\n  harness: claude-code\n  role: worker\n"
    "- id: bob\n  harness: grok\n  role: worker\n"
)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def _identity(repo: Path) -> None:
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _git("-C", str(repo), "config", k, v)


class _TwoClones(unittest.TestCase):
    """Clone A seeds origin/swarm; clone B is a second harness's checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.a = root / "a" / "_swarm"
        self.b = root / "b" / "_swarm"
        _git("init", "--bare", "-q", "-b", "swarm", str(self.origin))
        self.assertEqual(_run(["init", "--hive", str(self.a), "--no-git"])[0], 0)
        (self.a / "agents" / "registry.yaml").write_text(REGISTRY, encoding="utf-8")
        _git("init", "-q", "-b", "swarm", str(self.a))
        _identity(self.a)
        _git("-C", str(self.a), "add", "-A")
        _git("-C", str(self.a), "commit", "-qm", "seed")
        _git("-C", str(self.a), "remote", "add", "origin", str(self.origin))
        _git("-C", str(self.a), "push", "-q", "-u", "origin", "swarm")
        _git("clone", "-q", "-b", "swarm", "--single-branch", str(self.origin), str(self.b))
        _identity(self.b)

    def tearDown(self):
        self.tmp.cleanup()

    def _head(self, hive: Path) -> str:
        return _git("-C", str(hive), "rev-parse", "HEAD").strip()


class TestSync(_TwoClones):
    def test_sync_pulls_other_clones_work(self):
        rc, out, _ = _run([
            "inbox-add", "--hive", str(self.a), "--title", "Review lever 1",
            "--created-by", "alice",
        ])
        self.assertEqual(rc, 0)
        task_id = out.split()[1].rstrip(":")
        self.assertFalse((self.b / "inbox" / f"{task_id}.json").exists())

        rc, out, err = _run(["sync", "--hive", str(self.b)])
        self.assertEqual(rc, 0, err)
        self.assertIn("pulled 1 commit", out)
        self.assertTrue((self.b / "inbox" / f"{task_id}.json").exists())
        self.assertEqual(self._head(self.a), self._head(self.b))

    def test_sync_up_to_date(self):
        rc, out, err = _run(["sync", "--hive", str(self.b)])
        self.assertEqual(rc, 0, err)
        self.assertIn("up to date", out)

    def test_sync_refuses_dirty_hive(self):
        (self.b / "inbox" / "stray.json").write_text("{}", encoding="utf-8")
        rc, _, err = _run(["sync", "--hive", str(self.b)])
        self.assertEqual(rc, 1)
        self.assertIn("dirty", err)

    def test_sync_refuses_unpushed_commits(self):
        self.assertEqual(
            _run(["inbox-add", "--hive", str(self.a), "--title", "t", "--created-by", "alice"])[0], 0
        )
        (self.b / "notes.txt").write_text("x", encoding="utf-8")
        _git("-C", str(self.b), "add", "notes.txt")
        _git("-C", str(self.b), "commit", "-qm", "local only")
        before = self._head(self.b)
        rc, _, err = _run(["sync", "--hive", str(self.b)])
        self.assertEqual(rc, 1)
        self.assertIn("unpushed", err)
        self.assertEqual(self._head(self.b), before)


class TestSuccessOutput(_TwoClones):
    def test_each_publishing_helper_prints_one_line(self):
        rc, out, _ = _run([
            "inbox-add", "--hive", str(self.a), "--title", "Review lever 1",
            "--created-by", "alice",
        ])
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^task task_[0-9A-Z]{26}: Review lever 1\n$")
        task_id = out.split()[1].rstrip(":")

        rc, out, _ = _run(["claim", "--hive", str(self.a), "--task", task_id, "--agent", "alice"])
        self.assertEqual(rc, 0)
        self.assertRegex(out, rf"^claimed {task_id} as alice until \d{{4}}-\d\d-\d\dT[\d:]+Z\n$")

        rc, out, _ = _run(["heartbeat", "--hive", str(self.a), "--task", task_id, "--agent", "alice"])
        self.assertEqual(rc, 0)
        self.assertRegex(out, rf"^heartbeat {task_id} as alice until .+Z\n$")

        rc, out, _ = _run([
            "message", "--hive", str(self.a), "--from", "alice", "--to", "bob",
            "--type", "note", "--body", "hi",
        ])
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^sent msg_[0-9A-Z]{26} alice -> bob\n$")

        rc, out, _ = _run([
            "complete", "--hive", str(self.a), "--task", task_id, "--agent", "alice",
            "--result-ref", "docs/review.md",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"complete {task_id} as alice (result_ref docs/review.md)\n")

    def test_release_prints_action(self):
        _, out, _ = _run(["inbox-add", "--hive", str(self.a), "--title", "t", "--created-by", "alice"])
        task_id = out.split()[1].rstrip(":")
        self.assertEqual(_run(["claim", "--hive", str(self.a), "--task", task_id, "--agent", "alice"])[0], 0)
        rc, out, _ = _run(["release", "--hive", str(self.a), "--task", task_id, "--agent", "alice"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"release {task_id} as alice\n")


class TestMessagesReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = Path(self.tmp.name) / "_swarm"
        self.assertEqual(_run(["init", "--hive", str(self.hive), "--no-git"])[0], 0)
        (self.hive / "agents" / "registry.yaml").write_text(
            REGISTRY + "- id: carol\n  harness: claude-code\n  role: worker\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, frm: str, to: str, text: str, at: str) -> str:
        with mock.patch("rip_swarm.cli.now_utc") as now:
            from rip_swarm.timeutil import parse_z

            now.return_value = parse_z(at)
            rc, out, err = _run([
                "message", "--hive", str(self.hive), "--local", "--from", frm,
                "--to", to, "--type", "note", "--body", text,
            ])
        self.assertEqual(rc, 0, err)
        return out.split()[1]

    def _seed(self):
        self._send("alice", "bob", "direct to bob", "2026-09-25T10:00:00Z")
        self._send("alice", "*", "broadcast", "2026-09-25T10:01:00Z")
        self._send("bob", "alice", "bob to alice", "2026-09-25T10:02:00Z")
        self._send("alice", "carol", "not for bob", "2026-09-25T10:03:00Z")
        self._send("carol", "orchestrator", "to baton", "2026-09-25T10:04:00Z")
        self._send("bob", "*", "own broadcast", "2026-09-25T10:05:00Z")

    def test_to_filters_direct_and_broadcast_excluding_own(self):
        self._seed()
        rc, out, err = _run(["messages", "--hive", str(self.hive), "--to", "bob"])
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertIn("alice -> bob [note] direct to bob", lines[0])
        self.assertIn("alice -> * [note] broadcast", lines[1])
        self.assertTrue(lines[0].startswith("2026-09-25T10:00:00Z msg_"))

    def test_since_is_exclusive(self):
        self._seed()
        rc, out, _ = _run([
            "messages", "--hive", str(self.hive), "--to", "bob",
            "--since", "2026-09-25T10:00:00Z",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("broadcast", out)

    def test_no_filter_lists_all_sorted(self):
        self._seed()
        rc, out, _ = _run(["messages", "--hive", str(self.hive)])
        self.assertEqual(rc, 0)
        stamps = [line.split()[0] for line in out.splitlines()]
        self.assertEqual(len(stamps), 6)
        self.assertEqual(stamps, sorted(stamps))

    def test_from_filter(self):
        self._seed()
        rc, out, _ = _run(["messages", "--hive", str(self.hive), "--from", "bob"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 2)

    def test_orchestrator_holder_sees_baton_messages(self):
        self._seed()
        profile = self.hive / "profiles" / "default.yaml"
        profile.write_text(
            profile.read_text(encoding="utf-8").replace(
                "allow_self_promote: false", "allow_self_promote: true"
            ),
            encoding="utf-8",
        )
        rc, _, err = _run(["promote", "--hive", str(self.hive), "--local", "--agent", "bob"])
        self.assertEqual(rc, 0, err)
        rc, out, _ = _run(["messages", "--hive", str(self.hive), "--to", "bob", "--type", "note"])
        self.assertEqual(rc, 0)
        self.assertIn("carol -> orchestrator [note] to baton", out)

    def test_type_filter_hides_promote_audit(self):
        self._seed()
        rc, out, _ = _run(["messages", "--hive", str(self.hive), "--type", "promote"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "(no messages)\n")

    def test_unknown_to_refused(self):
        rc, _, err = _run(["messages", "--hive", str(self.hive), "--to", "ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("ghost", err)

    def test_bad_since_refused(self):
        rc, _, err = _run(["messages", "--hive", str(self.hive), "--since", "yesterday"])
        self.assertEqual(rc, 1)
        self.assertIn("--since", err)

    def test_empty(self):
        rc, out, _ = _run(["messages", "--hive", str(self.hive), "--to", "bob"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "(no messages)\n")

    def test_read_only(self):
        self._seed()
        before = sorted(p.relative_to(self.hive) for p in self.hive.rglob("*"))
        _run(["messages", "--hive", str(self.hive), "--to", "bob"])
        self.assertEqual(sorted(p.relative_to(self.hive) for p in self.hive.rglob("*")), before)


class TestStatusTitles(unittest.TestCase):
    def test_inbox_and_active_claims_show_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            hive = Path(tmp) / "_swarm"
            self.assertEqual(_run(["init", "--hive", str(hive), "--no-git"])[0], 0)
            (hive / "agents" / "registry.yaml").write_text(REGISTRY, encoding="utf-8")
            _, out, _ = _run([
                "inbox-add", "--hive", str(hive), "--local", "--title", "Open one",
                "--created-by", "alice",
            ])
            open_id = out.split()[1].rstrip(":")
            _, out, _ = _run([
                "inbox-add", "--hive", str(hive), "--local", "--title", "Held one",
                "--created-by", "alice",
            ])
            held_id = out.split()[1].rstrip(":")
            self.assertEqual(
                _run(["claim", "--hive", str(hive), "--local", "--task", held_id, "--agent", "bob"])[0], 0
            )
            rc, out, _ = _run(["status", "--hive", str(hive)])
            self.assertEqual(rc, 0)
            self.assertIn(f"  {open_id} Open one", out)
            self.assertRegex(out, rf"  {held_id} agent=bob expires_at=\S+ Held one")


if __name__ == "__main__":
    unittest.main()
