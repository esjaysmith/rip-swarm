# tests/test_packaging.py — skills package layout (spec §2)
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


def frontmatter(path: Path) -> dict:
    """The `key: value` lines between the leading `---` fences of a SKILL.md."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{path}: no frontmatter")
    head = text[4:text.index("\n---", 4)]
    out = {}
    for line in head.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


class TestPackaging(unittest.TestCase):
    def test_no_root_skill_md(self):
        # A root SKILL.md would shadow skills/*/SKILL.md for `npx skills`.
        self.assertFalse((REPO / "SKILL.md").exists())

    def test_every_skill_dir_names_itself(self):
        dirs = sorted(p for p in SKILLS.iterdir() if p.is_dir())
        self.assertIn(SKILLS / "rip-swarm", dirs)
        for d in dirs:
            fm = frontmatter(d / "SKILL.md")
            self.assertEqual(fm["name"], d.name)
            self.assertTrue(fm["description"])

    def test_version(self):
        from rip_swarm import __version__
        self.assertEqual(__version__, "0.3.0")

    def test_scripts_run_from_a_copy_outside_the_repo(self):
        from rip_swarm import __version__
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "installed" / "rip-swarm"
            shutil.copytree(
                SKILLS / "rip-swarm", copy, ignore=shutil.ignore_patterns("__pycache__")
            )
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            run = subprocess.run(
                [sys.executable, str(copy / "scripts" / "version.py")],
                cwd=tmp, env=env, capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), f"rip-swarm {__version__}")


    ROLES = {"swarm-master": "<goal>", "swarm-worker": "leave"}

    def _text(self, name):
        return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")

    def test_role_skills_are_operator_invoked_only(self):
        for name, hint in self.ROLES.items():
            fm = frontmatter(SKILLS / name / "SKILL.md")
            self.assertEqual(fm["disable-model-invocation"], "true", name)
            self.assertIn(hint, fm["argument-hint"], name)

    def test_role_skills_run_wait_in_the_background_on_both_harnesses(self):
        for name in self.ROLES:
            text = self._text(name)
            for needle in ("run_in_background: true", "background: true",
                           "get_command_or_subagent_output", "wake ", "Exit 3"):
                self.assertIn(needle, text, f"{name} lacks {needle!r}")

    def test_role_skills_find_the_runtime(self):
        for name in self.ROLES:
            text = self._text(name)
            for needle in ("../rip-swarm", "~/.agents/skills/rip-swarm",
                           "npx skills add esjaysmith/rip-swarm"):
                self.assertIn(needle, text, f"{name} lacks {needle!r}")

    def test_every_messages_new_names_its_reader(self):
        for path in SKILLS.glob("*/SKILL.md"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "--new" in line:
                    self.assertIn("--to", line, f"{path}: {line}")

    def test_role_skill_git_commands_are_pinned_to_the_worktree(self):
        import re
        verbs = re.compile(r"\bgit (switch|merge|branch|rev-parse|rev-list|show-ref|status|commit"
                           r"|add|reset|update-ref)\b")
        for name in self.ROLES:
            for line in self._text(name).splitlines():
                if verbs.search(line):
                    self.assertIn('git -C "$WORKTREE"', line, f"{name}: {line}")

    def test_master_review_verifies_parents_quietly(self):
        text = self._text("swarm-master")
        self.assertIn('rev-parse -q --verify "$REVIEW^2" || true', text)
        self.assertIn('SHA=$(git -C "$WORKTREE" rev-parse -q --verify "$SHORT^{commit}")', text)

    def test_review_state_never_crosses_a_command(self):
        # Each harness command is a fresh shell: a fence may only read what it assigns.
        import re
        for name in self.ROLES:
            text = self._text(name)
            self.assertIn("fresh shell", text, name)
            for block in re.findall(r"```bash\n(.*?)```", text, re.S):
                for var in ("REVIEW", "TIP", "SHA", "SHORT", "T", "WORKTREE",
                            "RS", "HIVE", "AGENT", "BRANCH", "BUILD_ON", "KEPT"):
                    if re.search(rf"\${var}\b", block):
                        self.assertRegex(block, rf"(^|[\s;]){var}=", f"{name}: ${var} unassigned in\n{block}")
        master = self._text("swarm-master")
        review = next(b for b in re.findall(r"```bash\n(.*?)```", master, re.S)
                      if 'switch -c "$REVIEW"' in b)
        for needle in ('SHA=$(git -C "$WORKTREE" rev-parse -q --verify "$SHORT^{commit}")',
                       "TIP=$(", 'REVIEW="rip-swarm/review-$T"', 'echo "OUTCOME=',
                       "OUTCOME=badsha", "OUTCOME=dirty", "OUTCOME=error",
                       'switch -c "$REVIEW" "$TIP" &&'):
            self.assertIn(needle, review)

    SHELL_VARS = ("RS", "HIVE", "AGENT", "WORKTREE", "BRANCH")

    def test_inline_commands_assign_what_they_read(self):
        # An inline `…` command is a fresh shell too: `RS=` unset makes python3 exit 2,
        # which reads like a refusal. A bare `$VAR` span names a variable; it is no command.
        for name in self.ROLES:
            prose = re.sub(r"```bash\n.*?```", "", self._text(name), flags=re.S)
            for span in re.findall(r"`([^`\n]+)`", prose):
                if re.fullmatch(r"\$\w+", span):
                    continue
                for var in self.SHELL_VARS:
                    if re.search(rf"\${var}\b", span):
                        self.assertRegex(span, rf"(^|[\s;]){var}=",
                                         f"{name}: ${var} unassigned in `{span}`")

    def test_worker_step_one_starts_from_integration(self):
        text = self._text("swarm-worker")
        block = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if "BUILD_ON" in b)
        for needle in ('rev-list --count rip-swarm/integration..HEAD',
                       'update-ref "$KEPT" HEAD',
                       'reset -q --hard rip-swarm/integration',
                       'merge --no-edit "$BUILD_ON"',
                       'echo "SYNC=$SYNC BUILD=$BUILD KEPT=$KEPT"'):
            self.assertIn(needle, block)
        # The BUILD_ON merge is not gated on a failed integration merge (spec §8 step 4).
        self.assertNotIn('"$SYNC" = failed', block)

    def test_master_failed_resume_switch_is_an_error(self):
        text = self._text("swarm-master")
        review = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S)
                      if 'switch -c "$REVIEW"' in b)
        self.assertIn("RESUMED=failed", review)
        self.assertRegex(review, r'elif \[ "\$RESUMED" = failed \]; then\s*\n\s*git -C "\$WORKTREE" switch rip-swarm/integration\s*\n\s*OUTCOME=error')

    def test_role_skills_route_exit_2_mid_work(self):
        worker = self._text("swarm-worker")
        self.assertIn("**Exit 2 from `heartbeat` or `complete`**", worker)
        self.assertIn("`claim expired`", worker)
        master = self._text("swarm-master")
        self.assertIn("**Exit 2 from the heartbeat or from `accept.py`**", master)
        self.assertIn("does not hold a live orchestrator baton", master)

    def test_fresh_shell_rule_comes_before_the_first_command(self):
        for name in self.ROLES:
            text = self._text(name)
            self.assertLess(text.index("fresh shell"), text.index("```bash"), name)
            self.assertIn('RS=<RS>; python3 "$RS/scripts/join.py"', text, name)

    def test_master_pass_block_detects_a_dirty_tree(self):
        text = self._text("swarm-master")
        block = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S)
                     if "--ff-only" in b)
        self.assertIn("status --porcelain", block)
        self.assertIn("DIRTY", block)
        self.assertRegex(block, r'switch rip-swarm/integration &&\s*\\?\s*git -C "\$WORKTREE" merge --ff-only')

    def test_worker_commits_stage_everything_first(self):
        # A bare commit leaves untracked and unstaged work out of the result,
        # and `complete` would then record the pre-task sha.
        text = self._text("swarm-worker")
        commits = re.findall(r'git -C "\$WORKTREE" commit\b[^`\n]*', text)
        self.assertGreaterEqual(len(commits), 3, commits)
        for line in text.splitlines():
            for match in re.finditer(r'git -C "\$WORKTREE" commit\b', line):
                prefix = line[:match.end()]
                self.assertRegex(prefix, r'git -C "\$WORKTREE" add -A && git -C "\$WORKTREE" commit$',
                                 f"commit without add -A: {line}")
        self.assertIn('git -C "$WORKTREE" add -A && git -C "$WORKTREE" commit -m "<id>: <headline>"', text)
        # A merge resolution never opens an editor.
        self.assertIn('git -C "$WORKTREE" add -A && git -C "$WORKTREE" commit --no-edit', text)

    def test_worker_merges_integration_only_when_it_exists(self):
        text = self._text("swarm-worker")
        self.assertIn('show-ref --verify --quiet refs/heads/rip-swarm/integration', text)

    def test_worker_never_sends_its_brief(self):
        self.assertIn('--body "joined"', self._text("swarm-worker"))

    def test_readme_documents_install_and_roles(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        for needle in ("npx skills add esjaysmith/rip-swarm -g -a claude-code -a grok -s '*' -y",
                       "npx skills update rip-swarm swarm-master swarm-worker -g",
                       "/swarm-master", "/swarm-worker",
                       "PYTHONPATH=skills/rip-swarm python3 -m unittest discover -s tests"):
            self.assertIn(needle, text)

    def test_protocol_template_names_members_and_after(self):
        text = (SKILLS / "rip-swarm" / "templates" / "_swarm" / "PROTOCOL.md").read_text(encoding="utf-8")
        for needle in ("agents/<id>/member.json", "accepted/<task_id>.json", "`after`", "/swarm-master"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
