"""Pilot-shaped invalid outputs receive actionable feedback without weakening validation."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, ROOT, setUpModule as setUpModule, tearDownModule as tearDownModule

import design
from fakes import PLAN, BatchAgents, decision, design_report
from legacy_publications import legacy_events
from pipeline_fixtures import PipelineFixture
from policy import Blocked, design_roster, plan_output_contract, validate_plan


class EvidenceFeedbackTests(unittest.TestCase):
    """Source IDs, paths and quotes remain distinct, strictly checked contracts."""

    def setUp(self):
        """Use real criterion generation and a literal source fixture."""
        self.requirements = {"title": "Improve the guide", "body": "Explain enrollment."}
        self.report = design_report(decision("approve"), {"review_criteria": design.criteria(self.requirements)})

    def test_reference_errors_identify_the_exact_field_without_echoing_input(self):
        """The pilot's path:line and prose are not silently converted into source IDs."""
        for value in ("src/file.rs:42", "https://example.invalid/private", "unknown", [], "@someone secret"):
            with self.subTest(value=value):
                report = deepcopy(self.report)
                report["assessments"][0]["sources"] = [value]
                with self.assertRaises(design.ReviewError) as caught:
                    design.validate_evidence(report, self.requirements)
                error = caught.exception
                self.assertEqual(error.code, "review_evidence")
                self.assertEqual(error.field, "assessments[0].sources[0]")
                self.assertIn("sources[].id", error.detail)
                if isinstance(value, str):
                    self.assertNotIn(value, error.detail)
                self.assertEqual(report["assessments"][0]["sources"], [value])

    def test_source_path_feedback_precedes_repository_reads(self):
        """External URLs and traversal identify the source path, not generic coverage."""
        for path in ("https://github.com/o/r/issues/1", "../secret", "/tmp/source", "src\\file.rs"):
            with self.subTest(path=path):
                report = deepcopy(self.report)
                report["sources"][0]["path"] = path
                with self.assertRaises(design.ReviewError) as caught:
                    design.validate_evidence(report, self.requirements)
                self.assertEqual(caught.exception.field, "sources[0].path")
                self.assertIn("repository-relative", caught.exception.detail)
                self.assertNotIn(path, caught.exception.detail)

    def test_valid_ids_do_not_bypass_quotes_or_substantive_verdict_checks(self):
        """Fixing reference syntax neither invents evidence nor requires approval."""
        design.validate_evidence(self.report, self.requirements, files={"CONTRIBUTING.md": "Use docs."})
        self.report["sources"][0]["quote"] = "Invented quote"
        with self.assertRaises(design.ReviewError) as caught:
            design.validate_evidence(self.report, self.requirements, files={"CONTRIBUTING.md": "Use docs."})
        self.assertEqual(caught.exception.code, "review_citation")
        self.report["assessments"][0]["status"] = "uncertain"
        with self.assertRaisesRegex(Blocked, "approval contradicts"):
            design.validate_evidence(self.report, self.requirements)

    def test_schema_explains_reference_relationship(self):
        """The provider sees the relationship before its first report."""
        props = json.loads((ROOT / "bundle/schemas/plan_qc.json").read_text())["properties"]
        self.assertIn("sources[].id", props["assessments"]["items"]["properties"]["sources"]["description"])
        self.assertIn("repository-relative", props["sources"]["items"]["properties"]["path"]["description"])


