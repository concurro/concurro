"""PR review findings: the contract, the ledger and its lifecycle through repair rounds and design reopens."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule, tearDownModule  # unittest module fixtures
from test_pipeline import APPROVE, FINDING, FakeAgents, FakeGitHub
from checkpoints import LIMIT, Checkpoints
from controller import Controller
import conversation
from policy import Blocked, digest, validate_pr_review, validate_review
import reviews
from readiness import review_reasons

DESIGN_ISSUE = {"kind": "plan", "explanation": "The plan misses a case.", "evidence": ["The guide omits it."]}


def report(verdict="approve", findings=(), dispositions=(), design_issue=None):
    return {"verdict": verdict, "summary": "Reviewed.", "findings": [dict(f) for f in findings],
            "dispositions": [dict(d) for d in dispositions], "design_issue": design_issue}


def answer(identifier, status="fixed"):
    return {"id": identifier, "status": status, "evidence": "Lines 3-4 now define the term."}


class ContractTests(unittest.TestCase):
    """`validate_pr_review` checks anchors and that dispositions answer exactly the reviewer's findings."""

    def test_valid_reports(self):
        anchored = {**FINDING, "start_line": 3, "line": 5}
        general = {**FINDING, "path": None}
        validate_pr_review(report("changes_requested", [anchored, general]), [], [])
        validate_pr_review(report("changes_requested", dispositions=[answer("F1", "open")]), ["F1"], [])
        validate_pr_review(report(dispositions=[answer("F1"), answer("F2", "obsolete")]), ["F1"], ["F2"])
        validate_pr_review(report("reopen", design_issue=DESIGN_ISSUE), ["F1"], [])

    def test_invalid_findings(self):
        for change in ({"path": "/etc/passwd"}, {"path": "docs/../x"}, {"path": ""}, {"line": 0},
                       {"start_line": 4, "line": 3}, {"start_line": 3, "line": None}, {"path": None, "line": 2},
                       {"detail": ""}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                validate_pr_review(report("changes_requested", [{**FINDING, **change}]), [], [])
        with self.assertRaises(Blocked):
            validate_pr_review(report("changes_requested", [FINDING] * 21), [], [])

    def test_dispositions_answer_each_finding_once(self):
        cases = {"missing": ([], ["F1"], []), "duplicate": ([answer("F1"), answer("F1")], ["F1"], []),
                 "unknown": ([answer("F2")], ["F1"], []), "obsolete while open": ([answer("F1", "obsolete")], ["F1"], []),
                 "bad status": ([answer("F1", "maybe")], ["F1"], []),
                 "no evidence": ([{**answer("F1"), "evidence": " "}], ["F1"], [])}
        for name, (dispositions, open_ids, withdrawn_ids) in cases.items():
            with self.subTest(case=name), self.assertRaises(Blocked):
                validate_pr_review(report(dispositions=dispositions), open_ids, withdrawn_ids)

    def test_verdict_matches_what_remains_open(self):
        with self.assertRaises(Blocked):
            validate_pr_review(report(dispositions=[answer("F1", "open")]), ["F1"], [])
        with self.assertRaises(Blocked):
            validate_pr_review(report("changes_requested", dispositions=[answer("F1")]), ["F1"], [])
        with self.assertRaises(Blocked):
            validate_pr_review(report("reopen", dispositions=[answer("F1")], design_issue=DESIGN_ISSUE), ["F1"], [])

    def test_local_review_contract_is_unchanged(self):
        """Local review, and evidence stored before this change, keep the original contract."""
        local = {"verdict": "changes_requested", "summary": "Define it.", "findings": [{"path": "a", "detail": "b"}],
                 "design_issue": None}
        validate_review(local)
        validate_review(deepcopy(APPROVE))
        with self.assertRaises(Blocked):
            validate_pr_review(local, [], [])


class LedgerTests(unittest.TestCase):
    """The ledger gives each finding one identity and tracks it through dispositions."""

    def test_identities_are_stable_and_deterministic(self):
        state = {}
        first = report("changes_requested", [FINDING, {**FINDING, "line": 4}])
        sequence, identifiers = reviews.apply(state, "claude", first, "a" * 40)
        self.assertEqual((sequence, identifiers), (1, ["F1", "F2"]))
        replay = {}
        self.assertEqual(reviews.apply(replay, "claude", deepcopy(first), "a" * 40), (1, ["F1", "F2"]))
        self.assertEqual(replay, state, "a retained result applied again after a crash records the same ledger")
        _, more = reviews.apply(state, "codex", report("changes_requested", [FINDING]), "a" * 40)
        self.assertEqual(more, ["F3"])
        self.assertEqual([f["id"] for f in reviews.owned(state, "claude")], ["F1", "F2"])

    def test_dispositions_close_withdraw_retire_and_readopt(self):
        state = {}
        reviews.apply(state, "claude", report("changes_requested", [FINDING, FINDING, FINDING]), "a" * 40)
        reviews.apply(state, "claude", report("changes_requested", dispositions=[
            answer("F1"), answer("F2", "open"), answer("F3", "open")]), "b" * 40)
        self.assertEqual([f["status"] for f in state["findings"]], ["fixed", "open", "open"])
        reviews.withdraw(state)
        self.assertEqual([f["status"] for f in state["findings"]], ["fixed", "withdrawn", "withdrawn"])
        self.assertEqual([f["id"] for f in reviews.owned(state, "claude")], ["F2", "F3"])
        reviews.apply(state, "claude", report("changes_requested", dispositions=[
            answer("F2", "obsolete"), answer("F3", "open")]), "c" * 40)
        self.assertEqual([f["status"] for f in state["findings"]], ["fixed", "obsolete", "open"])

    def test_compaction_excerpts_only_closed_findings(self):
        state = {}
        long = {**FINDING, "detail": "word " * 200}
        reviews.apply(state, "claude", report("changes_requested", [long, long]), "a" * 40)
        reviews.apply(state, "claude", report("changes_requested", dispositions=[answer("F1"), answer("F2", "open")]),
                      "b" * 40)
        digests = [f["text_digest"] for f in state["findings"]]
        reviews.compact(state)
        reviews.compact(state)
        fixed, still_open = state["findings"]
        self.assertTrue(fixed["detail"].endswith(reviews.ELIDED))
        self.assertLess(len(fixed["detail"]), len(long["detail"]))
        self.assertEqual(still_open["detail"], long["detail"])
        self.assertEqual([f["text_digest"] for f in state["findings"]], digests)


class LegacyRenderingTests(unittest.TestCase):
    """Review events queued or posted before the ledger keep their exact text and digest."""

    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.gh = FakeGitHub(self.config)
        legacy = {"verdict": "changes_requested", "summary": "One ambiguity remains.",
                  "findings": [{"path": "docs/guide.md", "detail": "Define the term."},
                               {"path": "docs/other.md", "detail": "Link it."}],
                  "design_issue": None, "head": "c" * 40, "model": "m"}
        self.state = {"issue": 7, "reviews": {"claude": legacy}}
        # Queued before format 2: no `format`, so it renders in format 1 forever.
        details = {"head": "c" * 40, "round": 1}
        event = {"id": "e" * 64, "title": "claude / m · PR review · changes_requested", "sequence": 1,
                 "at": "2026-11-01T00:00:00+00:00", "source": ["reviews", "claude"], "kind": "review", "details": details}
        event["content_digest"] = digest(conversation.content(event, conversation.render(
            self.state, "review", ("reviews", "claude"), details)))
        self.state["conversation"] = {"sequence": 1, "pending": [event]}

    def test_rendering_is_unchanged(self):
        body = conversation.render(self.state, "review", ("reviews", "claude"), {"head": "c" * 40, "round": 1})
        self.assertEqual(body, f"Commit `{'c' * 40}` · round 1\n\nOne ambiguity remains."
                               "\n\n- docs/guide.md: Define the term.\n\n- docs/other.md: Link it.")

    def test_unpublished_and_already_posted_events_publish_once(self):
        pending = deepcopy(self.state["conversation"]["pending"])
        self.assertTrue(conversation.publish(self.state, self.gh, self.config))
        # The response was lost: the event is still pending, but the comment exists.
        self.state["conversation"]["pending"] = pending
        self.assertTrue(conversation.publish(self.state, self.gh, self.config))
        self.assertEqual(sum(1 for c in self.gh.comments if "PR review" in c["body"]), 1)


class Reviewers(FakeAgents):
    """Each listed provider requests one change on its first review, then confirms the fix.

    `retire` answers withdrawn findings as obsolete; `reopen_once` makes the implementer
    reopen the design on its first repair round.
    """

    def __init__(self, requesting, findings=None, reopen_once=False):
        super().__init__()
        self.requesting = set(requesting)
        self.findings = findings
        self.reopen_once = reopen_once

    def ask(self, provider, stage, context):
        from feedback import restore
        context = restore(context)
        if stage == "develop" and self.reopen_once and context["review_findings"]:
            self.reopen_once = False
            self.calls.append((provider, stage, deepcopy(context)))
            return {"summary": "Reconsider the plan.", "edits": [], "validation": "", "design_issue": DESIGN_ISSUE}
        if stage != "review":
            return super().ask(provider, stage, context)
        self.calls.append((provider, stage, deepcopy(context)))
        if provider in self.requesting:
            self.requesting.discard(provider)
            found = (self.findings or {}).get(provider) or [{**FINDING, "detail": provider + " finds the term undefined."}]
            return report("changes_requested", found)
        return report(dispositions=[answer(f["id"], "obsolete" if f["status"] == "withdrawn" else "fixed")
                                    for f in context["own_findings"]])


class Harness:
    """The real controller over the fake GitHub, ticked as the Actions path does."""

    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh = FakeGitHub(self.config)

    def run_to(self, agents, stage, ticks=30, until=lambda state: True):
        """Tick, approving the plan when asked, until `stage` (and `until`); return the controller."""
        for _ in range(ticks):
            controller = Controller(self.gh, agents, self.config)
            controller.advance(7)
            state = controller.state
            if state["stage"] == "planned":
                self.gh.comment("/concurro approve " + state["plan_id"])
            if state["stage"] == stage and until(state):
                return controller
        self.fail("never reached " + stage)

    def load(self):
        """The task's checkpoint and its state."""
        checkpoints = Checkpoints(self.gh, self.config, 7)
        return checkpoints, checkpoints.load(self.gh.pages("issues/7/comments"))

    def pad(self, total, reason=None):
        """Grow the checkpoint to about `total` bytes with untrusted outcome text, which only feeds model context."""
        checkpoints, state = self.load()
        state["prior_outcomes"] = []
        if reason:
            state["reason"] = reason
        base = len(checkpoints.audit_body(state).encode())
        state["prior_outcomes"] = [{"padding": "x" * max(0, total - base - 30)}]
        checkpoints.save(state)

    def reasons(self, state):
        """The merge gates' reasons for this state against the fake's PR."""
        files = [f["filename"] for f in self.gh.pages("pulls/9/files")]
        return review_reasons(state, self.gh.pr, files, self.gh.threads(9), self.config)


class LifecycleTests(Harness, unittest.TestCase):
    """Findings through the real controller: repair rounds, freshness and gates."""

    def test_both_providers_fix_their_findings_on_one_head(self):
        """Each provider's dispositions leave the other's report fresh; one repair round reaches the gates."""
        agents = Reviewers({"claude", "codex"})
        state = self.run_to(agents, "gates").state
        self.assertEqual(state["round"], 1)
        self.assertEqual(sum(1 for _, stage, _ in agents.calls if stage == "review"), 4)
        self.assertEqual([(f["id"], f["provider"], f["status"]) for f in state["findings"]],
                         [("F1", "claude", "fixed"), ("F2", "codex", "fixed")])
        self.assertNotIn("claude approval missing or stale", self.reasons(state))
        self.assertNotIn("codex approval missing or stale", self.reasons(state))
        archives = [a for a in self.gh.archives() if "Code review ·" in a.split("\n")[1]]
        self.assertEqual(len(archives), 4, "each review is archived; none is copied to the issue")
        self.assertIn('"finding_ids": [\n    "F1"\n  ]', "\n".join(archives))
        self.assertEqual([r["event"] for r in self.gh.app_reviews], ["COMMENT"] * 4)
        self.assertEqual([t["isResolved"] for t in self.gh.thread_list], [True, True])

    def test_repair_round_receives_open_findings(self):
        agents = Reviewers({"claude"})
        self.run_to(agents, "gates")
        repairs = [context for _, stage, context in agents.calls if stage == "develop" and context["review_findings"]]
        self.assertEqual([[f["id"] for f in c["review_findings"]] for c in repairs], [["F1"]])

    def test_gates_require_fresh_reports_and_a_closed_ledger(self):
        state = self.run_to(Reviewers({"claude"}), "gates").state
        self.assertFalse([r for r in self.reasons(state) if "review" in r or "approval" in r])
        stale = {**state, "input_revision": state["input_revision"] + 1}
        self.assertIn("claude approval missing or stale", self.reasons(stale))
        reopened = deepcopy(state)
        reopened["findings"][0]["status"] = "open"
        self.assertIn("open review findings", self.reasons(reopened))
        reopened["findings"][0]["status"] = "withdrawn"
        self.assertIn("withdrawn findings awaiting retirement", self.reasons(reopened))

    def test_retirement_through_a_replan(self):
        """The implementer reopens the design; the new plan's review retires the withdrawn finding."""
        agents = Reviewers({"claude"}, reopen_once=True)
        state = self.run_to(agents, "gates", ticks=40).state
        self.assertEqual(state["design_reopens"], 1)
        self.assertEqual([(f["id"], f["status"]) for f in state["findings"]], [("F1", "obsolete")])
        retiring = [c for p, stage, c in agents.calls if stage == "review" and p == "claude" and c["own_findings"]]
        self.assertEqual([[f["status"] for f in c["own_findings"]] for c in retiring], [["withdrawn"]])

    def test_crash_after_the_review_result_is_kept(self):
        """A crash before the ledger is saved re-applies the retained result: same IDs, no second model call."""
        agents = Reviewers({"claude"})
        save, crashed = Controller.save, []

        def crash_once(controller, *args, **kwargs):
            if controller.state.get("findings") and not crashed:
                crashed.append(True)
                raise RuntimeError("process killed")
            return save(controller, *args, **kwargs)

        with patch.object(Controller, "save", crash_once):
            with self.assertRaises(RuntimeError):
                self.run_to(agents, "gates")
        calls = sum(1 for p, stage, _ in agents.calls if stage == "review" and p == "claude")
        controller = self.run_to(agents, "review", until=lambda state: state.get("findings"))
        self.assertEqual(sum(1 for p, stage, _ in agents.calls if stage == "review" and p == "claude"), calls)
        self.assertEqual([f["id"] for f in controller.state["findings"]], ["F1"])
        self.assertEqual(controller.state["finding_sequence"], 1)

    def test_large_reviews_keep_all_findings_and_reach_the_gates(self):
        """Full reviews and their ledger copies use references rather than shrinking reviewer capacity."""
        large = [{**FINDING, "detail": "x" * 3800}] * 6
        agents = Reviewers({"claude", "codex"}, findings={"claude": [{**FINDING, "detail": "y" * 1500}] * 2,
                                                           "codex": large})
        controller = self.run_to(agents, "gates")
        self.assertEqual(len(controller.state['findings']), 8)
        self.assertLessEqual(len(controller.checkpoints.audit_body(controller.state).encode()), LIMIT)

    def test_review_steps_never_overflow_the_checkpoint(self):
        """Bulky prior outcomes do not reduce either reviewer's evidence allowance."""
        for total in (48000, 59000, 90000):
            with self.subTest(total=total):
                self.setUp()
                self.run_to(Reviewers(set()), "review")
                self.pad(total, reason="r" * 1000)
                controller = self.run_to(Reviewers(set()), "gates", ticks=4)
                self.assertLessEqual(len(controller.checkpoints.audit_body(controller.state).encode()), LIMIT)

    def test_large_reopen_is_persisted_with_its_full_design_issue(self):
        """A valid design defect remains actionable even when old outcomes are large."""
        self.run_to(Reviewers(set()), "review")
        self.pad(51000)
        agents = FakeAgents()
        agents.ask = lambda provider, stage, context: report(
            "reopen", design_issue={**DESIGN_ISSUE, "explanation": "e" * 500}) | {"summary": "s" * 2500}
        controller = Controller(self.gh, agents, self.config)
        controller.advance(7)
        _, after = self.load()
        self.assertEqual(after["stage"], "plan")
        self.assertEqual(after['design_reopens'], 1)
        self.assertIn('e' * 500, '\n'.join(self.gh.archives()))

    def test_design_reopen_withdraws_and_a_new_review_retires(self):
        """Open findings are withdrawn with the design; their provider answers them under the new plan."""
        controller = self.run_to(Reviewers({"claude"}), "implement",
                                 until=lambda state: any(f["status"] == "open" for f in state.get("findings", [])))
        state = controller.state
        revision = state["input_revision"]
        controller.invalidate_design("The plan misses a case.")
        self.assertEqual(state["input_revision"], revision + 1)
        self.assertEqual({f["status"] for f in state["findings"]}, {"withdrawn"})
        self.assertEqual([f["id"] for f in reviews.owned(state, "claude")], ["F1"])
        sequence, _ = reviews.apply(state, "claude", report(dispositions=[answer("F1", "obsolete")]), "d" * 40)
        self.assertEqual((state["findings"][0]["status"], state["findings"][0]["answered"]), ("obsolete", sequence))


PATCH = "@@ -1,3 +1,4 @@\n context one\n-old two\n+new two\n+new three\n context four\n@@ -20,2 +21,2 @@\n context x\n+added y"


class PlacementTests(unittest.TestCase):
    """Only lines the patch shows on the right side are commented inline."""

    def test_shown_lines(self):
        self.assertEqual(reviews.shown_lines(PATCH), [{1, 2, 3, 4}, {21, 22}])

    def test_placement(self):
        patches = {"a.md": PATCH, "big.md": None}
        cases = [({"path": "a.md", "start_line": None, "line": 2}, "inline"),
                 ({"path": "a.md", "start_line": 2, "line": 4}, "inline"),
                 ({"path": "a.md", "start_line": 4, "line": 21}, "file"),
                 ({"path": "a.md", "start_line": None, "line": 10}, "file"),
                 ({"path": "a.md", "start_line": None, "line": None}, "file"),
                 ({"path": "big.md", "start_line": None, "line": 1}, "file"),
                 ({"path": "other.md", "start_line": None, "line": 1}, "body"),
                 ({"path": None, "start_line": None, "line": None}, "body")]
        for finding, expected in cases:
            with self.subTest(finding=finding):
                self.assertEqual(reviews.placement(finding, patches), expected)
        self.assertEqual(reviews.placement(cases[0][0], patches, inline=False), "file")


    def test_opening_comment_binding(self):
        """An opening is matched by finding, path and text digest, so an excerpted ledger entry still matches."""
        state = {}
        reviews.apply(state, "claude", report("changes_requested", [{**FINDING, "detail": "word " * 200}]), "a" * 40)
        finding = state["findings"][0]
        opening = {"id": 1, "path": FINDING["path"], "body": reviews.finding_body(finding)}
        finding["status"] = "fixed"
        reviews.compact(state)
        self.assertTrue(finding["detail"].endswith(reviews.ELIDED))
        self.assertTrue(reviews.opening_matches(finding, opening))
        for changed in ({"path": "other.md"}, {"in_reply_to_id": 5}, {"body": opening["body"] + " Edited."}):
            with self.subTest(changed=changed):
                self.assertFalse(reviews.opening_matches(finding, {**opening, **changed}))


class PublicationTests(Harness, unittest.TestCase):
    """Reviews on the PR: one thread per finding, answered and resolved at most once, recovered after losses."""

    def comments(self, marker):
        return [c for c in self.gh.review_comments if c["body"].startswith(marker)]

    def test_inline_finding_is_threaded_replied_and_resolved(self):
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
        state = self.run_to(agents, "gates").state
        [first, *_] = self.gh.app_reviews
        self.assertTrue(first["body"].startswith(f"{reviews.REVIEW_MARKER}{CONFIG['repository']}#7:1 -->"))
        [opening] = self.comments(f"{reviews.FINDING_MARKER}F1 -->")
        self.assertEqual((opening["line"], opening["review"]), (1, first["id"]))
        [reply] = self.comments(f"{reviews.DISPOSITION_MARKER}F1:")
        self.assertEqual(reply["in_reply_to_id"], opening["id"])
        self.assertIn("✅ **Fixed** in `", reply["body"])
        finding = state["findings"][0]
        self.assertEqual((finding["thread"], finding["comment"], finding["resolution"]), ("T1", opening["id"], "done"))
        self.assertNotIn("review publication incomplete", self.reasons(state))
        self.assertNotIn("unresolved review threads", self.reasons(state))

    def test_unanchored_and_general_findings(self):
        agents = Reviewers({"claude"}, findings={"claude": [FINDING, {**FINDING, "path": None, "detail": "Overall."}]})
        state = self.run_to(agents, "gates").state
        [file_level] = self.comments(f"{reviews.FINDING_MARKER}F1 -->")
        self.assertEqual(file_level["subject_type"], "file")
        self.assertEqual(self.comments(f"{reviews.FINDING_MARKER}F2 -->"), [])
        body = self.gh.app_reviews[0]["body"]
        self.assertIn("| — | **F2** Overall. | general |", body)
        self.assertIn("<details><summary>Findings without a file</summary>\n\n**F2 · Overall.**\n\nOverall.", body)
        self.assertEqual([f["thread"] for f in state["findings"]], ["T1", "body"])

    def lose_once(self, matches):
        """Apply the next matching write but lose its response."""
        original, lost = self.gh.repo, []

        def repo(path, method="GET", data=None):
            result = original(path, method, data)
            if method == "POST" and matches(path) and not lost:
                lost.append(path)
                raise RuntimeError("lost response")
            return result

        return patch.object(self.gh, "repo", side_effect=repo), lost

    def test_lost_review_and_reply_responses_publish_once(self):
        for target in ("/reviews", "/replies"):
            with self.subTest(target=target):
                self.setUp()
                agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
                patcher, lost = self.lose_once(lambda path: path.endswith(target))
                with patcher:
                    state = self.run_to(agents, "gates").state
                self.assertTrue(lost)
                self.assertEqual(len(self.comments(f"{reviews.FINDING_MARKER}F1 -->")), 1)
                self.assertEqual(len(self.comments(f"{reviews.DISPOSITION_MARKER}F1:")), 1)
                self.assertEqual(len([r for r in self.gh.app_reviews if ":1 -->" in r["body"]]), 1)
                self.assertEqual(state["findings"][0]["resolution"], "done")

    def test_rejected_review_falls_back_to_file_comments_then_blocks(self):
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
        original, rejected = self.gh.repo, []

        def repo(path, method="GET", data=None):
            if method == "POST" and path.endswith("/reviews") and data["comments"] and not rejected:
                rejected.append(data)
                raise RuntimeError("GitHub request failed: POST pulls/9/reviews (HTTP 422)")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=repo):
            self.run_to(agents, "gates")
        [opening] = self.comments(f"{reviews.FINDING_MARKER}F1 -->")
        self.assertEqual(opening["subject_type"], "file")
        self.setUp()
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
        original = self.gh.repo

        def refuse(path, method="GET", data=None):
            if method == "POST" and path.endswith("/reviews"):
                raise RuntimeError("GitHub request failed: POST pulls/9/reviews (HTTP 422)")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=refuse), self.assertRaisesRegex(Blocked, "failed repeatedly"):
            self.run_to(agents, "gates")

    def test_resolution_is_attempted_once(self):
        """A lost resolution is confirmed from the thread; a thread found unresolved is left to a human."""
        for reopened in (False, True):
            with self.subTest(reopened=reopened):
                self.setUp()
                agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
                calls, resolve = [], self.gh.resolve_thread

                def lost(thread_id):
                    calls.append(thread_id)
                    resolve(thread_id)
                    if reopened:
                        self.gh.thread_list[0]["isResolved"] = False
                    raise RuntimeError("lost response")

                with patch.object(self.gh, "resolve_thread", side_effect=lost):
                    controller = self.run_to(agents, "gates", until=lambda state: not state.get("pr_outbox"))
                self.assertEqual(calls, ["T1"])
                finding = controller.state["findings"][0]
                self.assertEqual(finding["resolution"], "manual" if reopened else "done")
                self.assertEqual("unresolved review threads" in self.reasons(controller.state), reopened)

    def test_publication_gate_and_stale_head(self):
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
        controller = self.run_to(agents, "review")
        state = deepcopy(controller.state)
        state["pr_outbox"] = [{"kind": "review"}]
        self.assertIn("review publication incomplete", self.reasons(state))
        controller = self.run_to(agents, "review", until=lambda state: state.get("reviews"))
        posted = len(self.gh.app_reviews)
        [report] = controller.state["reviews"].values()
        reviews.queue_review(controller.state, "claude", {**report, "sequence": report["sequence"]})
        self.gh.pr["head"]["sha"] = "f" * 40  # moved since the review
        controller.state["findings"][0]["comment"] = None
        self.assertTrue(reviews.publish(controller.state, self.gh, self.config, lambda: None))
        self.assertEqual((controller.state["pr_outbox"], len(self.gh.app_reviews)), ([], posted))
        self.assertIsNone(controller.state["findings"][0]["comment"], "nothing re-mapped for a stale head")

    def test_withdrawn_thread_is_notified_then_retired(self):
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]}, reopen_once=True)
        state = self.run_to(agents, "gates", ticks=40).state
        [notice] = self.comments(f"{reviews.WITHDRAWN_MARKER}F1:")
        self.assertIn("design was reopened", notice["body"])
        [reply] = self.comments(f"{reviews.DISPOSITION_MARKER}F1:")
        self.assertIn("🗃️ **Obsolete** under plan", reply["body"])
        self.assertEqual((state["findings"][0]["status"], state["findings"][0]["resolution"]), ("obsolete", "done"))

    def test_review_remains_full_after_publication(self):
        """Referenced reports retain their complete summary before and after PR publication."""
        agents = Reviewers(set())
        ask, summary = agents.ask, "The note is accurate. " * 80

        def long_summary(provider, stage, context):
            result = ask(provider, stage, context)
            return result | {"summary": summary} if stage == "review" else result

        agents.ask = long_summary
        state = self.run_to(agents, "gates").state
        self.assertEqual([summary in r["body"] for r in self.gh.app_reviews], [True, True])
        self.assertTrue(all(r["summary"] == summary for r in state["reviews"].values()),
                        "canonical report references retain the full summary")

    def test_hidden_opening_comment_is_retried_not_unthreaded(self):
        """An opening comment missing from one read keeps the review pending; it is never recorded as body-only."""
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
        original, hidden = self.gh.pages, []

        def pages(path, key=None):
            result = original(path, key)
            if path == "pulls/9/comments" and result and not hidden:
                hidden.append(path)
                return []
            return result

        with patch.object(self.gh, "pages", side_effect=pages):
            controller = self.run_to(agents, "implement", until=lambda state: state.get("findings"))
        self.assertTrue(hidden)
        finding = controller.state["findings"][0]
        self.assertNotEqual(finding.get("thread"), "body")
        state = self.run_to(agents, "gates").state
        self.assertEqual((state["findings"][0]["thread"], state["findings"][0]["resolution"]), ("T1", "done"))
        self.assertEqual(len(self.comments(f"{reviews.FINDING_MARKER}F1 -->")), 1)

    def test_changed_opening_comment_blocks_before_reply_or_resolution(self):
        """An opening comment edited under its marker is a conflict, whether found at publication or before a reply."""
        for when in ("publication", "reply"):
            with self.subTest(when=when):
                self.setUp()
                agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
                original = self.gh.repo

                def edit(path, method="GET", data=None):
                    result = original(path, method, data)
                    if method == "POST" and path.endswith("/reviews") and self.gh.review_comments:
                        self.gh.review_comments[0]["body"] += " Edited."
                    return result

                if when == "reply":
                    self.run_to(agents, "implement", until=lambda state: state.get("findings"))
                    self.gh.review_comments[0]["body"] += " Edited."
                    run = lambda: self.run_to(agents, "gates")
                else:
                    def run():
                        with patch.object(self.gh, "repo", side_effect=edit):
                            self.run_to(agents, "gates")
                with self.assertRaisesRegex(Blocked, "differs from its recorded content"):
                    run()
                self.assertEqual(self.comments(f"{reviews.DISPOSITION_MARKER}F1:"), [])
                self.assertEqual([t["isResolved"] for t in self.gh.thread_list], [False])
                repairs = [c for _, stage, c in agents.calls if stage == "develop" and c["review_findings"]]
                self.assertEqual(bool(repairs), when == "reply", "a conflict at publication blocks before repair")

    def test_reopen_verdict_is_published_on_the_pr(self):
        """A PR review reopening the design is a COMMENT review on the PR, published from its outbox copy."""
        agents = Reviewers(set())
        ask = agents.ask
        reopened = []

        def reopen_once(provider, stage, context):
            if stage == "review" and not reopened:
                reopened.append(provider)
                return report("reopen", design_issue=DESIGN_ISSUE)
            return ask(provider, stage, context)

        agents.ask = reopen_once
        original, refused = self.gh.repo, []

        def refuse_once(path, method="GET", data=None):
            if method == "POST" and path.endswith("/reviews") and not refused:
                refused.append(path)
                raise RuntimeError("GitHub request failed: POST pulls/9/reviews (HTTP 502)")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=refuse_once):
            controller = self.run_to(agents, "plan", until=lambda state: state["design_reopens"] == 1)
        self.assertTrue(refused)
        self.assertEqual(len(controller.state["pr_outbox"]), 1, "retained after the failure")
        controller = self.run_to(agents, "gates", ticks=40)
        self.assertNotIn("review_event", controller.state)
        [reopen] = [r for r in self.gh.app_reviews if "### 🧭 Concurro review · claude · Design reopened" in r["body"]]
        self.assertEqual(reopen["event"], "COMMENT")
        self.assertIn("**Design issue** · plan\n\nThe plan misses a case.", reopen["body"])
        self.assertIn("- The guide omits it.", reopen["body"])
        [archived] = [a for a in self.gh.archives() if "Code review · claude · Design reopened" in a]
        self.assertIn('"explanation": "The plan misses a case."', archived)


if __name__ == "__main__":
    unittest.main()
