# tests/test_reviews.py — review rounds per artifact kind (execution proposals §5)
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hivekit import T0, local_hive
from rip_swarm.acceptance import accept_task, master_reject
from rip_swarm.board import list_tombstones, read_board
from rip_swarm.claim import ClaimDenied, complete, release, try_claim
from rip_swarm.cli import main
from rip_swarm.inbox import create_task
from rip_swarm.orchestrator import promote
from rip_swarm.reviews import ReviewsError, next_step

REG = (
    "- id: op\n  harness: human\n  role: operator\n"
    "- id: master\n  harness: grok\n  role: worker\n"
    "- id: alice\n  harness: claude-code\n  role: worker\n"
    "- id: bob\n  harness: grok\n  role: worker\n"
    "- id: carol\n  harness: claude-code\n  role: worker\n"
)
HARNESS = {"master": "grok", "alice": "claude-code", "bob": "grok", "carol": "claude-code"}
FLOORS = {"spec": 0, "plan": 0, "implementation": 0}
S = timedelta(seconds=1)


def cli(*argv, at=T0):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), \
            mock.patch("rip_swarm.cli.now_utc", return_value=at):
        rc = main([str(a) for a in argv])
    return rc, out.getvalue().strip(), err.getvalue().strip()


class ChainCase(unittest.TestCase):
    """A local board: `master` holds the baton, `alice` writes, `bob` reviews.
    Every post and complete moves the clock one second, so posting order is
    creation order unless a test pins the time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hive = local_hive(Path(self.tmp.name), REG)
        promote(self.hive, agent="master", harness="grok", now=T0, lease_seconds=86400,
                reason="master", allow_self_promote=False, operators=["op"], by="op")
        self.clock = T0
        self.shas = (f"{n:07x}" for n in range(0xa000001, 0xa0fffff))

    def tearDown(self):
        self.tmp.cleanup()

    def later(self):
        self.clock += S
        return self.clock

    def post(self, title, **kw):
        now = kw.pop("now", None) or self.later()
        return create_task(self.hive, title=title, created_by="master", now=now,
                           floors=FLOORS, **kw)["id"]

    def artifact(self, n=2):
        return self.post("Spec", kind="spec", min_reviews=n)

    def done(self, tid, agent, verdict=None):
        now = self.later()
        try_claim(self.hive, tid, agent, HARNESS[agent], now, 3600)
        sha = next(self.shas)
        complete(self.hive, tid, agent, now, f"rip-swarm/{agent}@{sha}", verdict=verdict)
        return sha

    def review(self, a, agent="bob", verdict="clean"):
        r = self.post("Review", reviews=a)
        return r, self.done(r, agent, verdict)

    def fix(self, a, agent="alice"):
        f = self.post("Revise", fixes=a)
        return f, self.done(f, agent)

    def step(self, tid):
        return next_step(self.hive, read_board(self.hive, self.clock), tid)

    def claim_file(self, tid):
        return self.hive / "claims" / f"{tid}.json"


class TestVerdict(ChainCase):
    def test_a_review_completes_with_a_verdict(self):
        a = self.artifact()
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        try_claim(self.hive, r, "bob", "grok", self.later(), 3600)
        for bad in (None, "Clean", "ok"):                                   # Review Focus 1
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "--verdict clean or findings"):
                    complete(self.hive, r, "bob", self.clock, "rip-swarm/bob@abc1234", verdict=bad)
                self.assertTrue(self.claim_file(r).is_file())             # still held, nothing written
        complete(self.hive, r, "bob", self.clock, "rip-swarm/bob@abc1234", verdict="findings")
        [stone] = [s for s in list_tombstones(self.hive) if s.task_id == r]
        self.assertEqual(stone.doc()["verdict"], "findings")

    def test_other_tasks_refuse_a_verdict(self):
        t = self.post("t")
        try_claim(self.hive, t, "alice", "claude-code", self.later(), 3600)
        with self.assertRaisesRegex(ValueError, "not a review task"):
            complete(self.hive, t, "alice", self.clock, "rip-swarm/alice@abc1234", verdict="clean")
        self.assertTrue(self.claim_file(t).is_file())

    def test_cli_verdict_errors_exit_1(self):
        t = self.post("t")
        try_claim(self.hive, t, "alice", "claude-code", self.later(), 3600)
        rc, _, err = cli("complete", "--hive", self.hive, "--task", t, "--agent", "alice",
                         "--result-ref", "rip-swarm/alice@abc1234", "--verdict", "clean",
                         "--local", at=self.clock)
        self.assertEqual(rc, 1)
        self.assertIn("not a review task", err)


class TestNextStep(ChainCase):
    def test_nothing_completed_waits(self):
        a = self.artifact()
        self.assertEqual(self.step(a).line(),
                         f"NEXT=wait ARTIFACT={a} ROUNDS=0/2 HEAD=none SHA=none CHAIN=")

    def test_the_chain_moves_round_by_round(self):
        a = self.artifact(2)
        sa = self.done(a, "alice")
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds, s.chain), ("post-review", a, sa, 0, ()))
        r1 = self.post("Review 1", reviews=a)
        self.assertEqual(self.step(a).next, "wait")                         # a review is open
        sr1 = self.done(r1, "bob", "findings")
        s = self.step(r1)                                                   # any chain task resolves A
        self.assertEqual((s.next, s.artifact, s.head, s.sha, s.rounds), ("post-revise", a, r1, sr1, 1))
        f1, sf1 = self.fix(a)
        s = self.step(f1)
        self.assertEqual((s.next, s.head, s.sha, s.rounds, s.chain), ("post-review", f1, sf1, 1, (r1, f1)))
        r2, sr2 = self.review(a, verdict="clean")
        self.assertEqual(self.step(a).line(),
                         f"NEXT=merge ARTIFACT={a} ROUNDS=2/2 HEAD={r2} SHA={sr2} CHAIN={r1},{f1},{r2}")

    def test_clean_but_short_asks_for_another_round(self):
        a = self.artifact(2)
        self.done(a, "alice")
        r1, sr1 = self.review(a)
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds), ("post-review", r1, sr1, 1))

    def test_a_fix_after_a_clean_review_needs_another_round(self):
        a = self.artifact(1)
        self.done(a, "alice")
        self.review(a)
        f, sf = self.fix(a)
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds), ("post-review", f, sf, 1))

    def test_a_rejected_review_is_not_a_round(self):
        a = self.artifact(1)
        sa = self.done(a, "alice")
        r1, _ = self.review(a)
        self.assertEqual(self.step(a).next, "merge")
        master_reject(self.hive, agent="master", task_id=r1, note="review rejected: thin", now=self.later())
        s = self.step(a)
        self.assertEqual((s.next, s.head, s.sha, s.rounds, s.chain), ("post-review", a, sa, 0, ()))

    def test_done_once_the_artifact_is_settled(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r1, _ = self.review(a)
        accept_task(self.hive, agent="master", task_id=r1, integration_sha="0123abc", now=self.later())
        accept_task(self.hive, agent="master", task_id=a, integration_sha="0123abc", now=self.later())
        self.assertEqual(self.step(r1).next, "done")
        b = self.artifact(1)
        master_reject(self.hive, agent="master", task_id=b, note="drop", now=self.later())
        self.assertEqual(self.step(b).next, "done")

    def test_posting_order_breaks_ties_by_task_id(self):
        low, high = "task_01J00000000000000000000001", "task_01J00000000000000000000002"
        for review_id, fix_id, expected in ((high, low, "merge"), (low, high, "post-review")):
            with self.subTest(review=review_id):
                self.tearDown()
                self.setUp()
                a = self.artifact(1)
                self.done(a, "alice")
                same = self.later()
                self.post("Review", reviews=a, task_id=review_id, now=same)
                self.post("Revise", fixes=a, task_id=fix_id, now=same)
                self.done(fix_id, "alice")
                self.done(review_id, "bob", "clean")
                s = self.step(a)
                self.assertEqual(s.next, expected)
                self.assertEqual(s.chain, tuple(sorted([low, high])))

    def test_a_review_that_cannot_build_asks_for_a_rebase_once(self):
        a = self.artifact(1)
        sa = self.done(a, "alice")
        r1 = self.post("Review", reviews=a)
        try_claim(self.hive, r1, "bob", "grok", self.later(), 3600)
        release(self.hive, r1, "bob", self.clock, note=f"review {r1} cannot build on {sa}: conflict")
        s = self.step(a)
        self.assertEqual((s.next, s.review, s.sha), ("post-rebase", r1, sa))
        self.assertTrue(s.line().endswith(f" REVIEW={r1}"))
        self.post("Rebase Spec onto rip-swarm/integration", fixes=a)
        s = self.step(a)
        self.assertEqual((s.next, s.review), ("reject-review", r1))
        master_reject(self.hive, agent="master", task_id=r1, note="superseded", now=self.later())
        self.assertEqual(self.step(a).next, "wait")                         # the rebase is open

    def test_a_second_cannot_build_release_keeps_the_rebase_posted(self):
        # Grok review 2, M1: the review stays open until the master rejects it,
        # so another worker may claim it and release it with the same note. The
        # rebase posted after the first release still counts.
        a = self.artifact(1)
        sa = self.done(a, "alice")
        r1 = self.post("Review", reviews=a)
        note = f"review {r1} cannot build on {sa}: conflict"
        try_claim(self.hive, r1, "bob", "grok", self.later(), 3600)
        release(self.hive, r1, "bob", self.clock, note=note)
        self.assertEqual(self.step(a).next, "post-rebase")
        self.post("Rebase Spec onto rip-swarm/integration", fixes=a)
        self.assertEqual(self.step(a).next, "reject-review")
        try_claim(self.hive, r1, "carol", "claude-code", self.later(), 3600)
        release(self.hive, r1, "carol", self.later(), note=note)
        s = self.step(a)
        self.assertEqual((s.next, s.review, s.sha), ("reject-review", r1, sa))
        try_claim(self.hive, r1, "bob", "grok", self.later(), 3600)
        release(self.hive, r1, "bob", self.later(), note="gave up")
        try_claim(self.hive, r1, "carol", "claude-code", self.later(), 3600)
        release(self.hive, r1, "carol", self.later(), note=note)
        self.assertEqual(self.step(a).next, "reject-review")             # a note between

    def test_the_latest_release_of_one_second_decides(self):
        # Review Focus 2: the second release of a second is `<stamp>-2.json`,
        # which sorts before `<stamp>.json` by name.
        a = self.artifact(1)
        sa = self.done(a, "alice")
        conflict = "review {} cannot build on " + sa + ": conflict"
        for first, second, expected in ((conflict, "gave up", "wait"),
                                        ("gave up", conflict, "post-rebase")):
            with self.subTest(second=second):
                r = self.post("Review", reviews=a)
                now = self.later()
                for note in (first, second):
                    try_claim(self.hive, r, "bob", "grok", now, 3600)
                    release(self.hive, r, "bob", now, note=note.format(r))
                self.assertEqual(self.step(a).next, expected)
                master_reject(self.hive, agent="master", task_id=r, note="x", now=self.later())

    def test_exit_1_cases(self):
        plain = self.post("plain")
        with self.assertRaisesRegex(ReviewsError, "is not a reviewed artifact or in the chain of one"):
            self.step(plain)
        a = self.artifact(1)
        self.done(a, "alice")
        r1, _ = self.review(a)
        [stone] = [s for s in list_tombstones(self.hive) if s.task_id == r1]
        doc = stone.doc()
        del doc["verdict"]                                                  # edited by hand
        stone.path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(ReviewsError, "has no verdict"):
            self.step(a)

    def test_cli(self):
        a = self.artifact(2)
        rc, out, _ = cli("reviews", "--hive", self.hive, "--task", a, at=self.clock)
        self.assertEqual((rc, out), (0, f"NEXT=wait ARTIFACT={a} ROUNDS=0/2 HEAD=none SHA=none CHAIN="))
        plain = self.post("plain")
        rc, out, err = cli("reviews", "--hive", self.hive, "--task", plain, at=self.clock)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("is not a reviewed artifact", err)


class TestIndependence(ChainCase):
    def claim(self, tid, agent):
        return try_claim(self.hive, tid, agent, HARNESS[agent], self.later(), 3600)

    def test_an_author_is_refused_a_review(self):
        a = self.artifact(2)
        self.done(a, "alice")
        r1 = self.post("Review 1", reviews=a)
        with self.assertRaisesRegex(ClaimDenied, f"alice wrote part of {a}; "
                                                 "its review must come from another agent"):
            self.claim(r1, "alice")
        self.done(r1, "bob", "findings")
        self.fix(a, agent="carol")                                          # a third agent folds
        r2 = self.post("Review 2", reviews=a)
        with self.assertRaisesRegex(ClaimDenied, "carol wrote part of"):
            self.claim(r2, "carol")                                         # and is an author now
        self.claim(r2, "bob")

    def test_a_reviewer_is_refused_a_fix_however_it_reviewed(self):
        for how in ("completed", "released", "holds"):
            with self.subTest(how=how):
                self.tearDown()
                self.setUp()
                a = self.artifact(2)
                self.done(a, "alice")
                r = self.post("Review", reviews=a)
                if how == "completed":
                    self.done(r, "bob", "findings")
                else:
                    self.claim(r, "bob")
                    if how == "released":
                        release(self.hive, r, "bob", self.clock, note="cannot build")
                f = self.post("Revise", fixes=a)
                with self.assertRaisesRegex(ClaimDenied, f"bob reviewed {a}; "
                                                         "its fixes must come from another agent"):
                    self.claim(f, "bob")

    def test_one_reviewer_may_do_every_round(self):
        a = self.artifact(2)
        self.done(a, "alice")
        self.review(a, agent="bob", verdict="clean")
        r2 = self.post("Review 2", reviews=a)
        self.claim(r2, "bob")                                               # after its own clean review

    def test_a_masters_reject_is_not_a_claim(self):
        from rip_swarm.reviews import reviewers
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        master_reject(self.hive, agent="master", task_id=r, note="superseded", now=self.later())
        self.assertEqual(reviewers(self.hive, read_board(self.hive, self.clock), a, self.clock), set())

    def test_the_chain_of_a_rejected_artifact_is_closed(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        master_reject(self.hive, agent="master", task_id=a, note="drop", now=self.later())
        with self.assertRaisesRegex(ClaimDenied, f"{a} is rejected"):
            self.claim(r, "bob")

    def test_cli_refusal_exits_2(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        rc, _, err = cli("claim", "--hive", self.hive, "--task", r, "--agent", "alice",
                         "--local", at=self.later())
        self.assertEqual(rc, 2)
        self.assertIn("alice wrote part of", err)


class TestAcceptReadiness(ChainCase):
    def accept(self, tid, *via):
        return accept_task(self.hive, agent="master", task_id=tid, integration_sha="0123abc",
                           via=via, now=self.later())

    def refused(self, a, n, k):
        with self.assertRaisesRegex(ClaimDenied, f"{a} needs {n} review rounds ending clean, has {k}"):
            self.accept(a)

    def test_findings_last(self):
        a = self.artifact(1)
        self.done(a, "alice")
        self.review(a, verdict="findings")
        self.refused(a, 1, 1)

    def test_clean_but_short(self):
        a = self.artifact(2)
        self.done(a, "alice")
        self.review(a)
        self.refused(a, 2, 1)

    def test_clean_and_met(self):
        a = self.artifact(2)
        self.done(a, "alice")
        r1, _ = self.review(a, verdict="findings")
        f1, _ = self.fix(a)
        r2, _ = self.review(a)
        for tid in (r1, f1, r2):                                            # chain tasks: today's rules
            self.accept(tid)
        self.assertEqual(self.accept(a)["task_id"], a)

    def test_a_fix_after_the_clean_review(self):
        a = self.artifact(1)
        self.done(a, "alice")
        self.review(a)
        self.fix(a)
        self.refused(a, 1, 1)

    def test_a_rejected_review_does_not_count(self):
        a = self.artifact(1)
        self.done(a, "alice")
        r1, _ = self.review(a)
        master_reject(self.hive, agent="master", task_id=r1, note="review rejected: thin",
                      now=self.later())
        self.refused(a, 1, 0)

    def test_via_does_not_bypass_the_rounds(self):
        a = self.artifact(1)
        self.done(a, "alice")
        f, _ = self.fix(a)
        self.accept(f)
        with self.assertRaisesRegex(ClaimDenied, f"{a} needs 1 review rounds ending clean, has 0"):
            self.accept(a, f)

    def test_zero_rounds_is_todays_path(self):
        a = self.artifact(0)
        self.done(a, "alice")
        self.assertEqual(self.accept(a)["task_id"], a)


class TestChainCascade(ChainCase):
    def test_the_cascade_rejects_the_chain_and_skips_a_live_claim(self):
        from rip_swarm.acceptance import cascade_reject
        a = self.artifact(2)
        self.done(a, "alice")
        r1, _ = self.review(a, verdict="findings")
        f1, _ = self.fix(a)
        r2 = self.post("Review 2", reviews=a)
        try_claim(self.hive, r2, "bob", "grok", self.later(), 3600)          # held at the reject
        d = self.post("Plan", after=[a])
        master_reject(self.hive, agent="master", task_id=a, note="not worth pursuing",
                      now=self.later())
        doc = cascade_reject(self.hive, agent="master", task_id=a, now=self.clock)
        self.assertEqual(sorted(doc["rejected"]), sorted([r1, f1, d]))
        [skip] = doc["skipped"]
        self.assertEqual((skip["task_id"], skip["agent"], skip["chain_of"]), (r2, "bob", a))
        from rip_swarm.cli import _cascade_lines
        self.assertIn(f"skipped {r2} (chain of {a}) held by bob until ", _cascade_lines(doc))

    def test_the_derived_wake_waits_for_a_live_claim(self):
        from rip_swarm.acceptance import cascade_reject
        from rip_swarm.state import seed_state
        from rip_swarm.waiter import Wake, tick
        a = self.artifact(1)
        self.done(a, "alice")
        r = self.post("Review", reviews=a)
        try_claim(self.hive, r, "bob", "grok", self.later(), 3600)
        state = seed_state(self.hive, "master")                             # A's complete is unseen
        wake = lambda: tick(self.hive, "master", state, self.clock, idle_after=600)
        master_reject(self.hive, agent="master", task_id=a, note="drop", now=self.later())
        self.assertEqual(wake(), Wake("task-finished", f"{a} complete"))    # its bare complete
        self.assertEqual(wake(), Wake("task-finished", f"{a} reject"))      # the tombstone
        self.assertIsNone(wake())                                           # silent: r is held
        complete(self.hive, r, "bob", self.later(), "rip-swarm/bob@abc1234", verdict="clean")
        self.assertEqual(wake(), Wake("task-finished", f"{r} complete"))
        self.assertEqual(wake(), Wake("task-finished", f"{a} reject"))      # derived, recurs
        self.assertEqual(wake(), Wake("task-finished", f"{a} reject"))
        cascade_reject(self.hive, agent="master", task_id=a, now=self.clock)
        self.assertEqual(wake(), Wake("task-finished", f"{r} reject"))
        self.assertEqual(wake(), Wake("all-complete"))

    def test_status_shows_kind_and_rounds(self):
        from rip_swarm.status import format_status, status_report
        a = self.artifact(2)
        self.done(a, "alice")
        r1, _ = self.review(a, verdict="clean")
        r2 = self.post("Review 2", reviews=a)
        plain = self.post("Plain", kind="plan")                             # a kind, zero rounds
        text = format_status(status_report(self.hive, self.clock))
        self.assertIn(f"{a} Spec (spec, reviews 1/2)", text)
        self.assertIn(f"{r2} Review 2 (reviews {a})", text)
        self.assertIn(f"{plain} Plain (plan)", text)


if __name__ == "__main__":
    unittest.main()