class PlannerFeedbackTests(PipelineFixture, unittest.TestCase):
    """Surface simultaneous plan failures within the existing correction allowance."""

    def setUp(self):
        """Pin opt-in limits for the historical pilot-shaped correction fixtures."""
        super().setUp()
        self.config["limits"] = {"primary_files": 8, "scope_paths": 8, "changed_files": 32}

    def invalid_plan(self):
        """Reproduce the independent violations discovered across #33's three outputs."""
        plan = deepcopy(PLAN)
        plan["publication"]["changes"] = [""]
        plan["files"] = [f"docs/{i}.md" for i in range(9)] + ["docs/rpc/openapi.yaml"]
        plan["scope"] = {"paths": [f"docs/{i}.md" for i in range(11)], "max_files": 12}
        return plan

    def test_simultaneous_limits_are_reported_together(self):
        """One correction can address summary, file, path-policy and envelope failures."""
        with self.assertRaises(Blocked) as caught:
            validate_plan(self.invalid_plan(), self.config)
        reason = str(caught.exception)
        for part in ("publication.changes[0]", "limits.primary_files", "files[9]", "scope paths"):
            self.assertIn(part, reason)
        self.assertNotIn("x" * 301, reason)

    def test_scope_path_contract_matches_validation_boundary(self):
        """The advertised path length and enforced boundary must stay aligned."""
        limit = plan_output_contract(CONFIG)["limits"]["scope_path_characters"]
        self.assertEqual(limit, 200)
        report = deepcopy(PLAN)
        report["scope"] = {"paths": ["docs/**", "x" * limit], "max_files": 1}
        validate_plan(report, CONFIG)
        report["scope"]["paths"][1] += "x"
        with self.assertRaisesRegex(Blocked, "at most 200 characters"):
            validate_plan(report, CONFIG)

    def test_malformed_scope_keeps_independent_feedback(self):
        """JSON shape errors in scope must not discard publication and file diagnostics."""
        scalars = (False, 1, "scope", [], {})
        scopes = [*scalars, *({"paths": value, "max_files": 12} for value in (*scalars, [None], [{}], [[]]))]
        scopes += [{"paths": ["docs/**"], "max_files": value} for value in (None, *scalars)]
        for scope in scopes:
            with self.subTest(scope=scope):
                report = self.invalid_plan()
                report["scope"] = scope
                with self.assertRaises(Blocked) as caught:
                    validate_plan(report, CONFIG)
                reason = str(caught.exception)
                for part in ("publication.changes[0]", "files[9]", "plan scope"):
                    self.assertIn(part, reason)

    def test_context_and_reloaded_correction_include_the_full_contract(self):
        """The first call gets policy/limits, and one rejected draft still spends one correction."""
        self.reach("plan")
        original = self.agents.ask

        def answer(provider, stage, context):
            """Return the pilot-shaped failure only on the first planning attempt."""
            result = original(provider, stage, context)
            return self.invalid_plan() if stage == "plan" and "plan_validation_feedback" not in context else result

        with patch.object(self.agents, "ask", side_effect=answer):
            rejected = self.tick()
            self.assertEqual(rejected.state["plan_draft"]["attempt"], 1)
            self.assertNotIn("approval", rejected.state)
            accepted = self.tick()
        contexts = [c for _, stage, c in self.agents.calls if stage == "plan"]
        self.assertEqual(len(contexts), 2)
        contract = contexts[0]["plan_output_contract"]
        self.assertEqual(contract["limits"]["primary_files"], 8)
        self.assertEqual(contract["limits"]["scope_paths"], 8)
        self.assertEqual(contract["limits"]["changed_files"], 32)
        self.assertNotIn("publication_item_characters_max", contract)
        self.assertEqual(contract["edit_policy"]["paths"], self.config["paths"])
        self.assertEqual(contract, contexts[1]["plan_output_contract"])
        self.assertIn("files[9]", contexts[1]["plan_validation_feedback"]["reason"])
        self.assertIn("scope paths", contexts[1]["plan_validation_feedback"]["reason"])
        self.assertEqual(accepted.state["stage"], "plan_qc")
        self.assertEqual(accepted.state["calls"], rejected.state["calls"] + 1)
        self.assertEqual(accepted.state["plan_revisions"], 0)

    def test_publication_schema_matches_runtime_boundaries(self):
        """Nonempty publication prose remains valid beyond the old length ceiling."""
        props = json.loads((ROOT / "bundle/schemas/plan.json").read_text())["properties"]
        for name in ("changes", "review_focus"):
            field = props["publication"]["properties"][name]
            self.assertEqual(field["minItems"], 1)
            self.assertNotIn("maxItems", field)
            self.assertEqual(field["items"]["minLength"], 1)
            self.assertNotIn("maxLength", field["items"])
            for length, valid in ((0, False), (300, True), (301, True), (10000, True)):
                report = deepcopy(PLAN)
                report["publication"][name] = ["x" * length]
                if valid:
                    validate_plan(report, CONFIG)
                else:
                    with self.assertRaises(Blocked):
                        validate_plan(report, CONFIG)

    def test_clarification_does_not_grant_path_permission(self):
        """Questions can describe a policy conflict, but removing them cannot authorize YAML."""
        report = deepcopy(PLAN)
        report.update(files=["docs/rpc/openapi.yaml"], questions=["This requires a target-policy decision."])
        validate_plan(report, CONFIG)
        report["questions"] = []
        with self.assertRaisesRegex(Blocked, "protected or unsupported"):
            validate_plan(report, CONFIG)


