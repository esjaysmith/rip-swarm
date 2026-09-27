# tests/test_rehearsal.py — model-free rehearsal: one master, two workers (spec §11)
import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, git, make_project, remote_show
from rip_swarm.board import artifact_of, chain, downstream, fixers, posting_order, read_board
from rip_swarm.cli import main
from rip_swarm.gitops import publish, sync
from rip_swarm.join import join, leave
from rip_swarm.state import load_state, save_state
from rip_swarm.waiter import Wake, tick

LATER = T0 + timedelta(minutes=31)   # past the 30m baton and worker leases


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def cli(*argv, at=T0, err=None):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO() if err is None else err), \
            mock.patch("rip_swarm.cli.now_utc", return_value=at):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue().strip()


class Session:
    """What a role skill does between wakes, minus the model."""

    def __init__(self, result, now=T0):
        self.agent, self.hive, self.wt, self.now = result.agent, result.hive, result.worktree, now

    def tick(self):
        sync(self.hive)
        state = load_state(self.hive, self.agent)
        wake = tick(self.hive, self.agent, state, self.now, idle_after=600)
        save_state(self.hive, state)
        return wake

    def post(self, title, *extra):
        rc, out, err = self.try_post(title, *extra)
        assert rc == 0, err or out
        return out.split()[1].rstrip(":")

    def try_post(self, title, *extra):
        """`inbox-add` as the skills run it: (exit code, stdout, stderr)."""
        # Real posts and messages are seconds apart. On a frozen clock they would
        # share the ULID's millisecond, and its random part would pick the order:
        # the chain's posting order (created_at, then id: §5.3), or whether a
        # message sorts after the reader's cursor. So each one moves the clock.
        self.now += timedelta(milliseconds=1)
        err = io.StringIO()
        rc, out = cli("inbox-add", "--hive", self.hive, "--created-by", self.agent,
                      "--title", title, *extra, at=self.now, err=err)
        return rc, out, err.getvalue()

    def send(self, to, body):
        self.now += timedelta(milliseconds=1)                                      # see `post`
        rc, out = cli("message", "--hive", self.hive, "--from", self.agent, "--to", to,
                      "--type", "note", "--body", body, at=self.now)
        assert rc == 0, out

    def claim(self, task):
        return cli("claim", "--hive", self.hive, "--task", task, "--agent", self.agent, at=self.now)[0]

    def start_task(self, build_on=""):
        """/swarm-worker §5 step 1, line for line: (SYNC, BUILD, KEPT)."""
        wt, kept, build = self.wt, "none", "none"
        if git(wt, "status", "--porcelain"):
            sync_ = "dirty"                                                   # nothing touched
        elif run(wt, "show-ref", "--verify", "--quiet", "refs/heads/rip-swarm/integration").returncode:
            sync_ = "none"
        elif git(wt, "rev-list", "--count", "rip-swarm/integration..HEAD") == "0":
            sync_ = "merged" if run(wt, "merge", "--no-edit", "rip-swarm/integration").returncode == 0 else "error"
        else:
            kept = f"refs/rip-swarm/prev/{self.agent}/{git(wt, 'rev-parse', '--short', 'HEAD')}"
            ok = (run(wt, "update-ref", kept, "HEAD").returncode == 0
                  and run(wt, "reset", "-q", "--hard", "rip-swarm/integration").returncode == 0)
            sync_ = "reset" if ok else "error"
        if build_on and sync_ not in ("dirty", "error"):
            if run(wt, "merge", "--no-edit", build_on).returncode == 0:
                build = "merged"
            elif run(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
                build = "conflict"
            else:
                build = "error"
        return sync_, build, kept

    def work(self, name, text, msg):
        """Step 2's edit, then step 4's `add -A && commit -m`, line for line."""
        (self.wt / name).write_text(text, encoding="utf-8")                # may be untracked
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", msg)
        return git(self.wt, "rev-parse", "HEAD")

    def complete(self, task, *extra):
        sha = git(self.wt, "rev-parse", "--short", "HEAD")      # what /swarm-worker records
        rc, out = cli("complete", "--hive", self.hive, "--task", task, "--agent", self.agent,
                      "--result-ref", f"rip-swarm/{self.agent}@{sha}", *extra, at=self.now)
        assert rc == 0, out
        return sha

    def body_sha(self, task):
        """The sha a review, revise, rebase or follow-up body names to build on."""
        sync(self.hive)
        body = json.loads((self.hive / "inbox" / f"{task}.json").read_text(encoding="utf-8"))["body"]
        return re.search(r"\b[0-9a-f]{7,40}\b", body).group(0)

    def review(self, task, verdict, text, *, message=True, result_ref=None):
        """/swarm-worker *Review tasks*, step by step. Returns the short sha, or
        "refused" (claim exit 2), or "released" (the build did not merge cleanly)."""
        rc = self.claim(task)
        if rc == 2:
            return "refused"
        assert rc == 0, rc                                                         # any other exit is an error
        sha = self.body_sha(task)
        sync_, build, _ = self.start_task(sha)                                     # step 1
        assert sync_ != "dirty", sync_                                             # never goes on after dirty
        if sync_ == "error" or build in ("conflict", "error"):                     # step 2
            if run(self.wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
                git(self.wt, "merge", "--abort")                                   # 2.1
            note = f"review {task} cannot build on {sha}: conflict"                # the body's sha
            rc, out = cli("release", "--hive", self.hive, "--task", task, "--agent", self.agent,
                          "--note", note, at=self.now)                             # 2.2
            assert rc == 0, out
            if message:                                                            # 2.3
                self.send("orchestrator", note)
            return "released"
        doc = self.wt / "spec.md"                                                  # steps 3-4
        doc.write_text(doc.read_text(encoding="utf-8") + text, encoding="utf-8")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", f"{task}: review")
        if result_ref is not None:                                                 # a slip, for badsha
            rc, out = cli("complete", "--hive", self.hive, "--task", task, "--agent", self.agent,
                          "--result-ref", result_ref, "--verdict", verdict, at=self.now)
            assert rc == 0, out
            return result_ref
        return self.complete(task, "--verdict", verdict)                          # step 5

    def fold(self, task, text):
        """A revise, rebase or follow-up of a reviewed artifact: /swarm-worker §5
        with BUILD_ON from the body. A conflict is the work: `text` resolves it."""
        rc = self.claim(task)
        if rc == 2:
            return "refused"
        assert rc == 0, rc                                                         # any other exit is an error
        sync_, build, _ = self.start_task(self.body_sha(task))
        assert sync_ in ("merged", "reset", "none") and build in ("merged", "conflict"), (sync_, build)
        (self.wt / "spec.md").write_text(text, encoding="utf-8")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", f"{task}: fold")
        return self.complete(task)


class Master(Session):
    def __init__(self, result, now=T0):
        super().__init__(result, now)
        self.loop = []                  # section 2's heartbeat loop, recorded: "start", "stop"

    def loop_start(self):
        """`heartbeat --loop` in the background. Recorded, not run: the clock is
        frozen, so the lease never runs short. A second start while one runs
        would be exit 3; `loop_closed` catches it as two starts in a row."""
        self.loop.append("start")

    def loop_stop(self):
        """`heartbeat --stop`, until it exits 0 (section 2)."""
        self.loop.append("stop")

    def loop_closed(self):
        """Every start was followed by its stop, and nothing runs now."""
        return self.loop == ["start", "stop"] * (len(self.loop) // 2)

    def result_sha(self, task):
        sync(self.hive)
        stone = next((self.hive / "claims").glob(f"{task}.complete.*.json"))
        return json.loads(stone.read_text(encoding="utf-8"))["result_ref"].split("@", 1)[1]

    def handle_complete(self, task, check, **arms):
        """`wake task-finished <task> complete` per /swarm-master §6."""
        sync(self.hive)
        if self.in_chain(task):                                                    # step 0
            return self.chain_wake(task, check=check, **arms)
        if (self.hive / "accepted" / f"{task}.json").exists():
            return "noop"
        board = read_board(self.hive, self.now)
        if any(not view.settled for view in fixers(board, task)):
            return "skip"
        return self.review(task, self.result_sha(task), check)

    def review(self, task, sha, check):
        """Step 4's block, then step 9's check and Pass/Falls-short commands, per /swarm-master §6.
        The loop starts before the block and stops where step 4's stop sentence says."""
        self.loop_start()
        outcome = self.review_block(task, sha)
        if outcome != "merged":
            self.loop_stop()                                              # any other OUTCOME=
            return outcome
        wt, review = self.wt, f"rip-swarm/review-{task}"
        if not check(wt):
            ok = self._ok("switch", "rip-swarm/integration") and self._ok("branch", "-D", review)
            self.loop_stop()                                              # after Falls short
            return "short" if ok else "failed"
        if git(wt, "status", "--porcelain"):
            return "dirty"                                                # clean, run again: loop runs on
        if git(wt, "rev-parse", "rip-swarm/integration") != self.last_tip:
            self.loop_stop()                                              # MOVED:, before step 4 again
            return "moved"
        ok = (self._ok("switch", "rip-swarm/integration")
              and self._ok("merge", "--ff-only", review)
              and self._ok("branch", "-d", review))
        self.loop_stop()                                                  # NEW_TIP= or FAILED:
        if not ok:
            return "failed"
        new_tip = git(wt, "rev-parse", "HEAD")
        rc, out = cli("accept", "--hive", self.hive, "--agent", self.agent, "--task", task,
                      "--integration-sha", new_tip, at=self.now)
        assert rc == 0, out
        child = task
        while True:
            parent = json.loads((self.hive / "inbox" / f"{child}.json").read_text()).get("fixes")
            if not parent:
                break
            err = io.StringIO()
            rc, out = cli("accept", "--hive", self.hive, "--agent", self.agent, "--task", parent,
                          "--integration-sha", new_tip, "--via", child, at=self.now, err=err)
            if rc == 2 and f"{parent} is rejected; it cannot be accepted" in err.getvalue():
                break                                                     # dropped: the walk stops
            assert rc == 0, out
            if out.startswith("already accepted"):
                break
            child = parent
        return "pass"

    def _ok(self, *args):
        return run(self.wt, *args).returncode == 0

    def review_block(self, task, short):
        """The step 4 block, line for line: merged | conflict | badsha | dirty | error."""
        wt, review = self.wt, f"rip-swarm/review-{task}"
        tip = self.last_tip = git(wt, "rev-parse", "rip-swarm/integration")
        resumed = False
        if self._ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            git(wt, "merge", "--abort")                                   # a crash mid-merge
        resolved = run(wt, "rev-parse", "-q", "--verify", f"{short}^{{commit}}")
        if resolved.returncode != 0:
            return "badsha"                                               # nothing touched
        full = self.last_full = resolved.stdout.strip()                           # the OUTCOME line's SHA=
        if git(wt, "status", "--porcelain"):
            return "dirty"                                                # nothing touched
        if self._ok("show-ref", "--verify", "--quiet", f"refs/heads/{review}"):
            # Empty when the branch is not a merge (e.g. right after the abort): the recreate path.
            p1 = run(wt, "rev-parse", "-q", "--verify", f"{review}^1").stdout.strip()
            p2 = run(wt, "rev-parse", "-q", "--verify", f"{review}^2").stdout.strip()
            if p1 == tip and p2 == full:
                if git(wt, "branch", "--show-current") == review or self._ok("switch", review):
                    resumed = True
                else:
                    resumed = "failed"                                    # the resume switch failed
            else:
                run(wt, "switch", "rip-swarm/integration")
                run(wt, "branch", "-D", review)
        if resumed is True:
            return "merged"
        if resumed == "failed":
            run(wt, "switch", "rip-swarm/integration")
            return "error"
        if self._ok("switch", "-c", review, tip) and self._ok("merge", "--no-ff", "--no-edit", full):
            return "merged"
        if self._ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            run(wt, "merge", "--abort")
            run(wt, "switch", "rip-swarm/integration")
            run(wt, "branch", "-D", review)
            return "conflict"
        run(wt, "switch", "rip-swarm/integration")
        return "error"

    def reject(self, task, note):
        rc, out = cli("reject", "--hive", self.hive, "--agent", self.agent, "--task", task,
                      "--note", note, at=self.now)
        assert rc == 0, out

    def handle_reject(self, task, wanted=(), **arms):
        """`wake task-finished <task> reject` per /swarm-master §6, step by step.

        `wanted` is the model's call: the rejected tasks whose work is still
        wanted. The orphan is always rejected here; the other choice is `review`.
        Every step reads the board first, so a rerun does nothing twice.
        `arms` are the model's calls for *Review chains* (see `chain_wake`).
        Returns what it did."""
        sync(self.hive)
        board = read_board(self.hive, self.now)
        done = []
        stone = next((self.hive / "claims").glob(f"{task}.reject.*.json"))
        note = json.loads(stone.read_text(encoding="utf-8")).get("note") or ""
        root = not re.fullmatch(r"dependency \S+ rejected", note)                 # step 1
        a = artifact_of(board, task)
        chain_task = a is not None and a != task and not board[a].rejected         # chain case 1
        has_chain = board[task].min_reviews >= 1 and bool(chain(board, task))       # chain case 2
        if a is not None and a != task and board[a].rejected:                      # chain case 3:
            root = False                                                           # step 4 only
        if root and not chain_task and not has_chain:                              # step 2
            done += self.replace(task, wanted)
            board = read_board(self.hive, self.now)
        x = board[task].fixes
        if root and not chain_task and x and not board[x].settled and all(         # step 3
                view.settled for view in fixers(board, x) if view.task_id != task):
            self.reject(x, "fix abandoned")
            done.append(f"orphan {x}")
        rc, out = cli("reject", "--hive", self.hive, "--task", task,               # step 4
                      "--agent", self.agent, "--cascade", at=self.now)
        assert rc == 0, out
        lines = out.splitlines()
        done += [f"reject {line.split()[1]}" for line in lines if line.startswith("rejected ")]
        if any(line.startswith("skipped ") and "(chain of " not in line for line in lines):
            return done + ["stop"]                                                 # §2's stop
        if chain_task:
            done += self.chain_wake(task, **arms)
        return done

    def replace(self, task, wanted):
        """The reject handler's step 2, "Replacements": `task`'s replacement, then
        each dependent that is not rejected yet, for those in `wanted`, unless the
        grep finds a usable one. `task` is rejected, or about to be (*Review
        chains* `merge` item 6 runs this before it rejects A). Returns the posts."""
        board = read_board(self.hive, self.now)
        deps = self._downstream(board, task)
        dead = {task, *(tid for tid, _ in downstream(board, task))}              # step 4 rejects these
        replaced, done = {}, []
        for old in [task, *(tid for tid in deps if not board[tid].rejected)]:
            if old not in wanted:
                continue
            found = [r for r in self.replacements(old)
                     if not board[r].rejected and not any(
                         dep in dead or board[dep].rejected for dep in board[r].after)]
            if found:
                replaced[old] = found[0]                                           # the first usable
                continue
            inbox = self.inbox(old)
            after = [replaced.get(dep, dep) for dep in board[old].after            # swap, then drop
                     if dep in replaced or not (dep in dead or board[dep].rejected)]
            extra = [arg for dep in after for arg in ("--after", dep)]
            if inbox.get("body") is not None:
                extra += ["--body", inbox["body"]]
            if inbox.get("fixes"):
                extra += ["--fixes", inbox["fixes"]]
            kind, copy = inbox.get("kind"), []
            if kind is not None:                                                   # copied, whatever
                extra += ["--kind", kind]                                          # the profile says
            if inbox.get("min_reviews") is not None:
                copy = ["--min-reviews", str(inbox["min_reviews"])]
            title = f"{board[old].title} (replaces {old})"
            rc, out, err = self.try_post(title, *extra, *copy)
            if rc != 0 and kind is not None and re.search(
                    rf"min_reviews for {re.escape(kind)} is at least \d+ \(profile\)", err):
                # The exact-copy exception needs `old` rejected; this post came
                # first. Posted again with `--kind` alone: the profile's number.
                assert not board[old].rejected, err
                done.append(f"refused {old}")
                rc, out, err = self.try_post(title, *extra)
            assert rc == 0, err or out
            replaced[old] = out.split()[1].rstrip(":")
            done.append(f"post {replaced[old]}")
        return done

    @staticmethod
    def _downstream(board, task):
        """Every task whose `after` chain leads to `task`, in dependency order."""
        out, frontier = [], [task]
        while frontier:
            nxt = sorted(tid for tid, v in board.items()
                         if tid not in out and any(dep in frontier for dep in v.after))
            out.extend(nxt)
            frontier = nxt
        return out

    def replacements(self, task):
        """Inbox tasks titled `… (replaces <task>)`, per the handler's grep."""
        sync(self.hive)
        return sorted(p.stem for p in (self.hive / "inbox").glob("task_*.json")
                      if json.loads(p.read_text(encoding="utf-8"))["title"]
                      .endswith(f" (replaces {task})"))

    def inbox(self, task):
        return json.loads((self.hive / "inbox" / f"{task}.json").read_text(encoding="utf-8"))

    def completer(self, task):
        """(agent, result_ref) from `claims/<task>.complete.*.json`."""
        stone = next((self.hive / "claims").glob(f"{task}.complete.*.json"))
        doc = json.loads(stone.read_text(encoding="utf-8"))
        return doc["agent"], doc["result_ref"]

    def in_chain(self, task):
        """§6 step 0: min_reviews >= 1, or `reviews`, or `fixes` of such an artifact."""
        doc = self.inbox(task)
        if (doc.get("min_reviews") or 0) >= 1 or doc.get("reviews"):
            return True
        return bool(doc.get("fixes")) and (self.inbox(doc["fixes"]).get("min_reviews") or 0) >= 1

    def is_commit(self, sha):
        """`git -C "$WORKTREE" rev-parse -q --verify "<SHA>^{commit}"` succeeds."""
        return self._ok("rev-parse", "-q", "--verify", f"{sha}^{{commit}}")

    def reject_badsha(self, task):
        """The ordinary badsha reject of `task`, and a message to its worker."""
        worker, ref = self.completer(task)
        self.reject(task, f"result_ref {ref} is not a commit")
        self.send(worker, f"result_ref {ref} is not a commit")
        return [f"reject {task}"]

    def reviews(self, task):
        rc, out = cli("reviews", "--hive", self.hive, "--task", task, at=self.now)
        assert rc == 0, out
        return dict(field.split("=", 1) for field in out.split())

    def chain_wake(self, task, *, check=None, judge=None, worth=True, wanted=(), leftovers=True):
        """/swarm-master *Review chains*: run reviews.py and act on NEXT, arm by arm.

        The model's calls: `check(wt)` is A's acceptance check on the merged tree,
        `judge(head)` says why a review head is too thin or contradicts its verdict
        (None: it is fine; no judge: every review is fine), `worth` is False for
        *not worth pursuing*, `wanted` is the set of A and its dependents whose
        work is still wanted then (the reject handler's step 2), and `leftovers`
        says a dirty `WORKTREE` holds only leftovers of the master's own check.
        Returns what it did: `review <id>`, `revise <id>`, `rebase <id>`,
        `reject <id>`, `post <id>`, `refused <id>` (a replacement below the profile's
        floor, posted again), `accepted`, `short <id>`, `moved`, `restart`,
        or an OUTCOME (`dirty`, `error`)."""
        sync(self.hive)
        arms = dict(check=check, judge=judge, worth=worth, wanted=wanted, leftovers=leftovers)
        line = self.reviews(task)
        nxt, a, head = line["NEXT"], line["ARTIFACT"], line["HEAD"]
        title = self.inbox(a)["title"]
        if nxt in ("post-rebase", "reject-review"):                                # arm 1
            done = []
            if nxt == "post-rebase" and not self.is_commit(line["SHA"]):           # no rebase can build
                self.reject(line["REVIEW"], f"review {line['REVIEW']} cannot build: "
                                            f"{line['SHA']} is not a commit")
                return [f"reject {line['REVIEW']}", *self.chain_wake(task, **arms)]  # run it again
            if nxt == "post-rebase":
                reb = self.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a,
                                "--body", f"Merge {line['SHA']} onto rip-swarm/integration and "
                                          "resolve the conflict; the resolution is the work.")
                done.append(f"rebase {reb}")
            else:                                                                  # posted before a crash
                board = read_board(self.hive, self.now)
                reb = max((v for v in chain(board, a) if v.fixes == a), key=posting_order).task_id
            self.reject(line["REVIEW"], f"superseded by rebase {reb}")
            return done + [f"reject {line['REVIEW']}"]
        if nxt in ("post-review", "post-revise", "merge") \
                and self.inbox(head).get("reviews"):                               # arm 2
            if not self.is_commit(line["SHA"]):                                    # `git show` fails:
                return self.reject_badsha(head)                                    # merge item 8's case
            why = judge(head) if judge else None
            if why:
                self.reject(head, f"review rejected: {why}")
                return [f"reject {head}", *self.chain_wake(task, **arms)]          # run it again
        if nxt in ("done", "wait"):                                                # arm 3
            return []
        k = int(line["ROUNDS"].split("/")[0])
        if nxt == "post-review":                                                   # arm 4
            if not self.is_commit(line["SHA"]):                                    # A or a fix: no review
                return self.reject_badsha(head)
            r = self.post(f"Review {k + 1} of {title}", "--reviews", a, "--body",
                          f"Build on {line['SHA']} and review {title} there. Append the review "
                          f"to spec.md as a numbered section, ## <n>. Review {k + 1} "
                          f"(<your agent id>, <your harness>); for code, write "
                          f"docs/reviews/{a}-r{k + 1}.md instead. Edit nothing else. "
                          "Complete with --verdict clean only when the review has no finding "
                          "that needs a change, otherwise --verdict findings.")
            return [f"review {r}"]
        if nxt == "post-revise":                                                   # arm 5
            f = self.post(f"Revise {title} after review {k}", "--fixes", a, "--body",
                          f"Build on {line['SHA']}; fold each finding of review {k} and add a "
                          "dispositions table after the review section (finding, disposition, "
                          "where).")
            return [f"revise {f}"]
        return self.merge_arm(a, line, **arms)                                     # arm 6

    def merge_arm(self, a, line, *, check, judge=None, worth=True, wanted=(), leftovers=True):
        """*Review chains* arm 6: everything is about A; `<T>` is A throughout.
        `line` is the reviews.py line that entered the arm. Item 1's one-shot
        heartbeat is not rehearsed (the clock is frozen); the loop is recorded."""
        arms = dict(check=check, judge=judge, worth=worth, wanted=wanted, leftovers=leftovers)
        title = self.inbox(a)["title"]
        self.loop_start()                                                          # 6.1
        outcome = self.review_block(a, line["SHA"])
        if outcome != "merged":                                                    # 6.4, 6.7, 6.8:
            self.loop_stop()                                                       # right after the line
        if outcome == "conflict":                                                  # 6.4
            reb = self.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a,
                            "--body", f"Merge {self.last_full} onto rip-swarm/integration and "
                                      "resolve the conflict; the resolution is the work.")
            self.send(self.completer(a)[0], f"rebase {reb} of {a} is posted for you")  # A's author
            return [f"rebase {reb}"]
        if outcome == "badsha":                                                    # 6.8
            return self.reject_badsha(line["HEAD"])
        if outcome == "dirty" and leftovers:                                       # 6.7: its own dirt
            git(self.wt, "reset", "-q", "--hard")                                  # remove it, then
            git(self.wt, "clean", "-q", "-fd")                                     # restart from 6.1,
            return ["restart", *self.merge_arm(a, line, **arms)]                   # never ordinary 9
        if outcome != "merged":                                                    # 6.7: report, stop,
            return [outcome]                                                       # the loop stopped
        review = f"rip-swarm/review-{a}"
        if check(self.wt):
            self.loop_stop()                                                       # 6.2
            sync(self.hive)                                  # 6.2: the one-shot heartbeat syncs
            again = self.reviews(a)
            if (again["NEXT"], again["HEAD"], again["SHA"]) != ("merge", line["HEAD"], line["SHA"]):
                assert self._ok("switch", "rip-swarm/integration") and self._ok("branch", "-D", review)
                return ["moved", *self.chain_wake(a, **arms)]                      # act on the new line
            if git(self.wt, "status", "--porcelain"):                              # 6.3: Passes item 1
                return ["dirty"]
            if git(self.wt, "rev-parse", "rip-swarm/integration") != self.last_tip:
                return ["restart", *self.merge_arm(a, line, **arms)]               # MOVED: from 6.1
            assert (self._ok("switch", "rip-swarm/integration")
                    and self._ok("merge", "--ff-only", review) and self._ok("branch", "-d", review))
            new_tip = git(self.wt, "rev-parse", "HEAD")
            for tid in [*filter(None, line["CHAIN"].split(",")), a]:
                rc, out = cli("accept", "--hive", self.hive, "--agent", self.agent, "--task", tid,
                              "--integration-sha", new_tip, at=self.now)
                assert rc == 0, out
            return ["accepted"]
        assert self._ok("switch", "rip-swarm/integration") and self._ok("branch", "-D", review)
        self.loop_stop()                                                           # 6.5, 6.6
        if worth:                                                                  # 6.5
            f = self.post(f"Follow up {title}", "--fixes", a, "--body",
                          f"Build on {self.last_full}: close the gap the check found.")
            return [f"short {f}"]
        done = self.replace(a, wanted)                                             # 6.6: step 2 first
        self.reject(a, "not worth pursuing")
        return done + [f"reject {a}"]

    def handle_release(self, task, **arms):
        """`wake task-finished <task> release`: a chain task runs *Review chains*."""
        sync(self.hive)
        return self.chain_wake(task, **arms) if self.in_chain(task) else []

    def handle_messages(self, **arms):
        """`wake message`: a cannot-build message runs *Review chains* for its review."""
        rc, out = cli("messages", "--hive", self.hive, "--to", self.agent, "--new", at=self.now)
        assert rc == 0, out
        done = []
        for review in re.findall(r"review (task_\w+) cannot build on [0-9a-f]+: conflict", out):
            done += self.chain_wake(review, **arms)
        return done

    def handle_idle(self, task, **arms):
        """`wake idle-board <task>`: run the chain arms first; always report."""
        sync(self.hive)
        done = self.chain_wake(task, **arms) if self.in_chain(task) else []
        return done + [f"report {task}"]


def says(text):
    return lambda wt: (wt / "t.txt").read_text(encoding="utf-8") == text


class TestRehearsal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin, self.repo = make_project(Path(self.tmp.name))
        self.m = Master(join(self.repo, role="master", harness="grok", now=T0))
        self.w1 = Session(join(self.repo, role="worker", harness="claude-code", now=T0))
        self.w2 = Session(join(self.repo, role="worker", harness="claude-code", now=T0))

    def tearDown(self):
        self.tmp.cleanup()

    def _done(self, worker, task, text):
        self.assertEqual(worker.claim(task), 0)
        self.assertIn(worker.start_task()[0], ("merged", "reset"))
        worker.work("t.txt", text, f"work {task}")
        return worker.complete(task)

    def test_plan_race_accept_unblocks_the_dependent(self):
        a = self.m.post("A")
        b = self.m.post("B", "--after", a)
        c = self.m.post("C")
        first, second = sorted([a, c])
        self.assertEqual(self.w1.tick(), Wake("task-available", first))    # one task per wake
        self.assertEqual(self.w1.tick(), Wake("task-available", second))
        self.w2.tick()
        self.assertEqual(self.w1.claim(a), 0)
        self.assertEqual(self.w2.claim(a), 2)                       # lost: someone else holds it
        self.assertEqual(self.w2.claim(b), 2)                       # blocked
        self.assertEqual(self.w2.claim(c), 0)
        self.assertEqual(self.w1.start_task(), ("merged", "none", "none"))
        self.w1.work("t.txt", "A\n", "A")
        self.w1.complete(a)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        self.assertEqual(self.m.handle_complete(a, says("A\n")), "pass")
        self.assertEqual(git(self.m.wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{a}").returncode, 0)
        self.assertEqual(self.w1.tick(), Wake("task-available", b))

    def test_shortfall_is_reviewed_off_integration_then_fixed(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        c = self.m.post("C")
        self.w1.tick()
        bad = self._done(self.w1, t, "wrong\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        wt, review = self.m.wt, f"rip-swarm/review-{t}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", bad)
        # Mid-review, w2 claims other work and merges integration.
        self.w2.tick()
        self.assertEqual(self.w2.claim(c), 0)
        self.assertEqual(self.w2.start_task()[0], "merged")
        self.assertNotEqual(run(self.w2.wt, "merge-base", "--is-ancestor", bad, "HEAD").returncode, 0)
        # Falls short: drop the review branch; the sha stays only on the worker branch.
        git(wt, "switch", "rip-swarm/integration")
        git(wt, "branch", "-D", review)
        holders = git(self.repo, "branch", "--contains", bad, "--format=%(refname:short)").split()
        self.assertEqual(holders, [f"rip-swarm/{self.w1.agent}"])
        f = self.m.post("Fix T", "--fixes", t, "--body", f"build on {bad}; t.txt must say right")
        self.assertEqual(self.w1.tick(), Wake("task-available", f))
        self.assertEqual(self.w1.claim(f), 0)
        self.assertEqual(self.w1.start_task(bad)[:2], ("reset", "merged"))   # the fixes sha, always
        self.w1.work("t.txt", "right\n", "fix T")
        self.w1.complete(f)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} complete"))
        self.assertEqual(self.m.handle_complete(f, says("right\n")), "pass")
        record = json.loads((self.m.hive / "accepted" / f"{t}.json").read_text())
        self.assertEqual(record["via"], [f])
        self.assertEqual(self.w1.tick(), Wake("task-available", d))

    def test_review_conflict_rebase_task_is_the_work(self):
        x = self.m.post("X")
        self.m.now += timedelta(milliseconds=1)                            # x's id sorts before y's
        y = self.m.post("Y")
        self.w1.tick()
        self.w2.tick()
        self._done(self.w1, x, "one\n")
        sha_y = self._done(self.w2, y, "two\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{x} complete"))
        self.assertEqual(self.m.handle_complete(x, says("one\n")), "pass")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{y} complete"))
        self.assertEqual(self.m.handle_complete(y, says("one\ntwo\n")), "conflict")
        r = self.m.post("Rebase Y", "--fixes", y, "--body", f"merge {sha_y} onto integration")
        self.assertEqual(self.w2.tick(), Wake("task-available", r))
        self.assertEqual(self.w2.claim(r), 0)
        self.assertEqual(self.w2.start_task(sha_y)[:2], ("reset", "conflict"))
        (self.w2.wt / "t.txt").write_text("one\ntwo\n", encoding="utf-8")   # the resolution is the work
        git(self.w2.wt, "add", "-A")                                       # step 1's conflict commit
        git(self.w2.wt, "commit", "-q", "--no-edit")
        self.assertEqual(git(self.w2.wt, "status", "--porcelain"), "")      # nothing left to commit
        self.w2.complete(r)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r} complete"))
        self.assertEqual(self.m.handle_complete(r, says("one\ntwo\n")), "pass")
        self.assertTrue((self.m.hive / "accepted" / f"{y}.json").is_file())

    def test_own_release_is_not_reoffered_to_the_releaser(self):
        t = self.m.post("T")
        self.w1.tick()
        self.w2.tick()
        self.assertEqual(self.w1.claim(t), 0)
        rc, _ = cli("release", "--hive", self.w1.hive, "--task", t, "--agent", self.w1.agent,
                    "--note", "cannot merge integration: t.txt")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.w1.tick())
        self.assertEqual(self.w2.tick(), Wake("task-available", t))

    def test_a_crashed_workers_claim_is_stolen(self):
        t = self.m.post("T")
        self.assertEqual(self.w1.claim(t), 0)
        self.w2.now = LATER
        self.assertEqual(self.w2.tick(), Wake("task-available", t))
        self.assertEqual(self.w2.claim(t), 0)
        self.assertIn(f'"agent": "{self.w2.agent}"', remote_show(self.origin, f"claims/{t}.json"))

    def test_a_late_worker_picks_up_open_work(self):
        t = self.m.post("T")
        late = Session(join(self.repo, role="worker", harness="grok", now=T0))
        self.assertEqual(late.tick(), Wake("task-available", t))

    def _handoff(self, *, with_fix):
        t = self.m.post("T")
        self.w1.tick()
        bad = self._done(self.w1, t, "wrong\n")
        f = None
        if with_fix:
            self.m.tick()
            self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
            f = self.m.post("Fix T", "--fixes", t, "--body", f"build on {bad}")
        # Master A stops heartbeating; master B joins after the baton expired.
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        return b, t, f, bad

    def test_handoff_reviews_a_bare_complete_once(self):
        b, t, _f, _bad = self._handoff(with_fix=False)
        self.assertEqual(b.tick(), Wake("task-finished", f"{t} complete"))
        self.assertIsNone(b.tick())

    def test_handoff_skips_while_a_fix_is_live(self):
        b, t, f, _bad = self._handoff(with_fix=True)
        self.assertEqual(b.tick(), Wake("task-finished", f"{t} complete"))
        self.assertEqual(b.handle_complete(t, says("right\n")), "skip")
        self.assertNotEqual(b.tick(), Wake("task-finished", f"{t} complete"))

    def _handoff_with_finished_fix(self):
        b, t, f, bad = self._handoff(with_fix=True)
        self.w1.now = LATER
        self.assertEqual(self.w1.claim(f), 0)
        self.assertEqual(self.w1.start_task(bad)[:2], ("reset", "merged"))
        self.w1.work("t.txt", "right\n", "fix T")
        self.w1.complete(f)
        return b, t, f

    def test_handoff_fix_first(self):
        b, t, f = self._handoff_with_finished_fix()
        self.assertEqual(b.handle_complete(f, says("right\n")), "pass")
        self.assertEqual(b.handle_complete(t, says("right\n")), "noop")
        rc, out = cli("accept", "--hive", b.hive, "--agent", b.agent, "--task", t,
                      "--integration-sha", "abc1234", at=LATER)
        self.assertEqual((rc, out), (0, f"already accepted {t}"))

    def test_handoff_original_first(self):
        b, t, f = self._handoff_with_finished_fix()
        self.assertEqual(b.handle_complete(t, says("right\n")), "skip")     # f awaits acceptance
        self.assertEqual(b.handle_complete(f, says("right\n")), "pass")
        self.assertEqual(json.loads((b.hive / "accepted" / f"{t}.json").read_text())["via"], [f])

    def test_crash_left_review_branch_matching_is_resumed(self):
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")
        wt, review = self.m.wt, f"rip-swarm/review-{t}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", sha)                      # crash before the check
        merged = git(wt, "rev-parse", review)
        seen = []
        self.assertEqual(self.m.review(t, sha, lambda w: seen.append(git(w, "rev-parse", "HEAD")) or True), "pass")
        self.assertEqual(seen, [merged])                                    # no second merge

    def test_failed_resume_switch_is_an_error(self):
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")
        wt, review = self.m.wt, f"rip-swarm/review-{t}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", sha)                      # crash before the check
        git(wt, "switch", "rip-swarm/integration")
        other = Path(self.tmp.name) / "elsewhere"
        git(self.repo, "worktree", "add", "-q", str(other), review)        # switch now refuses it
        self.assertEqual(self.m.review_block(t, sha), "error")
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertFalse((self.m.hive / "accepted" / f"{t}.json").exists())

    def test_a_moved_integration_stops_the_loop_before_step_4_again(self):
        # Grok review 3, audit: on MOVED: the loop is still running, and step 4
        # starts with the one-shot heartbeat, which section 2 forbids while it runs.
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")

        def check(wt):                                   # integration moves during the check
            tip = git(wt, "rev-parse", "rip-swarm/integration")
            moved = git(wt, "commit-tree", "-p", tip, "-m", "moved", f"{tip}^{{tree}}")
            git(wt, "update-ref", "refs/heads/rip-swarm/integration", moved)
            return True

        self.assertEqual(self.m.review(t, sha, check), "moved")
        self.assertEqual(self.m.loop, ["start", "stop"])                   # stopped before step 4
        self.assertEqual(self.m.review(t, sha, says("T\n")), "pass")      # step 4 again
        self.assertTrue(self.m.loop_closed(), self.m.loop)

    def test_crash_left_review_branch_mismatched_is_recreated(self):
        t = self.m.post("T")
        self.w1.tick()
        sha = self._done(self.w1, t, "T\n")
        wt = self.m.wt
        git(wt, "switch", "-c", f"rip-swarm/review-{t}", git(wt, "rev-parse", "rip-swarm/integration"))
        self.assertEqual(self.m.review(t, sha, says("T\n")), "pass")
        self.assertEqual(run(self.repo, "merge-base", "--is-ancestor", sha, "rip-swarm/integration").returncode, 0)

    def test_crash_mid_conflict_is_aborted_and_recreated(self):
        x = self.m.post("X")
        y = self.m.post("Y")
        self.w1.tick()
        self.w2.tick()
        self._done(self.w1, x, "one\n")
        sha_y = self._done(self.w2, y, "two\n")
        self.assertEqual(self.m.review(x, self.m.result_sha(x), says("one\n")), "pass")
        wt = self.m.wt
        git(wt, "switch", "-c", f"rip-swarm/review-{y}", git(wt, "rev-parse", "rip-swarm/integration"))
        self.assertNotEqual(run(wt, "merge", "--no-ff", "--no-edit", sha_y).returncode, 0)  # crash mid-merge
        self.assertEqual(self.m.review(y, sha_y, says("one\ntwo\n")), "conflict")
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertNotEqual(run(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        self.assertNotEqual(run(wt, "show-ref", "--verify", "--quiet", f"refs/heads/rip-swarm/review-{y}").returncode, 0)

    def test_orphaned_original_and_reject_cascade_reach_all_complete(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        self.w1.tick()
        self._done(self.w1, t, "wrong\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t)
        self.m.reject(f, "not worth it")                                   # the master abandons the fix
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} reject"))
        board = read_board(self.m.hive, T0)
        self.assertTrue(all(view.settled for view in fixers(board, t)))    # T is orphaned
        self.m.reject(t, "fix abandoned")
        self.m.reject(d, f"dependency {t} rejected")                       # cascade
        wakes = sorted(self.m.tick().detail for _ in range(2))
        self.assertEqual(wakes, sorted([f"{t} reject", f"{d} reject"]))
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_reject_cascade_survives_a_master_change(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        e = self.m.post("E", "--after", d)
        self.m.reject(t, "not worth it")
        self.m.reject(d, f"dependency {t} rejected")                       # master A dies here
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        self.assertEqual(b.tick(), Wake("task-finished", f"{d} reject"))    # E still waits on D
        self.assertEqual(b.tick(), Wake("task-finished", f"{d} reject"))    # until E is rejected
        b.reject(e, f"dependency {d} rejected")
        self.assertEqual(b.tick(), Wake("task-finished", f"{e} reject"))
        self.assertEqual(b.tick(), Wake("all-complete"))

    def test_reject_cascade_survives_the_same_master_dying(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        e = self.m.post("E", "--after", d)
        self.m.reject(t, "not worth it")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.m.reject(d, f"dependency {t} rejected")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{d} reject"))
        # the session dies here; the same agent's next wait keeps its state file
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{d} reject"))
        self.m.reject(e, f"dependency {d} rejected")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{e} reject"))
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def _chain(self):
        t = self.m.post("T")
        d = self.m.post("D", "--after", t)
        e = self.m.post("E", "--after", d)
        return t, d, e

    def test_reject_handler_resumed_after_a_crash_posts_no_second_replacement(self):
        t, d, e = self._chain()
        self.m.reject(t, "wrong approach")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        r = self.m.post(f"T (replaces {t})")                              # the session dies here
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))    # D still waits on T
        self.m.handle_reject(t, wanted={t, d, e})
        self.assertEqual(self.m.replacements(t), [r])
        [d2], [e2] = self.m.replacements(d), self.m.replacements(e)
        board = read_board(self.m.hive, T0)
        self.assertEqual((board[d2].after, board[e2].after), ((r,), (d2,)))
        self.assertTrue(board[d].rejected and board[e].rejected)
        # The cascade's own tombstones wake the master; they change nothing.
        for dep in sorted([d, e]):
            self.assertEqual(self.m.tick(), Wake("task-finished", f"{dep} reject"))
            self.assertEqual(self.m.handle_reject(dep, wanted={t, d, e}), [])
        self.assertIsNone(self.m.tick())                                  # R, D2, E2 are the plan now
        self.assertEqual(len(list((self.m.hive / "inbox").glob("task_*.json"))), 6)

    def test_a_cascade_reject_posts_no_replacement_of_its_own(self):
        t, d, e = self._chain()
        self.m.reject(t, "not worth it")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.assertEqual(self.m.handle_reject(t), [f"reject {d}", f"reject {e}"])
        for dep in sorted([d, e]):
            self.assertEqual(self.m.tick(), Wake("task-finished", f"{dep} reject"))
            self.assertEqual(self.m.handle_reject(dep, wanted={d, e}), [])     # the note decides
        self.assertEqual(self.m.replacements(d), [])
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_a_replacement_never_waits_on_a_rejected_task(self):
        t, d, e = self._chain()
        self.m.reject(t, "not needed")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.m.handle_reject(t, wanted={d})                               # T is not wanted, D is
        [d2] = self.m.replacements(d)
        self.assertEqual(read_board(self.m.hive, T0)[d2].after, ())
        for dep in sorted([d, e]):
            self.assertEqual(self.m.tick(), Wake("task-finished", f"{dep} reject"))
        self.assertIsNone(self.m.tick())                                  # no reject wake recurs

    def test_a_replacement_drops_a_rejected_dependency_outside_the_chain(self):
        t = self.m.post("T")
        u = self.m.post("U")
        d = self.m.post("D", "--after", t, "--after", u)
        self.m.reject(t, "not needed")
        self.m.reject(u, "not needed")
        self.m.handle_reject(t, wanted={d})                               # T's wake comes first
        [d2] = self.m.replacements(d)
        self.assertEqual(read_board(self.m.hive, T0)[d2].after, ())

    def test_a_dead_replacement_is_not_reused(self):
        t, d, e = self._chain()
        self.m.reject(t, "wrong approach")
        stale = self.m.post(f"D (replaces {d})", "--after", t)            # an older run's slip
        self.m.handle_reject(t, wanted={t, d})
        found = self.m.replacements(d)
        self.assertEqual(len(found), 2)
        [fresh] = [r for r in found if r != stale]
        self.assertEqual(read_board(self.m.hive, T0)[fresh].after, tuple(self.m.replacements(t)))
        self.assertTrue(read_board(self.m.hive, T0)[stale].rejected)      # step 4 reached it
        self.assertEqual(self.m.handle_reject(t, wanted={t, d}), [])      # the rerun keeps `fresh`

    def test_replacing_a_follow_up_keeps_fixes_and_leaves_the_original(self):
        t = self.m.post("T")
        self.w1.tick()
        self._done(self.w1, t, "wrong\n")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t, "--body", "t.txt must say right")
        self.m.reject(f, "wrong approach")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} reject"))
        done = self.m.handle_reject(f, wanted={f})
        [r] = self.m.replacements(f)
        self.assertEqual(done, [f"post {r}"])                             # no orphan step for T
        doc = json.loads((self.m.hive / "inbox" / f"{r}.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["fixes"], doc["body"]), (t, "t.txt must say right"))
        self.assertFalse(read_board(self.m.hive, T0)[t].settled)          # R decides T now
        self.assertEqual(self.m.handle_reject(f, wanted={f}), [])

    def test_the_fixes_walk_stops_at_a_rejected_original(self):
        t = self.m.post("T")
        self.w1.tick()
        bad = self._done(self.w1, t, "wrong\n")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t)
        self.m.reject(t, "dropped")                                        # while F is still open
        self.assertEqual(self.w1.claim(f), 0)
        self.assertEqual(self.w1.start_task(bad)[:2], ("reset", "merged"))
        self.w1.work("t.txt", "right\n", "fix T")
        self.w1.complete(f)
        self.assertEqual(self.m.handle_complete(f, says("right\n")), "pass")
        self.assertFalse((self.m.hive / "accepted" / f"{t}.json").exists())

    def test_reject_handler_skips_an_orphan_already_settled(self):
        t = self.m.post("T")
        self.w1.tick()
        self._done(self.w1, t, "wrong\n")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(t, says("right\n")), "short")
        f = self.m.post("Fix T", "--fixes", t)
        self.m.reject(f, "not worth it")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} reject"))
        self.assertEqual(self.m.handle_reject(f), [f"orphan {t}"])
        self.assertEqual(self.m.handle_reject(f), [])                     # the wake again: X is settled

    def test_badsha_is_rejected_untouched(self):
        t = self.m.post("T")
        self.w1.tick()
        self.assertEqual(self.w1.claim(t), 0)
        (self.w1.wt / "t.txt").write_text("T\n", encoding="utf-8")
        blob = git(self.w1.wt, "hash-object", "-w", "t.txt")[:7]          # an object, not a commit
        ref = f"rip-swarm/{self.w1.agent}@{blob}"
        rc, out = cli("complete", "--hive", self.w1.hive, "--task", t, "--agent", self.w1.agent,
                      "--result-ref", ref)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        wt, before = self.m.wt, git(self.m.wt, "rev-parse", "rip-swarm/integration")
        self.assertEqual(self.m.handle_complete(t, says("T\n")), "badsha")
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertEqual(git(wt, "rev-parse", "rip-swarm/integration"), before)
        self.assertNotEqual(run(wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{t}").returncode, 0)
        self.m.reject(t, f"result_ref {ref} is not a commit")
        self.assertTrue(next((self.m.hive / "claims").glob(f"{t}.reject.*.json"), None))
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} reject"))
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_rejected_work_does_not_ride_into_the_next_result(self):
        t1 = self.m.post("T1")
        self.m.now += timedelta(milliseconds=1)
        t2 = self.m.post("T2")
        self.w1.tick()
        self.assertEqual(self.w1.claim(t1), 0)
        self.assertEqual(self.w1.start_task()[0], "merged")
        bad = self.w1.work("bad.txt", "bad\n", "work T1")
        self.w1.complete(t1)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t1} complete"))
        self.assertEqual(self.m.handle_complete(t1, lambda wt: False), "short")   # not worth pursuing
        self.m.reject(t1, "not worth pursuing")
        self.assertEqual(self.w1.claim(t2), 0)                                   # same worker, next task
        sync_, build, kept = self.w1.start_task()
        self.assertEqual((sync_, build), ("reset", "none"))
        self.assertEqual(git(self.w1.wt, "rev-parse", kept), bad)               # the old tip stays reachable
        self.w1.work("good.txt", "good\n", "work T2")
        self.w1.complete(t2)
        self.m.tick()
        self.assertEqual(self.m.handle_complete(
            t2, lambda wt: (wt / "good.txt").is_file() and not (wt / "bad.txt").exists()), "pass")
        self.assertNotEqual(run(self.repo, "merge-base", "--is-ancestor", bad,
                                "rip-swarm/integration").returncode, 0)

    def test_all_complete_then_the_master_leaves(self):
        t = self.m.post("T")
        self.w1.tick()
        self._done(self.w1, t, "T\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{t} complete"))
        self.assertEqual(self.m.handle_complete(t, says("T\n")), "pass")
        self.assertEqual(self.m.tick(), Wake("all-complete"))
        lines = leave(self.m.hive, self.m.agent, T0)
        self.assertIn("released orchestrator", lines)
        self.assertIsNone(remote_show(self.origin, "claims/orchestrator.json"))
        self.assertEqual(git(self.repo, "show", "rip-swarm/integration:t.txt"), "T")
        self.assertTrue((self.repo / ".worktrees" / "integration").is_dir())

    # Review chains (execution proposals §5.9): two workers, as the role skills assume.

    def _artifact(self, n):
        return self.m.post("Spec", "--kind", "spec", "--min-reviews", str(n), "--body", "Write spec.md")

    def _write_artifact(self, a, text="# Spec\nv1\n"):
        self.assertEqual(self.w1.claim(a), 0)
        self.w1.start_task()
        self.w1.work("spec.md", text, f"{a}: spec")
        return self.w1.complete(a)

    @staticmethod
    def _ids(done, verb):
        return [item.split()[1] for item in done if item.startswith(f"{verb} ")]

    def _spec(self):
        return git(self.repo, "show", "rip-swarm/integration:spec.md")

    def test_spec_with_two_rounds_findings_then_clean(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.assertEqual(self.w1.review(r1, "clean", "x"), "refused")               # the author
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: say why\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        self.assertEqual(self.w2.fold(f1, "x"), "refused")                          # the reviewer
        self.w1.fold(f1, "# Spec\nv2, says why\n\n## Review 1\nfinding: say why\n\n"
                         "### Dispositions\n| say why | accepted | v2 |\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f1} complete"))
        [r2] = self._ids(self.m.handle_complete(f1, None), "review")
        self.w2.review(r2, "clean", "\n## Review 2\nno findings\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} complete"))
        check = lambda wt: "Dispositions" in (wt / "spec.md").read_text(encoding="utf-8")
        self.assertEqual(self.m.handle_complete(r2, check), ["accepted"])
        for tid in (r1, f1, r2, a):
            self.assertTrue((self.m.hive / "accepted" / f"{tid}.json").exists(), tid)
        self.assertEqual(self.m.loop, ["start", "stop"])
        spec = self._spec()
        for needle in ("## Review 1", "### Dispositions", "## Review 2"):
            self.assertIn(needle, spec)
        self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{a}").returncode, 0)
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_the_author_is_refused_the_review_and_the_reviewer_the_revise(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        err = io.StringIO()
        rc, _ = cli("claim", "--hive", self.w1.hive, "--task", r1, "--agent", self.w1.agent, err=err)
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.w1.agent} wrote part of {a}", err.getvalue())
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: x\n")
        self.m.tick()
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        err = io.StringIO()
        rc, _ = cli("claim", "--hive", self.w2.hive, "--task", f1, "--agent", self.w2.agent, err=err)
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.w2.agent} reviewed {a}", err.getvalue())

    def test_two_clean_rounds_by_the_same_reviewer(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        [r2] = self._ids(self.m.handle_complete(r1, None), "review")               # 1/2: again
        self.assertNotEqual(self.w2.review(r2, "clean", "\n## Review 2\nno findings\n"), "refused")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(r2, lambda wt: True), ["accepted"])

    def test_a_takeover_mid_chain_posts_nothing_twice(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: x\n")
        self.m.tick()
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        # The master dies here. A new one joins after its baton expired.
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        self.w1.now = self.w2.now = LATER
        posted = len(list((b.hive / "inbox").glob("task_*.json")))
        for task in sorted([a, r1]):                                               # bare completes
            self.assertEqual(b.tick(), Wake("task-finished", f"{task} complete"))
            self.assertEqual(b.handle_complete(task, None), [])                    # NEXT=wait: F1 is open
        self.assertEqual(b.tick(), Wake("idle-board", f1))                         # open since T0
        self.assertEqual(b.handle_idle(f1), [f"report {f1}"])
        self.assertEqual(len(list((b.hive / "inbox").glob("task_*.json"))), posted)
        self.w1.fold(f1, "# Spec\nv2\n\n## Review 1\nfinding: x\n\n### Dispositions\n| x | done |\n")
        self.assertEqual(b.tick(), Wake("task-finished", f"{f1} complete"))
        self.assertEqual(len(self._ids(b.handle_complete(f1, None), "review")), 1)
        self.assertEqual(b.handle_complete(f1, None), [])                          # a repeat posts nothing

    def test_a_thin_or_contradicted_review_is_rejected_and_does_not_count(self):
        for text, why in (("\n## Review 1\nok\n", "too thin"),
                          ("\n## Review 1\nfinding: the spec skips errors\n", "clean over a listed finding")):
            with self.subTest(why=why):
                self.tearDown()
                self.setUp()
                a = self._artifact(1)
                self._write_artifact(a)
                self.m.tick()
                [r1] = self._ids(self.m.handle_complete(a, None), "review")
                self.w2.review(r1, "clean", text)
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
                done = self.m.handle_complete(r1, None, judge=lambda head: why)
                self.assertEqual(done[0], f"reject {r1}")
                [r2] = self._ids(done, "review")                                   # round 1 again
                self.assertEqual(self.m.reviews(a)["ROUNDS"], "0/1")
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
                self.assertEqual(self.m.handle_reject(r1), [])                     # NEXT=wait
                self.w2.review(r2, "clean", "\n## Review 2\nchecked every section; no findings\n")
                self.m.tick()
                self.assertEqual(self.m.handle_complete(r2, lambda wt: True, judge=lambda head: None),
                                 ["accepted"])
                self.assertFalse((self.m.hive / "accepted" / f"{r1}.json").exists())

    def test_a_master_that_dies_after_rejecting_a_review_posts_on_the_reject_wake(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nok\n")
        self.m.tick()
        self.m.reject(r1, "review rejected: too thin")                            # dies before the rerun
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(len(self._ids(self.m.handle_reject(r1), "review")), 1)

    def test_the_merge_rechecks_reviews_before_the_fast_forward(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        tip = git(self.m.wt, "rev-parse", "rip-swarm/integration")

        def check(wt):                                   # the board moves during the check
            self.m.reject(r1, "review rejected: found late")
            return True

        done = self.m.handle_complete(r1, check)
        self.assertEqual(done[0], "moved")
        self.assertEqual(len(self._ids(done, "review")), 1)
        self.assertEqual(self.m.loop, ["start", "stop"])
        self.assertEqual(git(self.m.wt, "rev-parse", "rip-swarm/integration"), tip)
        self.assertEqual(git(self.m.wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                f"refs/heads/rip-swarm/review-{a}").returncode, 0)

    def test_zero_rounds_is_todays_path(self):
        a = self.m.post("Spec", "--kind", "spec", "--body", "Write spec.md")      # the profile's 0
        self.assertEqual(self.m.inbox(a)["min_reviews"], 0)
        self._write_artifact(a, "T\n")
        self.m.tick()
        self.assertFalse(self.m.in_chain(a))
        self.assertEqual(self.m.handle_complete(a, lambda wt: True), "pass")
        self.assertEqual(len(list((self.m.hive / "inbox").glob("task_*.json"))), 1)
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    # Review chains, the failure paths (execution proposals §5.9).

    def _conflicting_review(self, message=True):
        """A's review cannot build: integration took another task's spec.md first.
        Returns (a, sha of A, the released review)."""
        a = self._artifact(1)
        x = self.m.post("Other", "--body", "Write spec.md too")
        sa = self._write_artifact(a)
        self.assertEqual(self.w2.claim(x), 0)
        self.w2.start_task()
        self.w2.work("spec.md", "# Other\n", f"{x}: other")
        self.w2.complete(x)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{x} complete"))
        self.assertEqual(self.m.handle_complete(x, lambda wt: True), "pass")
        self.assertEqual(self.w2.review(r1, "clean", "x", message=message), "released")
        stone = next((self.w2.hive / "claims").glob(f"{r1}.release.*.json"))
        note = json.loads(stone.read_text(encoding="utf-8"))["note"]
        self.assertEqual(note, f"review {r1} cannot build on {sa}: conflict")  # the body's sha
        self.assertNotEqual(git(self.w2.wt, "rev-parse", "--short", "HEAD"), sa)
        self.assertNotEqual(run(self.w2.wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        return a, sa, r1

    def _rebases(self, a):
        sync(self.m.hive)
        board = read_board(self.m.hive, self.m.now)
        return [v.task_id for v in chain(board, a) if v.title.startswith("Rebase ")]

    def test_a_review_that_cannot_build_is_rebased_once_and_costs_a_round(self):
        for message in (True, False):
            with self.subTest(message=message):
                self.tearDown()
                self.setUp()
                a, sa, r1 = self._conflicting_review(message=message)
                if message:                                                        # the message first
                    self.assertEqual(self.m.tick(), Wake("message"))
                    done = self.m.handle_messages()
                    self.assertEqual(self._ids(done, "reject"), [r1])
                    # Both tombstones wake now; by name `reject` sorts before `release`.
                    wakes = sorted(self.m.tick().detail for _ in range(2))
                    self.assertEqual(wakes, sorted([f"{r1} reject", f"{r1} release"]))
                    self.assertEqual(self.m.handle_release(r1), [])                # posts nothing more
                    self.assertEqual(self.m.handle_reject(r1), [])
                else:                                                              # the release alone
                    self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} release"))
                    self.assertEqual(self._ids(self.m.handle_release(r1), "reject"), [r1])
                    self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
                    self.assertEqual(self.m.handle_reject(r1), [])
                [f] = self._rebases(a)
                self.assertEqual(self.w1.body_sha(f), sa)
                self.assertEqual(self.w2.fold(f, "x"), "refused")                  # released a review
                self.w1.fold(f, "# Spec\nv1\n# Other\n")                           # the author resolves
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{f} complete"))
                [r2] = self._ids(self.m.handle_complete(f, None), "review")
                self.assertEqual(self.m.reviews(a)["ROUNDS"], "0/1")               # another round
                self.w2.review(r2, "clean", "\n## Review 1\nno findings\n")
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} complete"))
                self.assertEqual(self.m.handle_complete(r2, lambda wt: True), ["accepted"])
                self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_a_takeover_after_a_release_posts_the_rebase_then_recovers_a_crash(self):
        a, sa, r1 = self._conflicting_review()
        b = Master(join(self.repo, role="master", harness="claude-code", now=LATER), now=LATER)
        self.w1.now = self.w2.now = LATER
        self.assertEqual(b.tick(), Wake("task-finished", f"{a} complete"))        # its first chain wake
        line = b.reviews(a)
        self.assertEqual((line["NEXT"], line["REVIEW"], line["SHA"]), ("post-rebase", r1, sa))
        title = b.inbox(a)["title"]
        f = b.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a, "--body",
                   f"Merge {sa} onto rip-swarm/integration and resolve the conflict; "
                   "the resolution is the work.")                                  # then b dies
        self.w1.fold(f, "# Spec\nv1\n# Other\n")
        self.assertEqual(b.tick(), Wake("task-finished", f"{f} complete"))
        self.assertEqual(b.handle_complete(f, None), [f"reject {r1}"])            # NEXT=reject-review
        self.assertEqual(b.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(len(self._ids(b.handle_reject(r1), "review")), 1)
        self.assertEqual(self._rebases(a), [f])

    def test_a_second_release_of_the_same_review_posts_no_second_rebase(self):
        # Grok review 2, M1: the master posts the rebase and dies before its
        # reject. The review is still open, so a third worker claims it and
        # releases it with the same note. The rebase already posted still counts.
        a, sa, r1 = self._conflicting_review(message=False)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} release"))
        title = self.m.inbox(a)["title"]
        f = self.m.post(f"Rebase {title} onto rip-swarm/integration", "--fixes", a, "--body",
                        f"Merge {sa} onto rip-swarm/integration and resolve the conflict; "
                        "the resolution is the work.")                             # then it dies
        w3 = Session(join(self.repo, role="worker", harness="grok", now=T0))
        w3.now += timedelta(minutes=1)                                             # a later second
        self.assertEqual(w3.review(r1, "clean", "x"), "released")
        self.assertEqual(self.m.tick(), Wake("message"))
        self.assertEqual(self.m.handle_messages(), [f"reject {r1}"])               # NEXT=reject-review
        self.assertEqual(self._rebases(a), [f])

    def test_a_dirty_merge_restarts_the_arm_and_accepts_the_whole_chain(self):
        # Grok review 2, M2: ordinary step 7's "run step 4 again" leads to
        # step 9, which accepts A alone. The merge arm restarts at item 1.
        for leftovers in (True, False):
            with self.subTest(leftovers=leftovers):
                self.tearDown()
                self.setUp()
                a = self._artifact(1)
                self._write_artifact(a)
                self.m.tick()
                [r1] = self._ids(self.m.handle_complete(a, None), "review")
                self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
                (self.m.wt / "check.log").write_text("left by the last check\n", encoding="utf-8")
                done = self.m.handle_complete(r1, lambda wt: True, leftovers=leftovers)
                accepted = [(self.m.hive / "accepted" / f"{tid}.json").exists() for tid in (r1, a)]
                if leftovers:
                    self.assertEqual(done, ["restart", "accepted"])
                    self.assertFalse((self.m.wt / "check.log").exists())
                    self.assertEqual(accepted, [True, True])
                    self.assertEqual(self.m.tick(), Wake("all-complete"))
                    self.assertEqual(self.m.loop, ["start", "stop"] * 2)
                else:                                                              # not ours: report
                    self.assertEqual(done, ["dirty"])
                    self.assertTrue((self.m.wt / "check.log").exists())
                    self.assertEqual(accepted, [False, False])
                    self.assertEqual(self.m.loop, ["start", "stop"])               # Grok review 3, M1

    def test_an_error_at_merge_stops_the_loop(self):
        # Grok review 3, M1: `OUTCOME=error` was "as step 8": report and stop,
        # with the loop of item 1 still heartbeating the baton.
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
        wt, review = self.m.wt, f"rip-swarm/review-{a}"
        git(wt, "switch", "-c", review, git(wt, "rev-parse", "rip-swarm/integration"))
        git(wt, "merge", "--no-ff", "--no-edit", self.m.result_sha(r1))    # crash before the check
        git(wt, "switch", "rip-swarm/integration")
        other = Path(self.tmp.name) / "elsewhere"
        git(self.repo, "worktree", "add", "-q", str(other), review)        # the resume switch refuses it
        self.assertEqual(self.m.handle_complete(r1, lambda wt: True), ["error"])
        self.assertEqual(git(wt, "branch", "--show-current"), "rip-swarm/integration")
        self.assertEqual(self.m.loop, ["start", "stop"])
        self.assertFalse((self.m.hive / "accepted" / f"{a}.json").exists())

    def test_a_conflict_at_merge_stops_the_loop_and_posts_the_rebase(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} complete"))
        (self.m.wt / "spec.md").write_text("# Other\n", encoding="utf-8")   # integration moved on
        git(self.m.wt, "add", "spec.md")
        git(self.m.wt, "commit", "-q", "-m", "other spec")
        [reb] = self._ids(self.m.handle_complete(r1, lambda wt: True), "rebase")
        self.assertEqual(self.m.inbox(reb)["fixes"], a)
        self.assertEqual(self.m.loop, ["start", "stop"])
        self.assertEqual(git(self.m.wt, "branch", "--show-current"), "rip-swarm/integration")

    def test_rejecting_a_revise_runs_reviews_and_never_the_orphan_step(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "findings", "\n## Review 1\nfinding: x\n")
        self.m.tick()
        [f1] = self._ids(self.m.handle_complete(r1, None), "revise")
        self.m.reject(f1, "wrong approach")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{f1} reject"))
        done = self.m.handle_reject(f1, wanted={f1, a})
        self.assertFalse([d for d in done if d.startswith(("orphan", "post"))], done)
        self.assertEqual(len(self._ids(done, "revise")), 1)
        self.assertFalse(read_board(self.m.hive, T0)[a].rejected)

    def test_rejecting_an_artifact_while_a_review_is_claimed(self):
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        [r2] = self._ids(self.m.handle_complete(r1, None), "review")
        self.assertEqual(self.w2.claim(r2), 0)                                     # held at the reject
        self.m.reject(a, "not worth pursuing")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
        before = int(git(self.origin, "rev-list", "--count", "swarm"))
        done = self.m.handle_reject(a, wanted={a})                                 # has a chain: no step 2
        self.assertEqual(done, [f"reject {r1}"])                                   # the skip is no stop
        self.assertEqual(int(git(self.origin, "rev-list", "--count", "swarm")), before + 1)
        self.assertEqual(self.m.replacements(a), [])
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(self.m.handle_reject(r1), [])                             # a cascade note
        self.assertIsNone(self.m.tick())                                           # silent: r2 is held
        # The held review's work by hand: `Session.review` would claim it again.
        self.w2.start_task(self.w2.body_sha(r2))
        self.w2.work("spec.md", "# Spec\nv1\n\n## Review 2\nno findings\n", f"{r2}: review")
        self.w2.complete(r2, "--verdict", "clean")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} complete"))
        self.assertEqual(self.m.handle_complete(r2, None), [])                     # NEXT=done
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))      # derived
        self.assertEqual(self.m.handle_reject(a, wanted={a}), [f"reject {r2}"])
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} reject"))
        self.assertEqual(self.m.handle_reject(r2), [])
        self.assertEqual(self.m.replacements(a), [])
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_a_holders_reject_after_the_artifact_is_rejected_posts_nothing(self):
        # Grok review 3, m1: once A is rejected, a chain task's reject fell
        # through to "run the steps as they are", and step 2 could post a
        # replacement review with no --reviews, outside A's chain.
        a = self._artifact(2)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        [r2] = self._ids(self.m.handle_complete(r1, None), "review")
        self.assertEqual(self.w2.claim(r2), 0)                                     # Bob holds it
        self.m.reject(a, "not worth pursuing")
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
        self.assertEqual(self.m.handle_reject(a, wanted={a}), [f"reject {r1}"])    # r2 skipped
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(self.m.handle_reject(r1), [])
        self.assertIsNone(self.m.tick())                                           # silent: r2 is held
        rc, out = cli("reject", "--hive", self.w2.hive, "--agent", self.w2.agent, "--task", r2,
                      "--note", "cannot review this", at=self.w2.now)              # Bob gives up
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r2} reject"))
        before = int(git(self.origin, "rev-list", "--count", "swarm"))
        self.assertEqual(self.m.handle_reject(r2, wanted={a, r2}), [])             # step 4 only
        self.assertEqual(self.m.replacements(r2), [])
        self.assertEqual(int(git(self.origin, "rev-list", "--count", "swarm")), before)
        self.assertEqual(self.m.tick(), Wake("all-complete"))

    def test_badsha_at_merge_rejects_the_head_review_not_the_artifact(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n",
                       result_ref=f"rip-swarm/{self.w2.agent}@deadbee")
        self.m.tick()
        self.assertEqual(self.m.handle_complete(r1, lambda wt: True), [f"reject {r1}"])
        self.assertEqual(self.m.loop, [])                                          # arm 2, before the merge
        self.assertFalse(read_board(self.m.hive, T0)[a].rejected)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        self.assertEqual(len(self._ids(self.m.handle_reject(r1), "review")), 1)    # the head fell back

    def test_a_review_whose_build_fails_without_a_merge_still_releases(self):
        a = self._artifact(1)
        self._write_artifact(a)
        r1 = self.m.post("Review 1 of Spec", "--reviews", a, "--body",
                         "Build on deadbee and review Spec there.")                # no such commit
        self.assertEqual(self.w2.review(r1, "clean", "x", message=False), "released")
        stone = next((self.w2.hive / "claims").glob(f"{r1}.release.*.json"))
        self.assertEqual(json.loads(stone.read_text(encoding="utf-8"))["note"],
                         f"review {r1} cannot build on deadbee: conflict")

    def _bogus_artifact(self):
        """A completed with a result_ref that is not a commit."""
        a = self._artifact(1)
        self.assertEqual(self.w1.claim(a), 0)
        self.w1.start_task()
        self.w1.work("spec.md", "# Spec\nv1\n", f"{a}: spec")
        rc, out = cli("complete", "--hive", self.w1.hive, "--task", a, "--agent", self.w1.agent,
                      "--result-ref", f"rip-swarm/{self.w1.agent}@deadbee", at=self.w1.now)
        self.assertEqual(rc, 0, out)
        return a

    def test_an_artifact_whose_result_ref_is_not_a_commit_is_rejected_unreviewed(self):
        # Grok review 1, m2: post-review on a non-commit would start a chain
        # that can never build; A takes the ordinary badsha reject instead.
        a = self._bogus_artifact()
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} complete"))
        self.assertEqual(self.m.handle_complete(a, None), [f"reject {a}"])
        board = read_board(self.m.hive, T0)
        self.assertTrue(board[a].rejected)
        self.assertEqual(chain(board, a), [])                                      # no review posted
        stone = next((self.m.hive / "claims").glob(f"{a}.reject.*.json"))
        note = f"result_ref rip-swarm/{self.w1.agent}@deadbee is not a commit"
        self.assertEqual(json.loads(stone.read_text(encoding="utf-8"))["note"], note)
        sync(self.w1.hive)
        rc, out = cli("messages", "--hive", self.w1.hive, "--to", self.w1.agent, "--new", at=self.w1.now)
        self.assertIn(note, out)                                                   # its author is told
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
        self.assertEqual(self.m.handle_reject(a), [])

    def test_a_review_released_on_a_sha_that_is_not_a_commit_rejects_the_artifact(self):
        # Grok review 1, m2: a review posted on a bogus sha (by a master before
        # the check) is released as cannot-build; no rebase is posted for it.
        a = self._bogus_artifact()
        r1 = self.m.post("Review 1 of Spec", "--reviews", a, "--body",
                         "Build on deadbee and review Spec there.")
        self.assertEqual(self.w2.review(r1, "clean", "x", message=False), "released")
        sync(self.m.hive)
        self.assertEqual(self.m.reviews(a)["NEXT"], "post-rebase")
        self.assertEqual(self.m.handle_release(r1), [f"reject {r1}", f"reject {a}"])
        self.assertEqual(self._rebases(a), [])
        board = read_board(self.m.hive, T0)
        self.assertTrue(board[a].rejected and board[r1].rejected)

    def test_a_review_head_that_is_not_a_commit_gets_no_next_round(self):
        # Grok review 1, m2: arm 2's `git show` of a review head that is not a
        # commit is merge item 8's case: that review is rejected, not A.
        a = self.m.post("Spec", "--kind", "spec", "--min-reviews", "2", "--body", "Write spec.md")
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n",
                       result_ref=f"rip-swarm/{self.w2.agent}@deadbee")
        sync(self.m.hive)
        self.assertEqual(self.m.reviews(a)["NEXT"], "post-review")                 # rounds 1/2
        self.m.tick()
        self.assertEqual(self.m.handle_complete(r1, None), [f"reject {r1}"])
        self.assertFalse(read_board(self.m.hive, T0)[a].rejected)
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{r1} reject"))
        [r2] = self._ids(self.m.handle_reject(r1), "review")                       # on A's sha again
        self.assertEqual(self.w2.body_sha(r2), self.m.result_sha(a))

    def test_not_worth_pursuing_at_merge(self):
        for wanted in (False, True):
            with self.subTest(wanted=wanted):
                self.tearDown()
                self.setUp()
                a = self._artifact(1)
                d = self.m.post("Build", "--after", a, "--body", "Build what spec.md says")
                self._write_artifact(a)
                self.m.tick()
                [r1] = self._ids(self.m.handle_complete(a, None), "review")
                self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
                self.m.tick()
                tip = git(self.m.wt, "rev-parse", "rip-swarm/integration")
                done = self.m.handle_complete(r1, lambda wt: False, worth=False,
                                              wanted={a, d} if wanted else set())
                self.assertEqual(done[-1], f"reject {a}")
                self.assertEqual(self.m.loop, ["start", "stop"])
                self.assertEqual(git(self.m.wt, "rev-parse", "rip-swarm/integration"), tip)
                self.assertNotEqual(run(self.m.wt, "show-ref", "--verify", "--quiet",
                                        f"refs/heads/rip-swarm/review-{a}").returncode, 0)
                reps, d_reps = self.m.replacements(a), self.m.replacements(d)
                self.assertEqual((len(reps), len(d_reps)), (1, 1) if wanted else (0, 0))
                if wanted:                                                         # both before A's reject
                    self.assertEqual(done, [f"post {reps[0]}", f"post {d_reps[0]}", f"reject {a}"])
                    doc = self.m.inbox(reps[0])
                    self.assertEqual((doc["kind"], doc["min_reviews"]), ("spec", 1))
                    board = read_board(self.m.hive, T0)
                    self.assertEqual(board[d_reps[0]].after, (reps[0],))           # waits on A's replacement
                    self.assertFalse(board[d].rejected)                            # not yet: A's cascade does it
                else:
                    self.assertEqual(done, [f"reject {a}"])
                self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
                self.assertEqual(sorted(self.m.handle_reject(a, wanted={a, d})),   # has a chain: no step 2
                                 sorted([f"reject {d}", f"reject {r1}"]))
                self.assertEqual((self.m.replacements(a), self.m.replacements(d)),
                                 (reps, d_reps))                                   # none posted again
                stone = next((self.m.hive / "claims").glob(f"{d}.reject.*.json"))
                self.assertEqual(json.loads(stone.read_text(encoding="utf-8"))["note"],
                                 f"dependency {a} rejected")                       # D, by the cascade
                wakes = sorted(self.m.tick().detail for _ in range(2))
                self.assertEqual(wakes, sorted([f"{d} reject", f"{r1} reject"]))
                self.assertEqual(self.m.handle_reject(d, wanted={a, d}), [])       # a cascade note
                self.assertEqual(self.m.handle_reject(r1), [])
                if wanted:
                    self.assertIsNone(self.m.tick())                               # the replacements are the plan
                else:
                    self.assertEqual(self.m.tick(), Wake("all-complete"))

    def _raise_floor(self, kind, n):
        """The operator raises a kind's profile floor after the plan was posted.
        It lands as any hive write does: one allowlisted publish, here from the
        master's clone, so every helper reads it after its next fetch."""
        path = self.m.hive / "profiles" / "default.yaml"

        def op():
            text = path.read_text(encoding="utf-8")
            assert f"  {kind}: 0\n" in text, text
            path.write_text(text.replace(f"  {kind}: 0\n", f"  {kind}: {n}\n"), encoding="utf-8")
            return {}

        publish(self.m.hive, task_id="__none__", op=op, message=f"profile: min_reviews {kind} {n}",
                agent=self.m.agent, now=self.m.now, allow=["profiles/default.yaml"])

    def test_a_replacement_refused_below_a_raised_floor_takes_the_profiles_number(self):
        a = self._artifact(1)                                                      # under spec: 0
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        self._raise_floor("spec", 2)
        done = self.m.handle_complete(r1, lambda wt: False, worth=False, wanted={a})
        [a2] = self.m.replacements(a)
        # The exact copy (1) is refused while A is not yet rejected; the post
        # again with `--kind spec` alone takes the profile's 2.
        self.assertEqual(done, [f"refused {a}", f"post {a2}", f"reject {a}"])
        doc = self.m.inbox(a2)
        self.assertEqual((doc["kind"], doc["min_reviews"], doc["body"]), ("spec", 2, "Write spec.md"))
        self.assertEqual(self.m.tick(), Wake("task-finished", f"{a} reject"))
        self.assertEqual(self.m.handle_reject(a, wanted={a}), [f"reject {r1}"])
        self.assertEqual(self.m.replacements(a), [a2])                             # none posted again

    def test_shortfall_at_merge_posts_a_follow_up_that_costs_a_round(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.w2.review(r1, "clean", "\n## Review 1\nno findings\n")
        self.m.tick()
        tip = git(self.m.wt, "rev-parse", "rip-swarm/integration")
        [f] = self._ids(self.m.handle_complete(r1, lambda wt: False), "short")
        self.assertEqual(self.m.loop, ["start", "stop"])
        self.assertEqual(git(self.m.wt, "rev-parse", "rip-swarm/integration"), tip)
        self.assertEqual(self.m.inbox(f)["fixes"], a)
        self.w1.fold(f, "# Spec\nv2\n\n## Review 1\nno findings\n")
        self.m.tick()
        self.assertEqual(len(self._ids(self.m.handle_complete(f, None), "review")), 1)
        self.assertEqual(self.m.reviews(a)["ROUNDS"], "1/1")                       # the head is the fix

    def test_an_idle_review_nobody_may_claim_is_reported(self):
        a = self._artifact(1)
        self._write_artifact(a)
        self.m.tick()
        [r1] = self._ids(self.m.handle_complete(a, None), "review")
        self.assertEqual(self.w1.review(r1, "clean", "x"), "refused")              # w2's brief excludes it
        self.m.now = T0 + timedelta(minutes=11)
        self.assertEqual(self.m.tick(), Wake("idle-board", r1))
        self.assertEqual(self.m.handle_idle(r1), [f"report {r1}"])


if __name__ == "__main__":
    unittest.main()
