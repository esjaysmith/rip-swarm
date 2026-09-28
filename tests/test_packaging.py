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
        self.assertIn("`<X> is rejected; it cannot be accepted`", master)   # ends the fixes walk only

    def test_master_reject_handler_is_guarded_by_the_board(self):
        # The wake recurs, and a cascade's own tombstones wake the master too:
        # every step must be safe to run again from the board alone.
        text = self._text("swarm-master")
        handler = text[text.index("### `wake task-finished <T> reject`"):text.index("### Other wakes")]
        for needle in ("Read `note` from `<HIVE>/claims/<T>.reject.*.json`",
                       "If it is `dependency <id> rejected`",
                       "`<HIVE>/accepted/<X>.json`", "`<HIVE>/claims/<X>.reject.*.json`",
                       "`<title> (replaces <T>)`", "`<its title> (replaces <id>)`",
                       "HIVE=<HIVE>; grep -lE '\"title\": \".* \\(replaces <id>\\)\",?$' \"$HIVE\"/inbox/task_*.json",
                       "its `--fixes`", "first swap each replaced task for its replacement, then leave out `<T>`",
                       "Use the first file it prints whose task is not rejected",
                       "or only such dead tasks, post a new one",
                       "If a run of this handler changes nothing",
                       '--task <T> --agent "$AGENT" --cascade',
                       "`skipped <id> held by <agent> until <expires_at>`"):
            self.assertIn(needle, handler)
        # Replacements come first: before the dependents are rejected, while the wake
        # still recurs, and before the orphan step, which a replacement's `--fixes` settles.
        self.assertLess(handler.index("(replaces <T>)"), handler.index("**Orphaned original.**"))
        self.assertLess(handler.index("(replaces <T>)"), handler.index('--task <T> --agent "$AGENT" --cascade'))

    def test_heartbeat_loop_replaces_the_fifteen_minutes(self):
        # Execution proposals §3: the lease comes from the profile, not the prose.
        for name, task in (("swarm-master", "orchestrator"), ("swarm-worker", "<id>")):
            text = self._text(name)
            self.assertNotIn("15 minutes", text, name)
            self.assertIn(f'heartbeat --hive "$HIVE" --task {task} --agent "$AGENT" --loop', text, name)
            self.assertIn("`--stop` in place of `--loop`", text, name)
            self.assertIn("Its end is not a wake.", text, name)
        master = self._text("swarm-master")
        self.assertIn("Stop the heartbeat loop", master)
        self.assertLess(master.index("start the heartbeat loop"), master.index("OUTCOME=$OUTCOME"))

    def test_complete_is_the_handoff(self):
        # Execution proposals §4: no result message after complete.
        worker = self._text("swarm-worker")
        self.assertNotIn("--type result", worker)
        self.assertIn("`complete` is the handoff", worker)

    def test_reference_names_the_new_flags(self):
        ref = (SKILLS / "rip-swarm" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("--cascade", "--loop", "--stop"):
            self.assertIn(needle, ref)

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

    def test_worker_step_one_runs_once_per_task(self):
        # Re-running it mid-task resets the branch (the task's commits go to
        # refs/rip-swarm/prev) or commits conflict markers via SYNC=dirty.
        text = self._text("swarm-worker")
        self.assertIn("Run this block **exactly once** per task, right after the claim succeeds", text)
        self.assertIn("never after you have started or committed work for this task", text)

    def test_worker_merges_integration_only_when_it_exists(self):
        text = self._text("swarm-worker")
        self.assertIn('show-ref --verify --quiet refs/heads/rip-swarm/integration', text)

    def test_worker_never_sends_its_brief(self):
        self.assertIn('--body "joined"', self._text("swarm-worker"))

    def _section(self, text, start, end):
        return text[text.index(start):text.index(end)]

    def test_master_marks_artifacts_with_the_larger_number(self):
        text = self._text("swarm-master")
        plan = self._section(text, "## 4. Plan and post", "## 5.")
        for needle in ("reviews: <kind>=<N>", "the larger of the phrase's number and the profile's",
                       "--kind <kind>", "--min-reviews <N>",
                       "`min_reviews for <kind> is at least <n> (profile)`",
                       "without `--kind`"):
            self.assertIn(needle, plan)

    def test_master_runs_reviews_py_on_every_chain_wake(self):
        text = self._text("swarm-master")
        complete = self._section(text, "### `wake task-finished <T> complete`", "### Review chains")
        self.assertIn("**Review chains first.**", complete)
        self.assertLess(complete.index("**Review chains first.**"), complete.index("1. If `<HIVE>/accepted/<T>.json`"))
        chains = self._section(text, "### Review chains", "### `wake task-finished <T> release`")
        for needle in ('RS=<RS>; HIVE=<HIVE>; python3 "$RS/scripts/reviews.py" --hive "$HIVE" --task <T>',
                       "NEXT=<post-review|post-revise|post-rebase|reject-review|merge|wait|done>",
                       "**`post-rebase` or `reject-review`** first",
                       "read the head first when it is a review",
                       '--note "review rejected: <why>"',
                       "--reviews <A>", "Revise <title> after review <k>",
                       "In every skill command below, `<T>` is `<A>`",
                       "again **before** the fast-forward",
                       "same `HEAD=` and `SHA=`",
                       "accept each id of `CHAIN`, in order, then `A`",
                       "message the author of `A`",
                       "post the replacement first",
                       "reject **`A`**, not the woken review",
                       "reject that review, not `A`",
                       "Merge <SHA> onto rip-swarm/integration and resolve the conflict; the resolution is the work.",
                       '--note "superseded by rebase <id>"'):
            self.assertIn(needle, chains)
        release = self._section(text, "### `wake task-finished <T> release`", "### `wake task-finished <T> reject`")
        self.assertIn("*Review chains*", release)
        reject = self._section(text, "### `wake task-finished <T> reject`", "### Other wakes")
        for needle in ("**`<T>` is a chain task**", "skip steps 2 and 3", "**`<T>` is a reviewed artifact**",
                       "**`<T>` is a chain task and `<A>` is rejected**: run step 4 only",
                       "`skipped <id> (chain of <T>) held by", "Skip step 2 when `<T>` has a chain",
                       "its `--kind` and `--min-reviews`"):
            self.assertIn(needle, reject)
        other = self._section(text, "### Other wakes", "## 7.")
        self.assertIn("when `NEXT` is `wait` or `done`", other)

    def test_worker_reviews_with_a_verdict_and_never_resolves(self):
        text = self._text("swarm-worker")
        review = self._section(text, "**Review tasks.**", "Never push a project branch")
        for needle in ("--verdict clean", "--verdict findings",
                       'WORKTREE=<WORKTREE>; if git -C "$WORKTREE" rev-parse -q --verify MERGE_HEAD >/dev/null; then git -C "$WORKTREE" merge --abort; fi',
                       '--note "review <id> cannot build on <sha>: conflict"',
                       "never `HEAD`", "`SYNC=error`, `BUILD=conflict` or `BUILD=error`",
                       "Edit nothing else.", "`<agent> wrote part of <A>`",
                       "`<agent> reviewed <A>`", "`<A> is rejected`"):
            self.assertIn(needle, review)

    def test_reference_names_the_review_surface(self):
        ref = (SKILLS / "rip-swarm" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("--kind", "--min-reviews", "--reviews", "--verdict clean|findings",
                       'scripts/reviews.py" --hive "$HIVE" --task ID', "(chain of <A>)"):
            self.assertIn(needle, ref)

    def test_worker_review_pointer_precedes_step_one(self):
        # Fix round 1, F1: steps 1-6 and the BUILD=conflict bullet apply to
        # ordinary tasks; a review task is routed away before step 1 runs.
        text = self._text("swarm-worker")
        section = self._section(text, "## 5. Do a claimed task", "## 6. Leave")
        pointer = 'follow **Review tasks** below instead of steps 1–6'
        for needle in (pointer, "never in a review task: see **Review tasks**"):
            self.assertIn(needle, section)
        self.assertLess(section.index(pointer),
                         section.index("1. Start the task from `rip-swarm/integration`"))

    def test_worker_review_runs_only_the_start_block(self):
        # Grok review 1, M1: step 1's error bullet releases with a note the
        # master never reads as a review that cannot build, so a review runs
        # step 1's command block only, then its own step 2.
        text = self._text("swarm-worker")
        section = self._section(text, "## 5. Do a claimed task", "## 6. Leave")
        error_bullet = next(line for line in section.splitlines()
                            if line.lstrip().startswith("- `SYNC=error` or `BUILD=error`"))
        self.assertIn("(never in a review task: see **Review tasks**)", error_bullet)
        review = self._section(text, "**Review tasks.**", "Never push a project branch")
        step_1 = self._section(review, "1. Run only step 1's command block", "2. If that does not merge")
        for needle in ("`BUILD_ON=` the sha the body names", "step 1's `SYNC=dirty` bullet",
                       "go to step 2 below, never to step 1's other bullets"):
            self.assertIn(needle, step_1)

    def test_worker_review_heartbeats_until_it_releases_or_completes(self):
        # Grok review 1, M3: a review outlasting worker_lease_ttl lost its claim.
        text = self._text("swarm-worker")
        review = self._section(text, "**Review tasks.**", "Never push a project branch")
        loop = ('RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat '
                '--hive "$HIVE" --task <id> --agent "$AGENT" --loop')
        for needle in (loop, "`--stop` in place of `--loop`", "before you `release` or `complete`",
                       "Stop the heartbeat loop, commit it as step 4 does"):
            self.assertIn(needle, review)
        self.assertLess(review.index(loop), review.index("Read the artifact at that sha"))
        self.assertLess(review.index("Stop the heartbeat loop, commit"), review.index("--verdict clean"))

    def test_master_checks_the_sha_before_posting_a_chain_task(self):
        # Grok review 1, m2: a head whose result_ref is not a commit never took
        # the badsha path; the chain posted reviews and rebases on it forever.
        text = self._text("swarm-master")
        chains = self._section(text, "### Review chains", "### `wake task-finished <T> release`")
        check = 'WORKTREE=<WORKTREE>; git -C "$WORKTREE" rev-parse -q --verify "<SHA>^{commit}"'
        post_review = self._section(chains, "4. **`post-review`**", "5. **`post-revise`**")
        for needle in (check, '--task <HEAD> --agent "$AGENT" --note "result_ref <result_ref> is not a commit"',
                       "`A`'s author when the head is `A`"):
            self.assertIn(needle, post_review)
        self.assertLess(post_review.index(check), post_review.index("--reviews <A>"))
        rebase = self._section(chains, "- `post-rebase`:", "- `reject-review`:")
        for needle in ("*post-review*'s command",
                       '--note "review <REVIEW> cannot build: <SHA> is not a commit"',
                       "then run the helper again and act on the new line"):
            self.assertIn(needle, rebase)
        self.assertLess(rebase.index("*post-review*'s command"), rebase.index("--title") if "--title" in rebase
                        else rebase.index("Otherwise post a rebase"))
        arm_2 = self._section(chains, "2. For `post-review`", "3. **`done` or `wait`**")
        self.assertIn("If `git show` fails because `SHA` is not a commit", arm_2)
        self.assertIn("that is `merge` item 8's case", arm_2)

    def test_master_checks_the_note_sha_against_the_review_body(self):
        # Final review m4: a note naming another sha than the review's body
        # is a worker's slip; the master reports it before posting a rebase.
        text = self._text("swarm-master")
        chains = self._section(text, "### Review chains", "### `wake task-finished <T> release`")
        rebase = self._section(chains, "- `post-rebase`:", "- `reject-review`:")
        grep = '''HIVE=<HIVE>; grep -oE 'Build on [0-9a-f]+' "$HIVE/inbox/<REVIEW>.json"'''
        for needle in (grep, "report both shas to the operator and stop, and post nothing"):
            self.assertIn(needle, rebase)
        self.assertLess(rebase.index(grep), rebase.index("*post-review*'s command"))
        self.assertLess(rebase.index(grep), rebase.index("Otherwise post a rebase"))

    def test_master_waits_when_a_released_review_is_claimed_again(self):
        # Final review m5: exit 2 from rejecting REVIEW, because a worker
        # claimed it again, is not a failure; its next wake runs the helper.
        text = self._text("swarm-master")
        chains = self._section(text, "### Review chains", "### `wake task-finished <T> release`")
        for needle in ("An exit 2 from rejecting `REVIEW` in these arms that says "
                       "`<REVIEW> is held by <agent>`, or `lost race on remote tip`, is not a failure",
                       "Go back to wait. The next `release` or `complete` wake of `REVIEW` runs the helper again",
                       "Any other exit 2 is a failure: report it to the operator and stop."):
            self.assertIn(needle, chains)

    def test_master_merge_arm_cleans_up_on_dirty_at_the_fast_forward(self):
        # Final review m6: item 3 runs *Passes* item 1; DIRTY: there is cleaned
        # up and the command run again, as step 9 says.
        text = self._text("swarm-master")
        merge = self._section(text, "6. **`merge`.**", "7. **`OUTCOME=dirty`**")
        self.assertIn("On `DIRTY:`, remove your acceptance check's leftovers and run the command "
                      "again, as step 9 says.", merge)

    def test_master_merge_arm_resyncs_before_comparing(self):
        # Fix round 1, F2: reviews.py reads the local clone, which only a
        # heartbeat (one-shot or the loop's own publishes) fetches and
        # fast-forwards, so the clone must be resynced before the compare.
        text = self._text("swarm-master")
        merge = self._section(text, "6. **`merge`.**", "7. **`OUTCOME=dirty`**")
        stop_then_beat = "stop the heartbeat loop, then heartbeat once"
        beat_cmd = 'python3 "$RS/scripts/claim.py" heartbeat --hive "$HIVE" --task orchestrator --agent "$AGENT"'
        rerun = "run the helper with `--task <A>` again **before** the fast-forward"
        item3 = "Run the *Passes* item 1 command with `T=<A>` and the `TIP` of the `OUTCOME=` line; the heartbeat loop is already stopped"
        for needle in (stop_then_beat, beat_cmd, rerun, item3,
                       "only this command or the loop's own publishes fetch and fast-forward it",
                       "exit 2 means the baton is gone, as section 2 says"):
            self.assertIn(needle, merge)
        # Item 1 heartbeats too (Grok review 2, m1): look for item 2's after it.
        beat_at = merge.index(beat_cmd, merge.index(stop_then_beat))
        self.assertLess(beat_at, merge.index(rerun))
        self.assertLess(merge.index(rerun), merge.index(item3))

    def test_master_merge_arm_restarts_on_moved(self):
        # Fix round 1, F4: MOVED from the fast-forward command must restart
        # this arm at item 1, not fall through to plain step 9's own advice.
        text = self._text("swarm-master")
        merge = self._section(text, "6. **`merge`.**", "7. **`OUTCOME=dirty`**")
        self.assertIn("On `MOVED:`, restart this merge arm from item 1", merge)
        self.assertIn("leaving `CHAIN` unaccepted forever", merge)

    def test_master_merge_arm_replaces_the_whole_chain_before_rejecting(self):
        # Fix round 1, F3: "Not worth pursuing" must run the reject handler's
        # step 2 for A and its still-wanted dependents before A is rejected,
        # since the reject handler itself skips step 2 once A has a chain.
        text = self._text("swarm-master")
        merge = self._section(text, "6. **`merge`.**", "7. **`OUTCOME=dirty`**")
        run_step_2 = "run the reject handler's step 2 in full, with `<T>` as `A`, before rejecting it"
        dependents = "post each of `A`'s dependents that is not rejected yet and is still wanted"
        reject_a = 'python3 "$RS/scripts/claim.py" reject --hive "$HIVE" --task <A> --agent "$AGENT" --note "<why>"'
        for needle in (run_step_2, dependents, reject_a,
                       "because `A` now has a chain, skips step 2 itself"):
            self.assertIn(needle, merge)
        self.assertLess(merge.index(run_step_2), merge.index(reject_a))
        self.assertLess(merge.index(dependents), merge.index(reject_a))

    def test_master_replacement_refused_below_the_floor_takes_the_profile(self):
        # Controller ruling (Task 10): inbox-add's exact-copy exception needs the
        # original rejected, but step 2 posts a dependent's replacement, and
        # merge item 6 A's, before that reject; a floor refusal falls back to
        # `--kind` alone, as section 4 does for a plan post.
        text = self._text("swarm-master")
        reject = self._section(text, "### `wake task-finished <T> reject`", "### Other wakes")
        step_2 = self._section(reject, "2. **Replacements**", "3. **Orphaned original.**")
        for needle in ("a replacement posted before its original is rejected",
                       "`A` under *Review chains*, `merge` item 6",
                       "that is refused with `min_reviews for <kind> is at least <n> (profile)` "
                       "is posted again with `--kind <kind>` alone, which takes the profile's number"):
            self.assertIn(needle, step_2)
        self.assertLess(step_2.index("its `--kind` and `--min-reviews`"),
                        step_2.index("is posted again with `--kind <kind>` alone"))

    def test_master_merge_arm_stops_the_loop_on_every_path(self):
        # Fix round 1, F5: every hive write in the merge arm is preceded by
        # stopping the heartbeat loop. Grok review 3, M1: so is every report
        # and stop, and every item names the stop itself.
        text = self._text("swarm-master")
        merge = self._section(text, "6. **`merge`.**", "7. **`OUTCOME=dirty`**")
        self.assertIn("Stop the heartbeat loop on every path of this arm, before its next hive write "
                      "and before you report and stop or go back to section 5", merge)
        stop = ('RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat '
                '--hive "$HIVE" --task orchestrator --agent "$AGENT" --stop')
        self.assertIn(stop, merge)
        for start, end in (("   4. **`OUTCOME=conflict`**", "   5. **Falls short**"),
                           ("   8. **`OUTCOME=badsha`**", "**A review that cannot build.**")):
            item = self._section(text, start, end)
            self.assertIn("stop the heartbeat loop", item.lower())
        item_7 = self._section(text, "7. **`OUTCOME=dirty`**", "8. **`OUTCOME=badsha`**")
        foreign = self._section(item_7, "Otherwise", "**`OUTCOME=error`**")
        error = item_7[item_7.index("**`OUTCOME=error`**"):]
        for sentence, report in ((foreign, "report them to the operator and stop"),
                                 (error, "as step 8, with `T=<A>`")):
            self.assertIn("stop the heartbeat loop (`--stop`) until it exits 0", sentence)
            self.assertLess(sentence.index("`--stop`"), sentence.index(report))

    def test_master_stops_the_loop_on_moved_before_step_4_again(self):
        # Grok review 3, audit: step 4 opens with the one-shot heartbeat,
        # which section 2 forbids while the loop runs.
        text = self._text("swarm-master")
        step_4 = self._section(text, "**Stop the heartbeat loop** as soon as", "5. **`OUTCOME=conflict`.**")
        self.assertIn("on `MOVED:`, before you run step 4 again", step_4)

    def test_master_merge_arm_restarts_on_its_own_dirt(self):
        # Grok review 2, M2: step 7's "run step 4 again" leads to ordinary
        # step 9, which accepts A alone and leaves CHAIN unaccepted forever.
        text = self._text("swarm-master")
        item_7 = self._section(text, "7. **`OUTCOME=dirty`**", "8. **`OUTCOME=badsha`**")
        for needle in ("stop the heartbeat loop, remove them and restart this merge arm from item 1",
                       "as item 3 does on `MOVED:`",
                       "Never follow step 7's own \"run step 4 again\"",
                       "then report them to the operator and stop, as step 7 does",
                       "then as step 8, with `T=<A>`"):
            self.assertIn(needle, item_7)

    def test_master_merge_arm_starts_the_loop_before_the_block(self):
        # Grok review 2, m1: the loop start is a sentence above step 4's script,
        # so "run step 4 (the OUTCOME= block)" left the check without a loop.
        text = self._text("swarm-master")
        item_1 = self._section(text, "   1. Heartbeat", "   2. **Passes:**")
        beat = ('RS=<RS>; HIVE=<HIVE>; AGENT=<AGENT>; python3 "$RS/scripts/claim.py" heartbeat '
                '--hive "$HIVE" --task orchestrator --agent "$AGENT"')
        for needle in (beat, "then start the heartbeat loop in the background (section 2)",
                       "then run step 4's `OUTCOME=` block with `T=<A>` and `SHORT=<SHA>`",
                       "The loop covers the block and the acceptance check"):
            self.assertIn(needle, item_1)
        self.assertLess(item_1.index(beat), item_1.index("then start the heartbeat loop"))
        self.assertLess(item_1.index("then start the heartbeat loop"), item_1.index("`OUTCOME=` block"))
        merge = self._section(text, "6. **`merge`.**", "7. **`OUTCOME=dirty`**")
        self.assertIn("   1. Heartbeat", merge)

    def test_role_skills_wait_for_a_stop_that_timed_out(self):
        # Grok review 2, m2: exit 1 from `--stop` means the loop may still be
        # publishing; the next hive write must wait for a later `--stop` exit 0.
        rule = ("Exit 1 from `--stop` means the loop has not exited within 60 seconds and may still "
                "be publishing: run `--stop` again, and make no hive write until it exits 0.")
        master = self._text("swarm-master")
        self.assertIn(rule, self._section(master, "**The heartbeat loop**", "**Exit 2 from the heartbeat"))
        worker = self._text("swarm-worker")
        self.assertIn(rule, self._section(worker, "3. Heartbeat before each long step", "4. If `WORKTREE="))
        review = self._section(worker, "**Review tasks.**", "Never push a project branch")
        self.assertIn(rule, self._section(review, "3. Start the heartbeat loop", "4. Read the artifact"))

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

    def test_docs_name_the_execution_proposals(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("`reviews.py`", readme)
        self.assertIn("docs/specs/2026-09-26-execution-proposals.md", readme)
        protocol = (SKILLS / "rip-swarm" / "templates" / "_swarm" / "PROTOCOL.md").read_text(encoding="utf-8")
        for needle in ("min_reviews", "reject --cascade", "--verdict"):
            self.assertIn(needle, protocol)


if __name__ == "__main__":
    unittest.main()
