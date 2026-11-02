"""PR review findings: the contract, the ledger and its lifecycle through repair rounds and design reopens."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule, tearDownModule  # unittest module fixtures
from test_pipeline import APPROVE, FINDING, FakeAgents, FakeGitHub, FakeWorkspace
from checkpoints import LIMIT, Checkpoints
from controller import PR_REVIEW_RESERVE, Controller
import conversation
from policy import Blocked, merge_reasons, validate_pr_review, validate_review
import reviews

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
                       {"detail": ""}, {"detail": "x" * 4001}, {"extra": 1}):
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
        conversation.queue(self.state, "claude / m · PR review · changes_requested", source=("reviews", "claude"),
                           kind="review", details={"head": "c" * 40, "round": 1})

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
        if stage == "develop" and self.reopen_once and context["review_findings"]:
            self.reopen_once = False
            self.calls.append((provider, stage, deepcopy(context)))
            return {"summary": "Reconsider the plan.", "edits": [], "checks": [], "design_issue": DESIGN_ISSUE}
        if stage != "review":
            return super().ask(provider, stage, context)
        self.calls.append((provider, stage, deepcopy(context)))
        if provider in self.requesting:
            self.requesting.discard(provider)
            found = (self.findings or {}).get(provider) or [{**FINDING, "detail": provider + " finds the term undefined."}]
            return report("changes_requested", found)
        return report(dispositions=[answer(f["id"], "obsolete" if f["status"] == "withdrawn" else "fixed")
                                    for f in context["own_findings"]])


class LifecycleTests(unittest.TestCase):
    """Findings through the real controller: repair rounds, freshness and gates."""

    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh = FakeGitHub(self.config)

    def run_to(self, agents, stage, ticks=30, until=lambda state: True):
        """Tick, approving the plan when asked, until `stage` (and `until`); return the controller."""
        for _ in range(ticks):
            controller = Controller(self.gh, agents, self.config, workspace_factory=FakeWorkspace)
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
        return merge_reasons(state, self.gh.pr, files, self.gh.pages("commits/x/check-runs"), self.gh.threads(9),
                             self.gh.human_reviews, self.config)

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
        events = "\n".join(c["body"] for c in self.gh.comments if "PR review" in c["body"])
        self.assertIn("**F1** `docs/guide.md`: claude finds the term undefined.", events)
        self.assertIn("**F1** fixed: Lines 3-4 now define the term.", events)

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
        self.assertIn("withdrawn findings awaiting retirement: F1", self.reasons(reopened))

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

    def test_output_budget_keeps_the_checkpoint_within_its_limit(self):
        """With existing findings and both providers' reports, an oversized review is refused, not saved."""
        large = [{**FINDING, "detail": "x" * 3800}] * 6
        agents = Reviewers({"claude", "codex"}, findings={"claude": [{**FINDING, "detail": "y" * 1500}] * 2,
                                                           "codex": large})
        controller = None
        with self.assertRaisesRegex(Blocked, "exceeds the checkpoint budget"):
            controller = self.run_to(agents, "gates")
        controller = Controller(self.gh, agents, self.config, workspace_factory=FakeWorkspace)
        controller.issue = self.gh.repo("issues/7")
        controller.load(self.gh.pages("issues/7/comments"))
        state = controller.state
        self.assertEqual([f["provider"] for f in state["findings"]], ["claude"] * 2)
        self.assertLessEqual(len(controller.checkpoints.audit_body(state).encode()), LIMIT)
        budget = controller.pr_review_output_budget()["max_json_bytes"]
        self.assertLess(budget, len(json.dumps(report("changes_requested", large)).encode()))

    def test_review_steps_never_overflow_the_checkpoint(self):
        """Across the refusal boundary, reviews either reach the gates within the limit or are refused cleanly."""
        results = set()
        for total in range(LIMIT - 12000, LIMIT - 500, 500):
            with self.subTest(total=total):
                self.setUp()
                self.run_to(Reviewers(set()), "review")
                self.pad(total, reason="r" * 1000)
                try:
                    self.run_to(Reviewers(set()), "gates", ticks=4)
                    results.add("gates")
                except Blocked as error:
                    self.assertIn("checkpoint budget", str(error))
                    results.add("refused")
        self.assertEqual(results, {"gates", "refused"})

    def test_oversized_reopen_is_refused_without_changing_the_task(self):
        """A reopen that would not fit changes no finding, design state or approval, and the task blocks."""
        self.run_to(Reviewers(set()), "review")
        _, state = self.load()
        self.pad(LIMIT - PR_REVIEW_RESERVE - 3000)
        _, before = self.load()
        agents = FakeAgents()
        agents.ask = lambda provider, stage, context: report(
            "reopen", design_issue={**DESIGN_ISSUE, "explanation": "e" * 500}) | {"summary": "s" * 2500}
        controller = Controller(self.gh, agents, self.config, workspace_factory=FakeWorkspace)
        with self.assertRaisesRegex(Blocked, "checkpoint budget"):
            controller.advance(7)
        controller.suspend("blocked", "PR review exceeds the checkpoint budget", publish_events=False)
        _, after = self.load()
        self.assertEqual(after["stage"], "blocked")
        for field in ("findings", "design_generation", "approval", "design_reopens", "reviews"):
            self.assertEqual(after.get(field), before.get(field), field)
        self.assertNotIn("review_event", after)

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


if __name__ == "__main__":
    unittest.main()