class ReviewCorrectionFlowTests(PipelineFixture, unittest.TestCase):
    """The production batch path retains diagnostics, reports and call accounting."""

    @legacy_events
    def test_parallel_correction_delivers_specific_feedback_and_keeps_peer_vote(self):
        """A corrected source ID is accepted once without restarting the sibling."""
        self.config["autonomous"] = True
        self.agents = BatchAgents()
        self.reach("plan_qc")
        first, second = [m["provider"] for m in design_roster(self.config)]

        def invalid(report):
            """Reproduce the path-as-ID report without changing the verdict."""
            report["assessments"][0]["sources"] = ["CONTRIBUTING.md:1"]
            return report

        self.agents.effects[first] = [invalid]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "implement")
        calls = [c for p, stage, c in self.agents.calls if stage == "plan_qc" and p == first]
        feedback = calls[-1]["review_validation_feedback"]
        self.assertEqual(feedback["field"], "assessments[0].sources[0]")
        self.assertIn("sources[].id", feedback["detail"])
        self.assertIn("approval is never required", feedback["instruction"])
        self.assertEqual(sum(p == second and stage == "plan_qc" for p, stage, _ in self.agents.calls), 1)
        diagnostic = next(r for r in controller.state["reports"] if r.get("diagnostic"))
        self.assertEqual(diagnostic["diagnostic"]["field"], feedback["field"])
        self.assertIn("Use exact IDs declared", self.gh.artifacts[7][diagnostic["artifact"]]["artifact"]["body"])
        self.assertEqual(controller.state["calls"], 5)
        self.assertEqual(controller.state["review_repairs"], 1)
        self.assertTrue(all(v["first_pass"] for v in controller.state["design_votes"].values()))

    def test_repeated_invalid_reference_still_escalates_and_retains_peer_approval(self):
        """Better feedback does not reset allowances or accept a repeated invalid approval."""
        self.config["autonomous"] = True
        self.agents = BatchAgents()
        self.reach("plan_qc")
        first, second = [m["provider"] for m in design_roster(self.config)]

        def initial(report):
            """Start with a short path instead of the declared source ID."""
            report["assessments"][0]["sources"] = ["guide.md:1"]
            return report

        def repeated(report):
            """Repeat the same invalid correction, as in pilot #36."""
            report["assessments"][0]["sources"] = ["docs/guide.md:1"]
            return report

        self.agents.effects[first] = [initial, repeated, repeated]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "escalated")
        self.assertEqual(controller.state["calls"], 6)
        self.assertEqual(controller.state["review_repairs"], 2)
        self.assertNotIn("approval", controller.state)
        votes = list(controller.state["design_votes"].values())
        self.assertEqual([v["identity"]["provider"] for v in votes], [second])
        self.assertEqual(len([r for r in controller.state["reports"] if r.get("diagnostic")]), 3)
        reloaded = self.load()
        self.assertEqual(reloaded.state["calls"], 6)
        self.assertEqual(reloaded.state["design_votes"], controller.state["design_votes"])
