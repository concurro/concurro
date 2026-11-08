"""Evidence-based review, individual findings, bounded repairs and artifact publication."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # unittest module fixtures
from pipeline_fixtures import PipelineFixture
import conversation
import design
from policy import Blocked, approval_valid, consensus_valid
from fakes import decision, CONFIG, FakeAgents, FakeGitHub, design_report


class DesignContractTests(PipelineFixture, unittest.TestCase):
    """Reload durable state between provider turns, with merging always disabled."""

    def setUp(self):
        """Start a new isolated issue and deterministic provider transport."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def reach(self, stage):
        """Keep this contract fixture's tighter 12-tick setup bound."""
        for _ in range(12):
            controller = self.tick()
            if controller.state["stage"] == stage:
                return controller
        self.fail("stage not reached: " + stage)

    def test_review_rejects_missing_criterion_forged_source_and_unresolved_approval(self):
        """An unsupported approve response never records an approving vote."""
        for failure in ("missing", "quote", "ambiguous", "uncertain", "duplicate"):
            with self.subTest(failure=failure):
                self.setUp()
                self.reach("plan_qc")
                original = self.agents.ask

                def invalid(provider, stage, context):
                    """Alter only evidence while leaving the approve verdict intact."""
                    report = original(provider, stage, context)
                    if failure == "missing":
                        report["assessments"].pop()
                    elif failure == "quote":
                        report["sources"][0]["quote"] = "Invented source"
                    elif failure == "ambiguous":
                        path = report["sources"][0]["path"]
                        self.gh.trees[self.gh.base][path] = "Use docs.\nUse docs.\n"
                        report["sources"][0]["line"] = 999
                    elif failure == "uncertain":
                        report["assessments"][0]["status"] = "uncertain"
                    else:
                        report["assessments"].append(deepcopy(report["assessments"][0]))
                    return report

                with patch.object(self.agents, "ask", side_effect=invalid):
                    self.tick()
                state = self.load().state
                self.assertEqual((state["stage"], state["review_repairs"]), ("plan_qc", 1))
                self.assertFalse(state["design_votes"])
                self.assertFalse(approval_valid(state, self.config))
                self.assertFalse(self.gh.refs)

    def test_every_acceptance_bullet_and_authorized_discussion_is_covered(self):
        """Criterion extraction cannot silently omit a bullet or later requirement."""
        requirement = {
            "title": "Guide",
            "body": "Acceptance:\n- First rule\n  with continuation\n- Second rule",
            "discussion": [{"author": "writer", "body": "Also third rule"}],
        }
        items = design.criteria(requirement)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[1]["text"], "- First rule\n  with continuation")
        self.assertEqual(items[-1]["text"], "Also third rule")

    def test_long_source_quotes_are_verified_without_a_separate_length_limit(self):
        """A real multiline citation over 300 characters can become an approving vote."""
        for valid in (True, False):
            with self.subTest(valid=valid):
                self.setUp()
                self.reach("plan_qc")
                quote = "\n".join(f"Line {i}: " + "source evidence " * 8 for i in range(4))
                self.assertGreater(len(quote), 300)
                self.gh.trees[self.gh.base]["CONTRIBUTING.md"] = quote
                original = self.agents.ask

                def quoted(provider, stage, context):
                    """Return a long literal citation, optionally corrupting its last line."""
                    report = original(provider, stage, context)
                    report["sources"][0]["quote"] = quote if valid else quote + "invented"
                    return report

                with patch.object(self.agents, "ask", side_effect=quoted):
                    self.tick()
                state = self.load().state
                self.assertEqual(bool(state["design_votes"]), valid)
                if valid:
                    self.assertNotIn("review_repair", state)
                    vote = next(iter(state["design_votes"].values()))
                    self.assertEqual(vote["report"]["sources"][0]["quote"], quote)
                else:
                    self.assertEqual(state["review_repair"]["feedback"]["code"], "review_citation")
                    self.assertFalse(approval_valid(state, self.config))

    def test_executable_source_can_support_citations_but_is_not_editable(self):
        """Reading source evidence grants no permission to edit or follow symlinks."""
        controller = self.reach("plan_qc")
        tree = controller.tree(controller.state["base"])
        tree["CONTRIBUTING.md"]["mode"] = "100755"
        path = ["CONTRIBUTING.md"]
        self.assertEqual(controller.files(controller.state["base"], path, inspection=True)[path[0]], "Use docs.")
        with self.assertRaises(Blocked):
            controller.files(controller.state["base"], path)
        tree[path[0]]["mode"] = "120000"
        with self.assertRaises(Blocked):
            controller.files(controller.state["base"], path, inspection=True)

    def test_partial_resolution_keeps_one_stable_objection(self):
        """Accepting one correction does not require approving the entire plan."""
        self.agents.qc_reports = [
            decision("proceed"),
            decision("approve"),
            decision("revise", concerns=["Clarify enrollment.", "Validate the full diff."]),
        ]
        for _ in range(4):
            self.tick()
        old = deepcopy(self.load().state["objections"])
        first_review = "\n".join(self.gh.archives())
        for item in old:
            self.assertIn(json.dumps(item["text"]), first_review, "the revise vote is archived in full")
        self.tick()
        self.tick()
        original = self.agents.ask

        def partial(provider, stage, context):
            """Resolve the first finding and refine the second under its original ID."""
            original(provider, stage, context)
            report = design_report(
                decision("revise", concerns=["Validate the full diff including additions."]), context
            )
            for item, old_item in zip(report["objection_updates"], context["own_objections"]):
                item.update(id=old_item["id"], detail=old_item["text"], status="resolved")
            report["objection_updates"][1].update(status="open", detail=report["concerns"][0])
            return report

        with patch.object(self.agents, "ask", side_effect=partial):
            controller = self.tick()
        self.assertEqual(len(controller.state["objections"]), 1)
        self.assertEqual(controller.state["objections"][0]["id"], old[1]["id"])
        self.assertEqual(controller.state["objection_sequence"], 2)
        latest = json.loads(self.gh.archives()[-1].rsplit("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertIn(
            {"id": old[0]["id"], "status": "resolved"},
            [{k: u[k] for k in ("id", "status")} for u in latest["objection_updates"]],
        )
        self.tick()
        plan_context = [c for _, stage, c in self.agents.calls if stage == "plan"][-1]
        self.assertEqual([v["id"] for v in plan_context["open_objections"]], [old[1]["id"]])
        self.assertEqual([v["id"] for v in self.load().state["plan"]["responses"]], [old[1]["id"]])

    def test_foreign_or_duplicate_objection_resolution_is_rejected(self):
        """A reviewer cannot close another identity's findings or skip its own."""
        own = [{"id": "O1", "text": "Fix wording."}]
        context = {"review_criteria": design.criteria(self.gh.issue), "own_objections": own}
        report = design_report(decision("approve"), context)
        for identifiers in (["foreign"], ["O1", "O1"], []):
            candidate = deepcopy(report)
            candidate["objection_updates"] = [{**report["objection_updates"][0], "id": v} for v in identifiers]
            with self.assertRaises(Blocked):
                design.validate_evidence(candidate, self.gh.issue, own)

    def test_plan_is_linked_without_an_event_and_an_old_intent_is_dropped(self):
        """The plan is linked from the status comment; an intent saved by an earlier version posts nothing."""
        controller = self.reach("plan_qc")
        controller.state["plan_event_intent"] = {
            "plan": controller.state["plan_id"],
            "round": 1,
            "title": "x · plan updated",
        }
        controller.save()
        self.assertNotIn("plan_event_intent", self.load().state)
        self.assertEqual(
            [c["body"].split("\n")[1] for c in self.gh.comments if c["body"].startswith(conversation.MARKER)],
            ["@writer I've picked up this issue and will start digging in!"],
        )
        self.assertIn("| Plan | ✅ Version 1 | [Implementation plan](", self.gh.summary()["body"])

    def test_review_keeps_complete_design_without_peer_verdicts_or_publication_hints(self):
        """Both phases see the substantive plan summary; fresh reviewers see no prior discussion."""
        self.agents.qc_reports = [
            decision("proceed"),
            decision("approve"),
            decision("revise", concerns=["Add an error-path regression."]),
        ]
        original = self.agents.ask
        summary = "Keep entries in stable slots and flush in slot order, separate from recency links."

        def planning(provider, stage, context):
            """Place substantive architecture in the summary, as the LRU pilot did."""
            report = original(provider, stage, context)
            if stage == "plan":
                report["summary"] = summary
            return report

        with patch.object(self.agents, "ask", side_effect=planning):
            state = self.reach("implement").state
        fresh = [c for _, stage, c in self.agents.calls if stage == "plan_qc" and c["review_phase"] == "fresh"]
        self.assertEqual(len(fresh), 2)
        for _, stage, context in self.agents.calls:
            if stage != "plan_qc":
                continue
            self.assertEqual(context["plan"]["summary"], summary)
            self.assertIn('id ""', context["objection_instructions"])
            for key in ("qc", "design_votes", "design_feedback"):
                self.assertNotIn(key, context)
            self.assertNotIn("publication", context["plan"])
        for context in fresh:
            for key in ("publication", "responses"):
                self.assertNotIn(key, context["plan"])
            self.assertNotIn("controller_session", context)
        self.assertTrue(approval_valid(state, self.config))
        member = next(iter(state["design_votes"]))
        state["design_votes"][member]["report"]["assessments"][0]["status"] = "defect"
        self.assertFalse(consensus_valid(state, self.config))
