# tests/test_join.py — join and leave against a real project + bare origin (spec §4, §5)
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import rip_swarm.join as j
from hivekit import T0, git, make_project, remote_files, remote_show
from rip_swarm.claim import ClaimDenied, try_claim
from rip_swarm.cli import main
from rip_swarm.gitops import GitopsError, publish_or_apply
from rip_swarm.inbox import create_task
from rip_swarm.init_hive import _DEFAULT_TEMPLATE
from rip_swarm.join import join, leave
from rip_swarm.project import FALLBACK_EMAIL
from rip_swarm.state import load_state
from rip_swarm.waiter import Wake, tick


def _fields(result):
    return dict(line.split("=", 1) for line in result.lines() if not line.startswith("NOTE="))


class TestJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Review focus 2: a project path with a space in it.
        self.origin, self.repo = make_project(self.root, "my project")

    def tearDown(self):
        self.tmp.cleanup()

    def _live_baton(self):
        return remote_show(self.origin, "claims/orchestrator.json")

    def test_first_worker_bootstraps_without_touching_the_project(self):
        result = join(self.repo, role="worker", harness="claude-code", now=T0)
        f = _fields(result)
        self.assertEqual(f["AGENT"], "claude-1")
        self.assertEqual(f["ROLE"], "worker")
        self.assertEqual(Path(f["HIVE"]), (self.repo / ".git" / "rip-swarm" / "hive-claude-1").resolve())
        self.assertEqual(Path(f["WORKTREE"]), (self.repo / ".worktrees" / "claude-1").resolve())
        self.assertEqual(f["BRANCH"], "rip-swarm/claude-1")
        self.assertEqual(f["INTEGRATION"], "rip-swarm/integration")
        self.assertIn("NOTE=based on HEAD (no rip-swarm/integration yet)", result.lines())
        self.assertIn("agents/claude-1/member.json", remote_files(self.origin))
        self.assertIn("agents/registry.yaml", remote_files(self.origin))
        self.assertFalse((self.repo / "_swarm").exists())
        self.assertFalse((self.repo / ".gitignore").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertIn(".worktrees/", (self.repo / ".git" / "info" / "exclude").read_text())
        self.assertFalse(any(p.name.startswith("pending-") for p in (self.repo / ".git" / "rip-swarm").iterdir()))

    def test_ids_per_harness_and_rejoin_gets_a_new_id(self):
        self.assertEqual(join(self.repo, role="worker", harness="claude-code", now=T0).agent, "claude-1")
        self.assertEqual(join(self.repo, role="worker", harness="grok", now=T0).agent, "grok-1")
        self.assertEqual(join(self.repo, role="worker", harness="claude-code", now=T0).agent, "claude-2")

    def test_master_takes_the_baton_and_workers_base_on_integration(self):
        master = join(self.repo, role="master", harness="grok", now=T0)
        self.assertEqual(master.role, "master")
        self.assertEqual(master.branch, "rip-swarm/integration")
        self.assertIn('"agent": "grok-1"', self._live_baton())
        self.assertTrue((self.repo / ".worktrees" / "integration").is_dir())
        worker = join(self.repo, role="worker", harness="claude-code", now=T0)
        self.assertNotIn("NOTE=based on HEAD (no rip-swarm/integration yet)", worker.lines())

    def test_second_master_is_refused_with_exit_2_and_leaves_nothing(self):
        join(self.repo, role="master", harness="grok", now=T0)
        before = remote_files(self.origin)
        with self.assertRaises(ClaimDenied) as ctx:
            join(self.repo, role="master", harness="claude-code", now=T0)
        self.assertIn("grok-1", str(ctx.exception))
        self.assertEqual(remote_files(self.origin), before)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with mock.patch("rip_swarm.cli.now_utc", return_value=T0):
                rc = main(["join", "--role", "master", "--harness", "claude-code",
                           "--project", str(self.repo)])
        self.assertEqual(rc, 2)

    def test_pre_template_hive_refuses_master_but_not_worker(self):
        old = self.root / "old-template"
        shutil.copytree(_DEFAULT_TEMPLATE, old)
        (old / "agents" / "registry.yaml").write_text("[]\n", encoding="utf-8")
        prof = old / "profiles" / "default.yaml"
        prof.write_text(prof.read_text().replace("operators: [op]", "operators: []"), encoding="utf-8")
        with self.assertRaises(GitopsError) as ctx:
            join(self.repo, role="master", harness="grok", now=T0, template=old)
        self.assertIn("operators: [op]", str(ctx.exception))
        self.assertFalse(any(p.endswith("member.json") for p in remote_files(self.origin)))
        self.assertEqual(join(self.repo, role="worker", harness="grok", now=T0).agent, "grok-1")

    def test_lost_promote_race_undoes_the_member(self):
        with mock.patch.object(j, "promote_and_publish", side_effect=ClaimDenied("lost race on remote tip")):
            with self.assertRaises(ClaimDenied):
                join(self.repo, role="master", harness="grok", now=T0)
        self.assertIsNotNone(remote_show(self.origin, "agents/grok-1/member.json"))
        self.assertTrue(any(p.startswith("agents/grok-1/member.left.") for p in remote_files(self.origin)))
        self.assertFalse((self.repo / ".git" / "rip-swarm" / "hive-grok-1").exists())

    def test_failure_after_promote_releases_the_baton_first(self):
        real = j.ensure_worktree

        def fail_for_integration(project, path, branch, base):
            if branch == "rip-swarm/integration":
                raise GitopsError("disk full")
            return real(project, path, branch, base)

        with mock.patch.object(j, "ensure_worktree", side_effect=fail_for_integration):
            with self.assertRaises(GitopsError):
                join(self.repo, role="master", harness="grok", now=T0)
        self.assertIsNone(self._live_baton())
        self.assertTrue(any(p.startswith("agents/grok-1/member.left.") for p in remote_files(self.origin)))
        self.assertEqual(join(self.repo, role="master", harness="claude-code", now=T0).role, "master")

    def test_failed_undo_keeps_the_clone_and_says_how_to_finish(self):
        real = j.ensure_worktree

        def fail_for_integration(project, path, branch, base):
            if branch == "rip-swarm/integration":
                raise GitopsError("disk full")
            return real(project, path, branch, base)

        hive = self.repo / ".git" / "rip-swarm" / "hive-grok-1"
        with mock.patch.object(j, "ensure_worktree", side_effect=fail_for_integration), \
                mock.patch.object(j, "leave", side_effect=GitopsError("network down")):
            with self.assertRaises(GitopsError) as ctx:
                join(self.repo, role="master", harness="grok", now=T0)
        text = str(ctx.exception)
        self.assertIn("disk full", text)
        self.assertIn("undo failed: network down", text)
        self.assertIn(f"leave --hive {hive} --agent grok-1", text)
        self.assertTrue(hive.is_dir())
        err = io.StringIO()
        with mock.patch.object(j, "save_state", side_effect=OSError("read-only")), \
                mock.patch.object(j, "leave", side_effect=GitopsError("network down")), \
                redirect_stderr(err), redirect_stdout(io.StringIO()), \
                mock.patch("rip_swarm.cli.now_utc", return_value=T0):
            rc = main(["join", "--role", "worker", "--harness", "grok", "--project", str(self.repo)])
        self.assertEqual(rc, 1)
        self.assertIn("leave --hive", err.getvalue())

    def test_leave_removes_the_clone_even_if_tidy_raises(self):
        worker = join(self.repo, role="worker", harness="grok", now=T0)
        with mock.patch.object(j, "_project_of", side_effect=OSError("permission denied")):
            lines = leave(worker.hive, worker.agent, T0)
        self.assertIn("left as grok-1", lines)
        self.assertIn("worktree not tidied: permission denied", lines)
        self.assertFalse(worker.hive.exists())

    def test_subdirectory_start_and_dirty_main_note(self):
        sub = self.repo / "pkg"
        sub.mkdir()
        (self.repo / "app.py").write_text("changed\n", encoding="utf-8")
        result = join(sub, role="worker", harness="grok", now=T0)
        self.assertEqual(result.worktree, (self.repo / ".worktrees" / "grok-1").resolve())
        self.assertIn("NOTE=MAIN has uncommitted changes; they are not on this branch", result.lines())

    def test_local_rip_swarm_branch_is_refused(self):
        git(self.repo, "branch", "rip-swarm")
        with self.assertRaises(GitopsError) as ctx:
            join(self.repo, role="worker", harness="grok", now=T0)
        self.assertIn("rip-swarm", str(ctx.exception))

    def test_late_worker_is_offered_the_open_board(self):
        master = join(self.repo, role="master", harness="grok", now=T0)
        t = publish_or_apply(
            master.hive, task_id="__none__",
            op=lambda: create_task(master.hive, title="t", created_by=master.agent, now=T0),
            message="inbox-add", agent=master.agent, now=T0, allow=["inbox/*.json"],
        )["id"]
        worker = join(self.repo, role="worker", harness="claude-code", now=T0)
        state = load_state(worker.hive, worker.agent)
        self.assertEqual(state["seen_open"], [])
        self.assertEqual(tick(worker.hive, worker.agent, state, T0, idle_after=600),
                         Wake("task-available", t))

    def test_join_without_user_identity_still_publishes_the_member(self):
        # Spec §11: a machine without user.email still publishes the member.
        git(self.repo, "config", "--unset", "user.name")
        git(self.repo, "config", "--unset", "user.email")
        empty = self.root / "empty-gitconfig"
        empty.write_text("", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k not in ("EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                            "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        env.update(GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_NOSYSTEM="1")
        with mock.patch.dict(os.environ, env, clear=True):
            worker = join(self.repo, role="worker", harness="grok", now=T0)
        self.assertIn(f"agents/{worker.agent}/member.json", remote_files(self.origin))
        self.assertEqual(git(worker.hive, "config", "user.email"), FALLBACK_EMAIL)

    def test_cli_join_prints_key_value_lines(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("rip_swarm.cli.now_utc", return_value=T0):
            rc = main(["join", "--role", "worker", "--harness", "grok", "--project", str(self.repo)])
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "VERSION=0.3.0")
        self.assertIn("AGENT=grok-1", lines)


class TestLeave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin, self.repo = make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_leave_releases_claims_and_is_idempotent(self):
        master = join(self.repo, role="master", harness="grok", now=T0)
        worker = join(self.repo, role="worker", harness="claude-code", now=T0)
        t = publish_or_apply(
            master.hive, task_id="__none__",
            op=lambda: create_task(master.hive, title="t", created_by=master.agent, now=T0),
            message="inbox-add", agent=master.agent, now=T0, allow=["inbox/*.json"],
        )["id"]
        from rip_swarm.gitops import sync
        sync(worker.hive)
        publish_or_apply(worker.hive, task_id=t,
                         op=lambda: try_claim(worker.hive, t, worker.agent, "claude-code", T0, 1800),
                         message="claim", agent=worker.agent, now=T0)
        lines = leave(worker.hive, worker.agent, T0)
        self.assertIn(f"released {t}", lines)
        self.assertIn("left as claude-1", lines)
        self.assertIn(f"removed {worker.worktree}", lines)      # clean, merged into integration
        self.assertFalse(worker.hive.exists())
        self.assertIsNone(remote_show(self.origin, f"claims/{t}.json"))
        self.assertEqual(leave(worker.hive, worker.agent, T0), ["already left"])
        lines = leave(master.hive, master.agent, T0)
        self.assertIn("released orchestrator", lines)
        self.assertIsNone(remote_show(self.origin, "claims/orchestrator.json"))
        self.assertTrue((self.repo / ".worktrees" / "integration").is_dir())

    def test_leave_keeps_a_dirty_or_unmerged_worktree(self):
        join(self.repo, role="master", harness="grok", now=T0)
        dirty = join(self.repo, role="worker", harness="claude-code", now=T0)
        (dirty.worktree / "wip.txt").write_text("x\n", encoding="utf-8")
        self.assertIn(f"kept {dirty.worktree}: uncommitted changes", leave(dirty.hive, dirty.agent, T0))
        self.assertTrue(dirty.worktree.is_dir())
        unmerged = join(self.repo, role="worker", harness="claude-code", now=T0)
        (unmerged.worktree / "done.txt").write_text("x\n", encoding="utf-8")
        git(unmerged.worktree, "add", "done.txt")
        git(unmerged.worktree, "commit", "-qm", "work")
        lines = leave(unmerged.hive, unmerged.agent, T0)
        self.assertIn(
            f"kept {unmerged.worktree}: rip-swarm/{unmerged.agent} is not merged into rip-swarm/integration",
            lines,
        )

    def test_cli_leave(self):
        worker = join(self.repo, role="worker", harness="grok", now=T0)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["leave", "--hive", str(worker.hive), "--agent", worker.agent])
        self.assertEqual(rc, 0)
        self.assertIn("left as grok-1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
