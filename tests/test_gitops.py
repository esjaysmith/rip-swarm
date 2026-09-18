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
        # B now has an unpushed local commit. publish sees HEAD ahead of @{u}, fetches, finds
        # alice's unexpired claim on the remote tip and reports the lost race -- without
        # resetting, so B's unpushed commit survives for the operator to push or discard.
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.hb, text=True).strip()
        with self.assertRaises(ClaimDenied):
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=T0, lease_seconds=900,
            )
        self.assertEqual(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.hb, text=True).strip(),
            head,
        )
        self.assertEqual(read_json(self.hb / "claims" / f"{self.task['id']}.json")["agent"], "bob")
        self.assertEqual(
            read_json(self.ha / "claims" / f"{self.task['id']}.json")["agent"], "alice"
        )

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
                op=lambda: heartbeat(self.ha, self.task["id"], "alice", T0, 900),
                agent="alice", now=T0)

    def test_concurrent_audit_appends_union_merge(self):
        # Both hives append to messages.jsonl; second push must rebase cleanly via merge=union.
        def op_a():
            return write_message(self.ha, agent="alice", harness="claude-code", type="ops", to="*", body={"text": "a"}, now=T0)
        def op_b():
            return write_message(self.hb, agent="bob", harness="codex", type="ops", to="*", body={"text": "b"}, now=T0)
        publish(self.ha, task_id="__none__", op=op_a, message="msg a", agent="alice", now=T0)
        publish(self.hb, task_id="__none__", op=op_b, message="msg b", agent="bob", now=T0)
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

    def test_failed_op_resets_hive(self):
        before = (self.ha / "agents" / "registry.yaml").read_text(encoding="utf-8")

        def op():
            (self.ha / "scratch.txt").write_text("x", encoding="utf-8")
            (self.ha / "agents" / "registry.yaml").write_text(before + "# dirt\n", encoding="utf-8")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            publish(self.ha, task_id="__none__", op=op, message="x", agent="alice", now=T0)
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        self.assertFalse((self.ha / "scratch.txt").exists())
        self.assertEqual((self.ha / "agents" / "registry.yaml").read_text(encoding="utf-8"), before)

    def test_cap_denial_publishes_audit_and_leaves_hive_clean(self):
        from rip_swarm.claim import complete, heartbeat
        from rip_swarm.policy import try_claim_with_policy

        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )

        def add_task():
            return create_task(self.ha, title="U", created_by="op", now=T0)

        t2 = publish(
            self.ha, task_id="__none__", op=add_task, message="inbox-add U",
            agent="alice", now=T0, allow=["inbox/*.json"],
        )
        profile = {
            "worker_lease_ttl": "15m",
            "orchestrator_lease_ttl": "30m",
            "budget": {"max_claims_open_per_agent": 1},
        }

        def op():
            return try_claim_with_policy(
                self.ha,
                task_id=t2["id"],
                agent="alice",
                harness="claude-code",
                now=T0,
                profile=profile,
            )

        with self.assertRaises(ClaimDenied):
            publish(
                self.ha,
                task_id=t2["id"],
                op=op,
                message=f"claim {t2['id']}",
                agent="alice",
                now=T0,
            )
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        remote_msg = subprocess.check_output(
            ["git", "-C", str(self.ha), "show", "origin/swarm:store/messages.jsonl"],
            text=True,
        )
        self.assertIn("budget_block", remote_msg)
        self.assertFalse((self.ha / "claims" / f"{t2['id']}.json").exists())
        publish(
            self.ha,
            task_id=self.task["id"],
            message="heartbeat",
            op=lambda: heartbeat(self.ha, self.task["id"], "alice", T0, 900),
            agent="alice",
            now=T0,
        )
        publish(
            self.ha,
            task_id=self.task["id"],
            message="complete",
            op=lambda: complete(self.ha, self.task["id"], "alice", T0, "out"),
            agent="alice",
            now=T0,
        )
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )

    def test_lookback_then_claim_and_publish(self):
        from rip_swarm.lookback import write_lookback

        def op():
            path = write_lookback(self.ha, T0)
            return {"path": str(path)}

        publish(
            self.ha, task_id="__none__", op=op, message="lookback",
            agent="alice", now=T0, allow=["lookback/*.md"],
        )
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )

    def test_unexpired_held_by_other_expired_is_stealable(self):
        from rip_swarm.gitops import _unexpired_held_by_other

        expired = {"agent": "alice", "expires_at": "2026-09-17T09:00:00Z"}
        live = {"agent": "alice", "expires_at": "2026-09-17T10:00:00Z"}
        self.assertFalse(_unexpired_held_by_other(expired, "bob", T0))
        self.assertTrue(_unexpired_held_by_other(live, "bob", T0))
        self.assertFalse(_unexpired_held_by_other(live, "alice", T0))

    def test_expired_foreign_claim_rebases_after_rejected_push(self):
        from rip_swarm import gitops
        from rip_swarm.timeutil import add_seconds

        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=1,
        )
        later = add_seconds(T0, 2)
        real_run = gitops._run
        injected = {"done": False}

        def wrapped(hive, *args, check=True):
            hive_p = Path(hive).resolve()
            if hive_p == self.hb.resolve() and args[:1] == ("push",) and not injected["done"]:
                injected["done"] = True

                def op_a():
                    return write_message(
                        self.ha,
                        agent="alice",
                        harness="claude-code",
                        type="ops",
                        to="*",
                        body={"text": "jsonl only"},
                        now=later,
                    )

                gitops.publish(
                    self.ha, task_id="__none__", op=op_a, message="msg a",
                    agent="alice", now=later,
                )
            return real_run(hive, *args, check=check)

        gitops._run = wrapped
        try:
            claim_and_publish(
                self.hb,
                task_id=self.task["id"],
                agent="bob",
                harness="codex",
                now=later,
                lease_seconds=900,
            )
        finally:
            gitops._run = real_run

        self.assertTrue(injected["done"])
        self.assertEqual(read_json(self.hb / "claims" / f"{self.task['id']}.json")["agent"], "bob")
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True),
            "",
        )

    # --- item 1: unpushed local commit must never be destroyed ---

    def _head(self, repo):
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    def _local_claim_commit(self, hive, agent, harness, now):
        from rip_swarm.claim import try_claim

        try_claim(hive, self.task["id"], agent, harness, now, 900)
        _git(hive, "add", "-A")
        _git(hive, "commit", "-m", f"{agent} local claim")
        return self._head(hive)

    def test_unpushed_commit_survives_expired_remote_claim(self):
        from rip_swarm.gitops import GitopsError
        from rip_swarm.timeutil import add_seconds

        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=1,
        )
        later = add_seconds(T0, 120)
        _git(self.hb, "fetch")
        _git(self.hb, "reset", "--hard", "@{u}")
        head = self._local_claim_commit(self.hb, "bob", "codex", later)
        # alice's claim on the remote tip is expired: this is not a lost race, but bob's
        # unpushed commit must survive regardless of which error publish reports.
        with self.assertRaises(GitopsError) as ctx:
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=later, lease_seconds=900,
            )
        self.assertNotIsInstance(ctx.exception, ClaimDenied)
        self.assertEqual(self._head(self.hb), head)

    def test_unpushed_commit_survives_unexpired_remote_claim(self):
        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )
        _git(self.hb, "fetch")
        _git(self.hb, "reset", "--hard", "@{u}")
        # bob's local claim file loses to alice on the remote tip, but bob's commit survives.
        _git(self.hb, "rm", "-q", "--cached", f"claims/{self.task['id']}.json")
        (self.hb / "notes.txt").write_text("bob work\n", encoding="utf-8")
        _git(self.hb, "add", "-A")
        _git(self.hb, "commit", "-m", "bob local work")
        head = self._head(self.hb)
        with self.assertRaises(ClaimDenied):
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=T0, lease_seconds=900,
            )
        self.assertEqual(self._head(self.hb), head)
        self.assertTrue((self.hb / "notes.txt").exists())

    def test_unpushed_commit_survives_none_task_publish(self):
        from rip_swarm.gitops import GitopsError

        (self.ha / "notes.txt").write_text("local only\n", encoding="utf-8")
        _git(self.ha, "add", "-A")
        _git(self.ha, "commit", "-m", "unpushed local work")
        head = self._head(self.ha)
        with self.assertRaises(GitopsError) as ctx:
            publish(
                self.ha,
                task_id="__none__",
                op=lambda: write_message(
                    self.ha, agent="alice", harness="claude-code", type="ops",
                    to="*", body={"text": "x"}, now=T0,
                ),
                message="msg",
                agent="alice",
                now=T0,
            )
        self.assertNotIsInstance(ctx.exception, ClaimDenied)
        self.assertEqual(self._head(self.ha), head)
        self.assertTrue((self.ha / "notes.txt").exists())

    # --- item 2: malformed claim on the remote tip ---

    def test_unreadable_remote_claim_denies_instead_of_crashing(self):
        path = self.ha / "claims" / f"{self.task['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        _git(self.ha, "add", "-A")
        _git(self.ha, "commit", "-m", "corrupt claim")
        _git(self.ha, "push")
        _git(self.hb, "fetch")
        with self.assertRaises(ClaimDenied) as ctx:
            claim_and_publish(
                self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                now=T0, lease_seconds=900,
            )
        self.assertIn("unreadable claim", str(ctx.exception))
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True),
            "",
        )

    # --- item 3: symlink pointing outside the hive ---

    def test_untracked_symlink_outside_hive_is_removed_and_target_survives(self):
        target = self.a / "app.py"
        before = target.read_text(encoding="utf-8")

        def op():
            (self.ha / "escape.py").symlink_to(target)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            publish(self.ha, task_id="__none__", op=op, message="x", agent="alice", now=T0)
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        self.assertFalse((self.ha / "escape.py").is_symlink())
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_untracked_dir_symlink_outside_hive_is_removed_and_target_survives(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")

        def op():
            (self.ha / "escape_dir").symlink_to(outside, target_is_directory=True)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            publish(self.ha, task_id="__none__", op=op, message="x", agent="alice", now=T0)
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        self.assertFalse((self.ha / "escape_dir").is_symlink())
        self.assertTrue((outside / "keep.txt").is_file())

    # --- item 4: agent/now required; rebase conflict on the claim file ---

    def test_publish_requires_agent_and_now(self):
        with self.assertRaises(TypeError):
            publish(self.ha, task_id="__none__", op=lambda: {}, message="x")

    def test_rebase_addadd_conflict_on_claim_is_lost_race(self):
        from rip_swarm import gitops

        real_run = gitops._run
        injected = {"done": False}

        def wrapped(hive, *args, check=True):
            if Path(hive).resolve() == self.hb.resolve() and args[:1] == ("push",) and not injected["done"]:
                injected["done"] = True
                # alice lands her claim AND expires it, so the expiry-aware check does not
                # short-circuit: the rebase must hit an add/add conflict on the claim file.
                claim_and_publish(
                    self.ha, task_id=self.task["id"], agent="alice",
                    harness="claude-code", now=T0, lease_seconds=1,
                )
            return real_run(hive, *args, check=check)

        gitops._run = wrapped
        try:
            from rip_swarm.timeutil import add_seconds

            later = add_seconds(T0, 300)
            with self.assertRaises(ClaimDenied) as ctx:
                claim_and_publish(
                    self.hb, task_id=self.task["id"], agent="bob", harness="codex",
                    now=later, lease_seconds=900,
                )
        finally:
            gitops._run = real_run
        self.assertTrue(injected["done"])
        self.assertIn("lost race", str(ctx.exception))
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True),
            "",
        )
        self.assertFalse((self.hb / ".git" / "rebase-merge").exists())
        self.assertFalse((self.hb / ".git" / "rebase-apply").exists())

    # --- item 6: helper retry loop, conflicts, exhaustion ---

    def test_union_merge_through_retry_loop(self):
        # Force a rejected push mid-flight so the helper itself rebases; both JSONL
        # appends must survive on both sides.
        from rip_swarm import gitops

        real_run = gitops._run
        injected = {"done": False}

        def wrapped(hive, *args, check=True):
            if Path(hive).resolve() == self.hb.resolve() and args[:1] == ("push",) and not injected["done"]:
                injected["done"] = True
                gitops.publish(
                    self.ha,
                    task_id="__none__",
                    op=lambda: write_message(
                        self.ha, agent="alice", harness="claude-code", type="ops",
                        to="*", body={"text": "a"}, now=T0,
                    ),
                    message="msg a",
                    agent="alice",
                    now=T0,
                )
            return real_run(hive, *args, check=check)

        gitops._run = wrapped
        try:
            gitops.publish(
                self.hb,
                task_id="__none__",
                op=lambda: write_message(
                    self.hb, agent="bob", harness="codex", type="ops",
                    to="*", body={"text": "b"}, now=T0,
                ),
                message="msg b",
                agent="bob",
                now=T0,
            )
        finally:
            gitops._run = real_run
        self.assertTrue(injected["done"])
        lines = (self.hb / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual({json.loads(l)["from"]["agent"] for l in lines}, {"alice", "bob"})
        _git(self.ha, "pull", "--rebase")
        lines_a = (self.ha / "store" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual({json.loads(l)["from"]["agent"] for l in lines_a}, {"alice", "bob"})
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True),
            "",
        )

    def test_rebase_conflict_on_non_jsonl_leaves_clean_tree(self):
        from rip_swarm import gitops
        from rip_swarm.gitops import GitopsError

        real_run = gitops._run
        injected = {"done": False}

        def edit_registry(hive, text):
            (hive / "agents" / "registry.yaml").write_text(text, encoding="utf-8")
            return {"ok": True}

        def wrapped(hive, *args, check=True):
            if Path(hive).resolve() == self.hb.resolve() and args[:1] == ("push",) and not injected["done"]:
                injected["done"] = True
                gitops.publish(
                    self.ha,
                    task_id="__none__",
                    op=lambda: edit_registry(self.ha, REGISTRY + "# alice\n"),
                    message="reg a",
                    agent="alice",
                    now=T0,
                    allow=["agents/registry.yaml"],
                )
            return real_run(hive, *args, check=check)

        gitops._run = wrapped
        try:
            with self.assertRaises(GitopsError):
                gitops.publish(
                    self.hb,
                    task_id="__none__",
                    op=lambda: edit_registry(self.hb, REGISTRY + "# bob\n"),
                    message="reg b",
                    agent="bob",
                    now=T0,
                    allow=["agents/registry.yaml"],
                )
        finally:
            gitops._run = real_run
        self.assertTrue(injected["done"])
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True),
            "",
        )
        self.assertFalse((self.hb / ".git" / "rebase-merge").exists())
        self.assertFalse((self.hb / ".git" / "rebase-apply").exists())
        self.assertEqual(
            self._head(self.hb),
            subprocess.check_output(
                ["git", "rev-parse", "@{u}"], cwd=self.hb, text=True
            ).strip(),
        )

    def test_max_attempts_exhaustion_raises_with_clean_tree(self):
        from rip_swarm import gitops
        from rip_swarm.gitops import GitopsError

        real_run = gitops._run
        pushes = {"n": 0}

        def wrapped(hive, *args, check=True):
            if Path(hive).resolve() == self.hb.resolve() and args[:1] == ("push",):
                pushes["n"] += 1
                gitops.publish(
                    self.ha,
                    task_id="__none__",
                    op=lambda: write_message(
                        self.ha, agent="alice", harness="claude-code", type="ops",
                        to="*", body={"text": f"a{pushes['n']}"}, now=T0,
                    ),
                    message=f"msg a{pushes['n']}",
                    agent="alice",
                    now=T0,
                )
            return real_run(hive, *args, check=check)

        gitops._run = wrapped
        try:
            with self.assertRaises(GitopsError) as ctx:
                gitops.publish(
                    self.hb,
                    task_id="__none__",
                    op=lambda: write_message(
                        self.hb, agent="bob", harness="codex", type="ops",
                        to="*", body={"text": "b"}, now=T0,
                    ),
                    message="msg b",
                    max_attempts=2,
                    agent="bob",
                    now=T0,
                )
        finally:
            gitops._run = real_run
        self.assertIn("max attempts", str(ctx.exception))
        self.assertEqual(pushes["n"], 2)
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.hb, text=True),
            "",
        )
        self.assertEqual(
            self._head(self.hb),
            subprocess.check_output(
                ["git", "rev-parse", "@{u}"], cwd=self.hb, text=True
            ).strip(),
        )


    # --- M1: publish commits only the paths the op is allowed to write (section 8.5) ---

    def _remote_tree(self, hive):
        return subprocess.check_output(
            ["git", "-C", str(hive), "ls-tree", "-r", "--name-only", "origin/swarm"],
            text=True,
        )

    def test_op_writing_outside_allowlist_is_refused_and_nothing_is_pushed(self):
        from rip_swarm.claim import try_claim
        from rip_swarm.gitops import GitopsError

        def op():
            doc = try_claim(self.ha, self.task["id"], "alice", "claude-code", T0, 900)
            (self.ha / "UNRELATED_LEAK.txt").write_text("secret\n", encoding="utf-8")
            return doc

        with self.assertRaises(GitopsError) as ctx:
            publish(
                self.ha,
                task_id=self.task["id"],
                op=op,
                message=f"claim {self.task['id']}",
                agent="alice",
                now=T0,
            )
        self.assertIn("UNRELATED_LEAK.txt", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, ClaimDenied)
        # tree clean, leak gone, and neither the leak nor the claim reached the remote
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        self.assertFalse((self.ha / "UNRELATED_LEAK.txt").exists())
        _git(self.ha, "fetch")
        listed = self._remote_tree(self.ha)
        self.assertNotIn("UNRELATED_LEAK.txt", listed)
        self.assertNotIn(f"claims/{self.task['id']}.json", listed)

    def test_pre_existing_scratch_file_is_never_swept_into_a_commit(self):
        # An untracked file that predates the op makes the hive dirty, so publish
        # refuses up front rather than committing someone else's scratch file.
        (self.ha / "scratch.env").write_text("TOKEN=x\n", encoding="utf-8")
        with self.assertRaises(DirtyHive):
            claim_and_publish(
                self.ha, task_id=self.task["id"], agent="alice",
                harness="claude-code", now=T0, lease_seconds=900,
            )
        self.assertTrue((self.ha / "scratch.env").exists())
        _git(self.ha, "fetch")
        self.assertNotIn("scratch.env", self._remote_tree(self.ha))

    def test_allowlist_star_does_not_cross_a_slash(self):
        from rip_swarm.gitops import GitopsError

        def op():
            nested = self.ha / "agents" / "alice" / "outbox" / "deep"
            nested.mkdir(parents=True, exist_ok=True)
            (nested / "x.json").write_text("{}\n", encoding="utf-8")
            return {}

        with self.assertRaises(GitopsError) as ctx:
            publish(
                self.ha, task_id="__none__", op=op, message="x",
                agent="alice", now=T0,
            )
        self.assertIn("agents/alice/outbox/deep/x.json", str(ctx.exception))
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )

    def test_default_allow_covers_the_claim_lifecycle(self):
        from rip_swarm.gitops import default_allow

        allow = default_allow("task_01ABC", "alice")
        self.assertIn("claims/task_01ABC.json", allow)
        self.assertIn("claims/task_01ABC.*.json", allow)
        self.assertIn("store/claims.jsonl", allow)
        self.assertIn("store/messages.jsonl", allow)
        self.assertIn("agents/alice/outbox/*.json", allow)
        self.assertIn("orchestrator/CURRENT.json", default_allow("orchestrator", "alice"))

    def test_default_allow_treats_ids_as_literals_not_globs(self):
        from rip_swarm.gitops import _match_allow, default_allow

        allow = tuple(default_allow("task_[x]*", "alice"))
        self.assertTrue(_match_allow("claims/task_[x]*.json", allow))
        self.assertFalse(_match_allow("claims/task_x.json", allow))
        self.assertFalse(_match_allow("claims/task_anything.json", allow))

    def test_lifecycle_ops_publish_under_the_default_allowlist(self):
        from rip_swarm.claim import complete, heartbeat, reject, release

        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )
        publish(
            self.ha, task_id=self.task["id"], message="heartbeat",
            op=lambda: heartbeat(self.ha, self.task["id"], "alice", T0, 900),
            agent="alice", now=T0,
        )
        publish(
            self.ha, task_id=self.task["id"], message="complete",
            op=lambda: complete(self.ha, self.task["id"], "alice", T0, "out"),
            agent="alice", now=T0,
        )
        for action, fn in (("release", release), ("reject", reject)):
            task = publish(
                self.ha,
                task_id="__none__",
                op=lambda: create_task(self.ha, title=action, created_by="op", now=T0),
                message=f"inbox-add {action}",
                agent="alice",
                now=T0,
                allow=["inbox/*.json"],
            )
            claim_and_publish(
                self.ha, task_id=task["id"], agent="alice", harness="claude-code",
                now=T0, lease_seconds=900,
            )
            publish(
                self.ha, task_id=task["id"], message=action,
                op=(lambda t=task["id"], f=fn: f(self.ha, t, "alice", T0, None)),
                agent="alice", now=T0,
            )
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        _git(self.ha, "fetch")
        listed = self._remote_tree(self.ha)
        self.assertIn("store/claims.jsonl", listed)
        self.assertIn(f"claims/{self.task['id']}.complete.", listed)

    def test_promote_and_release_orchestrator_publish_under_the_default_allowlist(self):
        from rip_swarm.gitops import promote_and_publish
        from rip_swarm.orchestrator import release_orchestrator

        promote_and_publish(
            self.ha, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="designated", allow_self_promote=True,
            operators=[],
        )
        _git(self.ha, "fetch")
        self.assertIn("orchestrator/CURRENT.json", self._remote_tree(self.ha))
        publish(
            self.ha,
            task_id="orchestrator",
            op=lambda: release_orchestrator(self.ha, agent="alice", now=T0, note=None),
            message="release orchestrator",
            agent="alice",
            now=T0,
        )
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )
        _git(self.ha, "fetch")
        self.assertNotIn("orchestrator/CURRENT.json", self._remote_tree(self.ha))

    def test_promote_by_operator_publishes_the_by_agent_outbox(self):
        # C1: promote --by OPERATOR writes the promote message to the *by* agent's
        # outbox, not the promoted agent's -- the allowlist must cover both.
        from rip_swarm.gitops import promote_and_publish

        (self.ha / "agents" / "registry.yaml").write_text(
            REGISTRY + "- id: op\n  harness: human\n  role: operator\n",
            encoding="utf-8",
        )
        (self.ha / "profiles").mkdir(exist_ok=True)
        (self.ha / "profiles" / "default.yaml").write_text(
            "operators: [op]\n", encoding="utf-8"
        )
        _git(self.ha, "add", "-A")
        _git(self.ha, "commit", "-m", "register operator")
        _git(self.ha, "push")

        promote_and_publish(
            self.ha, agent="alice", harness="claude-code", now=T0,
            lease_seconds=1800, reason="designated", allow_self_promote=False,
            operators=["op"], by="op",
        )
        _git(self.ha, "fetch")
        listed = self._remote_tree(self.ha)
        self.assertIn("claims/orchestrator.json", listed)
        self.assertIn("orchestrator/CURRENT.json", listed)
        self.assertIn("agents/op/outbox/", listed)
        remote_msgs = subprocess.check_output(
            ["git", "-C", str(self.ha), "show", "origin/swarm:store/messages.jsonl"],
            text=True,
        )
        self.assertIn('"type": "promote"', remote_msgs)
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )

    # --- M2: claim_and_publish honours registry + budget (section 5) ---

    def test_claim_and_publish_refuses_unregistered_agent(self):
        from rip_swarm.registry import UnknownAgent

        before = subprocess.check_output(
            ["git", "-C", str(self.ha), "rev-parse", "origin/swarm"], text=True
        ).strip()
        with self.assertRaises(UnknownAgent):
            claim_and_publish(
                self.ha, task_id=self.task["id"], agent="ghost",
                harness="claude-code", now=T0, lease_seconds=900,
            )
        _git(self.ha, "fetch")
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.ha), "rev-parse", "origin/swarm"], text=True
            ).strip(),
            before,
        )
        self.assertFalse((self.ha / "claims" / f"{self.task['id']}.json").exists())
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )

    def test_claim_and_publish_enforces_the_open_claim_cap(self):
        claim_and_publish(
            self.ha, task_id=self.task["id"], agent="alice", harness="claude-code",
            now=T0, lease_seconds=900,
        )
        second = publish(
            self.ha,
            task_id="__none__",
            op=lambda: create_task(self.ha, title="U", created_by="op", now=T0),
            message="inbox-add U",
            agent="alice",
            now=T0,
            allow=["inbox/*.json"],
        )
        # default profile caps open claims at 1: budget_block is published, then denied.
        with self.assertRaises(ClaimDenied) as ctx:
            claim_and_publish(
                self.ha, task_id=second["id"], agent="alice", harness="claude-code",
                now=T0, lease_seconds=900,
            )
        self.assertIn("max_claims_open_per_agent", str(ctx.exception))
        _git(self.ha, "fetch")
        remote_msgs = subprocess.check_output(
            ["git", "-C", str(self.ha), "show", "origin/swarm:store/messages.jsonl"],
            text=True,
        )
        self.assertIn("budget_block", remote_msgs)
        self.assertFalse((self.ha / "claims" / f"{second['id']}.json").exists())
        self.assertNotIn(
            f"claims/{second['id']}.json", self._remote_tree(self.ha)
        )
        self.assertEqual(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=self.ha, text=True),
            "",
        )

    def test_claim_and_publish_refuses_the_orchestrator_baton(self):
        with self.assertRaises(ClaimDenied):
            claim_and_publish(
                self.ha, task_id="orchestrator", agent="alice",
                harness="claude-code", now=T0, lease_seconds=900,
            )
        self.assertFalse((self.ha / "claims" / "orchestrator.json").exists())
        self.assertFalse((self.ha / "orchestrator" / "CURRENT.json").exists())


if __name__ == "__main__":
    unittest.main()
