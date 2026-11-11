"""Realistic citation, review capacity and durable reviewer-repair regressions."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import design
from controller import Controller
from fakes import CONFIG, FakeAgents, FakeGitHub, decision, design_report, sized_report
from pipeline_fixtures import PipelineFixture
from policy import Blocked, approval_valid, validate_qc
from repository import source_lines


class ReviewEvidenceTests(unittest.TestCase):
    """Check exact source provenance without confusing excerpts with whole source lines."""

    def test_fragments_indentation_crlf_and_multiline_quotes(self):
        """A literal fragment may start within its cited line, never on a different line."""
        lines = source_lines("header\r\n    let merge_enabled = false;\r\n    next();\r\n")
        self.assertTrue(design.citation_matches(lines, 2, "let merge_enabled = false;"))
        self.assertTrue(design.citation_matches(lines, 2, "merge_enabled"))
        self.assertTrue(design.citation_matches(lines, 2, "false;\r\n    next();"))
        for line, quote in (
            (1, "merge_enabled"),
            (3, "merge_enabled"),
            (0, "header"),
            (4, "next"),
            (2, "false;\nnext();"),
            (2, "true"),
        ):
            self.assertFalse(design.citation_matches(lines, line, quote))
        self.assertTrue(design.citation_matches(source_lines("a\u2028b\nnext"), 1, "b"))
        self.assertFalse(design.citation_matches(source_lines("a\u2028b\nnext"), 2, "b"))

    def test_unique_citations_move_but_ambiguous_or_missing_quotes_do_not(self):
        """Relocation preserves exact text, LF numbering and valid existing anchors."""
        lines = source_lines("header\r\n    first();\r\n    second();\r\n")
        self.assertEqual(design.citation_line(lines, 3, "first();"), 2)
        self.assertEqual(design.citation_line(lines, 999, "first();\n    second();"), 2)
        self.assertIsNone(design.citation_line(lines, 1, "first();\nsecond();"))
        self.assertIsNone(design.citation_line(lines, 1, "invented"))
        self.assertIsNone(design.citation_line(["repeat", "repeat"], 99, "repeat"))
        self.assertIsNone(design.citation_line(["repeat repeat"], 99, "repeat"))
        self.assertIsNone(design.citation_line(["a", "b", "a", "b"], 99, "a\nb"))
        self.assertEqual(design.citation_line(["repeat", "repeat"], 2, "repeat"), 2)
        self.assertEqual(design.citation_line(source_lines("a\u2028b\nlast"), 2, "b"), 1)

    def test_headings_courtesy_repetition_and_substantive_short_answers(self):
        """Courtesy does not spend criteria; yes/no, constraints and substantive headings remain."""
        request = {
            "title": "Guide",
            "body": "# Acceptance criteria\n\n- Preserve this behavior.\n\nValidation:\n\n2. Check it.",
            "discussion": [
                {"body": text}
                for text in ["Thanks!"] * 40
                + ["Yes.", "No.", "Thanks, but keep merging disabled.", "# Never enable merging", "Yes."]
            ],
        }
        parts = [r["text"] for r in design.criteria(request)]
        self.assertEqual(len(parts), 7)
        self.assertIn("Yes.", parts)
        self.assertIn("No.", parts)
        self.assertIn("Thanks, but keep merging disabled.", parts)
        self.assertIn("# Never enable merging", parts)
        self.assertNotIn("Validation:", parts)
        request["discussion"] = [{"body": str(i)} for i in range(35)]
        self.assertGreater(len(design.criteria(request)), 32)

    def test_large_review_is_allowed_but_schema_and_actual_byte_bounds_apply(self):
        """Thirteen useful criteria can exceed the old fixed cap without being invalid."""
        request = {"title": "Guide", "body": "\n\n".join(f"- Requirement {i}" for i in range(12))}
        context = {"review_criteria": design.criteria(request)}
        report = sized_report(context)
        self.assertGreater(len(json.dumps(report).encode()), 12000)
        validate_qc(report, "plan_qc")
        design.validate_evidence(report, request, files={"CONTRIBUTING.md": "Use docs."})
        oversized = deepcopy(report)
        oversized["assessments"][0]["reason"] += "x" * 10000
        design.validate_evidence(oversized, request)
        oversized["sources"][0]["id"] = "x" * 81
        for item in oversized["assessments"]:
            item["sources"] = [oversized["sources"][0]["id"]]
        design.validate_evidence(oversized, request)
        unicode_report = deepcopy(report)
        for assessment in unicode_report["assessments"]:
            assessment["reason"] = "\U0001f600" * 400
        validate_qc(unicode_report, "plan_qc")


class ReviewerRepairTests(PipelineFixture, unittest.TestCase):
    """Advance only fake GitHub issues while measuring real checkpoint payloads."""

    def setUp(self):
        """Seed a supervised fixture with independent deterministic provider roles."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def reach(self, stage, phase=None):
        """Bound fixture setup and require the requested review phase when supplied."""
        for _ in range(15):
            controller = self.tick()
            if controller.state["stage"] == stage and (phase is None or controller.state["design_phase"] == phase):
                return controller
        self.fail("stage not reached")

    def test_large_source_uses_same_inspection_lines_without_a_size_gate(self):
        """A source above 128 KB is valid citation evidence, including a fragment after indentation."""
        path = "stackslib/source.rs"
        source = "padding\n" * 20000 + "    let merge_enabled = false;\r\n"
        self.gh.trees[self.gh.base][path] = source
        self.reach("plan_qc")
        original = self.agents.ask
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir()
            target.write_text(source)
            self.assertEqual(source_lines(target.read_text())[20000], "    let merge_enabled = false;")

        def citing(provider, stage, context):
            """Cite the same pinned line that native source inspection returned."""
            report = original(provider, stage, context)
            report["sources"][0].update(path=path, line=20001, quote="let merge_enabled = false;")
            return report

        with patch.object(self.agents, "ask", side_effect=citing):
            controller = self.tick()
        self.assertEqual(len(controller.state["design_votes"]), 1)
        self.assertNotIn("review_repair", controller.state)
        self.assertEqual(controller.files(self.gh.base, [path])[path], source)
        self.gh.trees[self.gh.base][path] = "x" * 2_000_001
        controller.trees.clear()
        self.assertEqual(len(controller.files(self.gh.base, [path], inspection=True)[path]), 2_000_001)

    def test_cooperative_repair_retains_session_and_spent_calls_without_peer_input(self):
        """An inaccurate citation is corrected by its owner before any vote is accepted."""
        self.reach("plan_qc")
        original = self.agents.ask

        def bad(provider, stage, context):
            """Return an unsupported literal on only the first attempt."""
            report = original(provider, stage, context)
            report["sources"][0]["quote"] = "fabricated"
            return report

        with patch.object(self.agents, "ask", side_effect=bad):
            before = self.tick().state
        self.assertFalse(before["design_votes"])
        self.assertEqual(before["review_repairs"], 1)
        after = self.tick().state
        contexts = [c for _, stage, c in self.agents.calls if stage == "plan_qc"]
        self.assertEqual(contexts[0]["controller_session"]["scope"], contexts[1]["controller_session"]["scope"])
        self.assertEqual(contexts[1]["controller_session"]["turn"], 1)
        self.assertEqual(contexts[1]["review_validation_feedback"]["code"], "review_citation")
        self.assertEqual(contexts[1]["review_validation_feedback"]["field"], "sources[0]")
        self.assertNotIn("design_votes", contexts[1])
        self.assertEqual(after["calls"], before["calls"] + 1)
        self.assertEqual(after["plan_revisions"], 0)
        self.assertEqual(len(after["design_votes"]), 1)
        self.assertNotIn("review_repair", after)
        self.tick()
        self.assertNotIn("review_validation_feedback", self.agents.calls[-1][2])

    def test_fresh_correction_is_independent_and_cannot_approve_incomplete_evidence(self):
        """Fresh corrections get only their own fixed diagnostic, never cooperative history."""
        self.agents.qc_reports = [
            decision("proceed"),
            decision("approve"),
            decision("revise", concerns=["Add an error-path regression."]),
        ]
        self.reach("plan_qc", "fresh")
        original = self.agents.ask

        def missing(provider, stage, context):
            """Omit one assessment from an otherwise approving response."""
            report = original(provider, stage, context)
            report["assessments"].pop()
            return report

        with patch.object(self.agents, "ask", side_effect=missing):
            state = self.tick().state
        self.assertFalse(approval_valid(state, self.config))
        self.tick()
        context = self.agents.calls[-1][2]
        self.assertEqual(context["review_phase"], "fresh")
        for key in ("controller_session", "own_objections", "design_feedback", "design_votes"):
            self.assertNotIn(key, context)
        self.assertNotIn("responses", context["plan"])
        self.assertIn("review_validation_feedback", context)

    def test_new_objection_errors_get_actionable_feedback_then_record_the_dissent(self):
        """Pilot #6's invented ID and missing dispositions can be corrected without losing the concern."""
        for failure, code, field in (
            ("invented_id", "review_objection_id", "objection_updates[0].id"),
            ("missing_updates", "review_concerns", "objection_updates"),
        ):
            with self.subTest(failure=failure):
                self.setUp()
                self.reach("plan_qc")
                original = self.agents.ask
                concern = "Specify stable slot reuse and partial-flush recovery."

                def review(provider, stage, context):
                    """Correct the malformed objection only after receiving its specific diagnostic."""
                    original(provider, stage, context)
                    report = design_report(decision("revise", concerns=[concern]), context)
                    if "review_validation_feedback" not in context:
                        if failure == "invented_id":
                            report["objection_updates"][0]["id"] = "D1"
                        else:
                            report["objection_updates"] = []
                    else:
                        feedback = context["review_validation_feedback"]
                        self.assertEqual((feedback["code"], feedback["field"]), (code, field))
                        self.assertIn('id ""', feedback["detail"])
                    return report

                with patch.object(self.agents, "ask", side_effect=review):
                    rejected = self.tick().state
                    self.assertFalse(rejected["design_votes"])
                    accepted = self.tick().state
                vote = next(iter(accepted["design_votes"].values()))
                self.assertEqual(vote["report"]["decision"], "revise")
                self.assertEqual(accepted["review_repairs"], 1)
                self.assertNotIn("review_repair", accepted)
                self.assertEqual(accepted["objections"][0]["text"], concern)
                self.assertTrue(accepted["objections"][0]["id"].startswith("O1-"))
                self.assertFalse(accepted["objections"][0]["resolved"])
                self.assertFalse(approval_valid(accepted, self.config))
                self.assertEqual(self.tick().state["stage"], "plan")

    def test_repeat_or_per_review_budget_stops_and_writer_retry_refills(self):
        """A retry clears both exhaustion and repeat detection, once per writer command."""
        for repeat in (True, False):
            with self.subTest(repeat=repeat):
                self.setUp()
                self.config["max_review_repairs"] = 3 if repeat else 1
                self.reach("plan_qc")
                original = self.agents.ask

                def bad(provider, stage, context):
                    """Vary the failed source only for the budget exhaustion case."""
                    report = original(provider, stage, context)
                    report["sources"][0]["quote"] = "wrong" if repeat else str(len(self.agents.calls))
                    return report

                with patch.object(self.agents, "ask", side_effect=bad):
                    self.tick()
                    stopped = self.tick().state
                    self.gh.comment("/concurro retry")
                    retried = self.tick().state
                    stopped_again = self.tick().state
                self.assertEqual(stopped["stage"], "escalated")
                self.assertEqual(retried["stage"], "plan_qc")
                self.assertEqual(retried["review_repair"]["attempts"], 1)
                self.assertEqual(retried["review_repairs"], 2)
                self.assertEqual(stopped_again["stage"], "escalated")
                self.assertEqual(stopped_again["review_repairs"], 2)
                self.assertEqual(retried["calls"], stopped["calls"] + 1)
                self.assertFalse(retried["design_votes"])
                self.assertEqual(retried["plan_revisions"], 0)

    def test_each_reviewer_and_phase_has_its_own_allowance(self):
        """Cooperative and fresh reviewers can each correct one error across restarts."""
        self.config["max_review_repairs"] = 1
        # A substantive revision (not output correction) requires the fresh phase.
        self.agents.qc_reports = [
            decision("proceed"),
            decision("approve"),
            decision("revise", concerns=["Add an error-path regression."]),
        ]
        self.reach("plan_qc")
        self.reach("plan")
        self.reach("plan_qc")
        original = self.agents.ask
        seen = set()

        def review(provider, stage, context):
            """Fail once per identity and phase, then return the ordinary approval."""
            report = original(provider, stage, context)
            key = (context["reviewer_identity"]["id"], context["review_phase"])
            if key not in seen:
                seen.add(key)
                self.assertNotIn("review_validation_feedback", context)
                report["sources"][0]["quote"] = "fabricated"
            else:
                self.assertEqual(context["review_validation_feedback"]["code"], "review_citation")
            return report

        with patch.object(self.agents, "ask", side_effect=review):
            controller = self.reach("implement")
        self.assertEqual(len(seen), 4)
        self.assertEqual(controller.state["review_repairs"], 4)
        self.assertEqual(controller.state["design_phase"], "fresh")
        self.assertTrue(approval_valid(controller.state, self.config))

    def test_legacy_task_count_does_not_exhaust_a_new_review(self):
        """Old checkpoints retain usage history without spending a new review's allowance."""
        self.config["max_review_repairs"] = 1
        controller = self.reach("plan_qc")
        controller.state["review_repairs"] = 4
        controller.save()
        original = self.agents.ask

        def bad(provider, stage, context):
            """Return an error that needs a provider correction."""
            report = original(provider, stage, context)
            report["sources"][0]["quote"] = "fabricated"
            return report

        with patch.object(self.agents, "ask", side_effect=bad):
            state = self.tick().state
        self.assertEqual(state["stage"], "plan_qc")
        self.assertEqual(state["review_repair"]["attempts"], 1)
        self.assertEqual(state["review_repairs"], 5)
        self.assertEqual(len(self.tick().state["design_votes"]), 1)

    def test_repair_charge_survives_a_crash_without_double_spending(self):
        """Replaying a retained invalid response charges one correction and makes no new call."""
        self.reach("plan_qc")
        original, save = self.agents.ask, Controller.save

        def bad(provider, stage, context):
            """Retain an invalid response before the repair bookkeeping is saved."""
            report = original(provider, stage, context)
            report["sources"][0]["quote"] = "fabricated"
            return report

        def crash(controller, *args, **kwargs):
            """Interrupt the first save containing a correction allowance."""
            if controller.state.get("review_repair"):
                raise RuntimeError("fixture crash")
            return save(controller, *args, **kwargs)

        with patch.object(self.agents, "ask", side_effect=bad), patch.object(Controller, "save", crash):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.tick()
        calls = len(self.agents.calls)
        state = self.tick().state
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(state["review_repairs"], 1)
        self.assertEqual(state["review_repair"]["attempts"], 1)
        self.assertEqual(len(self.tick().state["design_votes"]), 1)

    def test_corrected_citation_is_saved_and_archived_after_crash_replay(self):
        """Unique wrong-line quotes recover deterministically without a correction call."""
        self.gh.trees[self.gh.base]["CONTRIBUTING.md"] = "Header\nUse docs.\nOther.\n"
        self.reach("plan_qc")
        original, save = self.agents.ask, Controller.save

        def review(provider, stage, context):
            """Cite the real line's text with the pilot's one-line offset."""
            report = original(provider, stage, context)
            report["sources"][0]["line"] = 3
            return report

        def crash(controller, *args, **kwargs):
            """Stop after correction but before the vote is durably saved."""
            if controller.state.get("design_votes"):
                raise RuntimeError("fixture crash")
            return save(controller, *args, **kwargs)

        with patch.object(self.agents, "ask", side_effect=review), patch.object(Controller, "save", crash):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.tick()
        calls = len(self.agents.calls)
        state = self.tick().state
        self.assertEqual(len(self.agents.calls), calls)
        vote = next(iter(state["design_votes"].values()))
        self.assertEqual(vote["report"]["sources"][0]["line"], 2)
        self.assertFalse(state.get("review_repairs"))
        self.assertNotIn("review_repair", state)
        archives = [body for body in self.gh.archives() if '"sources"' in body]
        archived = json.loads(archives[-1].split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(archived["sources"][0]["line"], 2)

    def test_retry_cannot_refill_task_calls_or_a_disabled_correction_allowance(self):
        """Explicit retries never bypass the configured call ceiling or a zero allowance."""
        for limited_calls in (False, True):
            with self.subTest(limited_calls=limited_calls):
                self.setUp()
                self.config["max_review_repairs"] = 0
                if limited_calls:
                    self.config["max_model_calls"] = 3
                self.reach("plan_qc")
                original = self.agents.ask

                def bad(provider, stage, context):
                    """Request correction without claiming a valid source."""
                    report = original(provider, stage, context)
                    report["sources"][0]["quote"] = "fabricated"
                    return report

                with patch.object(self.agents, "ask", side_effect=bad):
                    stopped = self.tick().state
                    self.assertEqual(stopped["stage"], "escalated")
                    self.gh.comment("/concurro retry")
                    calls = len(self.agents.calls)
                    if limited_calls:
                        with self.assertRaisesRegex(Blocked, "model-call budget"):
                            self.tick()
                        self.assertEqual(len(self.agents.calls), calls)
                    else:
                        retried = self.tick().state
                        self.assertEqual(retried["stage"], "escalated")
                        self.assertEqual(retried["review_repair"]["attempts"], 0)
                        self.assertFalse(retried.get("review_repairs"))

    def test_task_call_limit_still_preempts_reviewer_repairs(self):
        """A repair allowance cannot authorize an attempt after the task call ceiling."""
        self.config["max_model_calls"] = 3
        self.reach("plan_qc")
        original = self.agents.ask

        def bad(provider, stage, context):
            """Use the last available attempt for an invalid quote."""
            report = original(provider, stage, context)
            report["sources"][0]["quote"] = "wrong"
            return report

        with patch.object(self.agents, "ask", side_effect=bad):
            self.tick()
        calls = len(self.agents.calls)
        with self.assertRaisesRegex(Blocked, "model-call budget"):
            self.tick()
        self.assertEqual(len(self.agents.calls), calls)

    def test_source_transport_failure_is_not_mislabeled_as_reviewer_output(self):
        """An unavailable GitHub read needs infrastructure recovery, not another model response."""
        self.reach("plan_qc")
        original = Controller.files

        def unavailable(controller, sha, paths, inspection=False):
            """Fail only the evidence read after the provider has returned."""
            if inspection:
                raise RuntimeError("fixture transport unavailable")
            return original(controller, sha, paths, inspection)

        with patch.object(Controller, "files", unavailable), self.assertRaises(RuntimeError):
            self.tick()
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        self.assertEqual(controller.state["calls"], 3)
        self.assertFalse(controller.state.get("review_repairs"))
        self.assertFalse(controller.state["design_votes"])

    def test_revision_and_exhaustion_transitions_fit_checkpoint_with_full_findings(self):
        """Design feedback, archived objections and terminal outcomes are included in capacity checks."""
        for allowance in (0, 2):
            with self.subTest(allowance=allowance):
                self.setUp()
                self.config["max_plan_revisions"] = allowance
                self.reach("plan_qc")
                original, repo = self.agents.ask, self.gh.repo
                sizes = []

                def objections(provider, stage, context):
                    """Both independent reviewers retain concrete findings in a long report."""
                    original(provider, stage, context)
                    concerns = [f"Finding {i}: " + "The plan misses required behavior. " * 8 for i in range(4)]
                    return design_report(decision("revise", concerns=concerns), context)

                def measure(path, method="GET", data=None):
                    """Include the actual transition and pending event in every measured write."""
                    if "/artifacts" in path and method in ("POST", "PATCH"):
                        sizes.append(len(data["body"].encode()))
                    return repo(path, method, data)

                with (
                    patch.object(self.agents, "ask", side_effect=objections),
                    patch.object(self.gh, "repo", side_effect=measure),
                ):
                    self.tick()
                    state = self.tick().state
                self.assertEqual(state["stage"], "plan" if allowance else "escalated")
                self.assertEqual(len(state["design_feedback"]), 2)
                self.assertLessEqual(max(sizes), 60000)
                self.assertFalse(state.get("review_repairs"))

    def test_realistic_and_maximum_criteria_fit_every_checkpoint_through_consensus(self):
        """Both reviewers receive a usable budget and compact wording before any oversized save."""
        for count in (13, 32):
            with self.subTest(count=count):
                self.setUp()
                self.gh.issue["body"] = "Acceptance criteria:\n\n" + "\n\n".join(
                    f"- Requirement {i}" for i in range(count - 1)
                )
                self.reach("plan_qc")
                original, repo = self.agents.ask, self.gh.repo
                sizes, seen = [], []

                def wordy(provider, stage, context):
                    """Simulate a reviewer choosing concise wording using its explicit budget."""
                    report = original(provider, stage, context)
                    if stage == "plan_qc":
                        report = sized_report(context)
                        seen.append(len(report["assessments"]))
                    return report

                def measure(path, method="GET", data=None):
                    """Measure actual transport payloads, including each pending conversation event."""
                    if "/artifacts" in path and method in ("POST", "PATCH"):
                        sizes.append(len(data["body"].encode()))
                    return repo(path, method, data)

                with (
                    patch.object(self.agents, "ask", side_effect=wordy),
                    patch.object(self.gh, "repo", side_effect=measure),
                ):
                    controller = self.reach("implement")
                self.assertEqual(seen, [count] * 2)
                self.assertLessEqual(max(sizes), 60000)
                self.assertTrue(approval_valid(controller.state, self.config))
                self.assertFalse(controller.state.get("review_repairs"))

    def test_full_review_survives_checkpoint_pressure_without_a_correction(self):
        """A complete valid response cannot crowd out the checkpoint or correction notice."""
        self.gh.issue["body"] = "\n\n".join(f"- Requirement {i}" for i in range(12))
        controller = self.reach("plan_qc")
        controller.state["fixture_padding"] = "x" * 28000
        controller.save()
        original = self.agents.ask

        def wordy(provider, stage, context):
            """Return a schema-valid response exceeding the remaining checkpoint space."""
            original(provider, stage, context)
            return sized_report(context)

        with (
            patch.object(self.agents, "ask", side_effect=wordy),
            patch.object(Controller, "review_output_budget", return_value={"max_json_bytes": 32000}),
        ):
            state = self.tick().state
        self.assertNotIn("review_repair", state)
        self.assertEqual(len(state["design_votes"]), 1)
        recovered = Controller(self.gh, self.agents, self.config)
        recovered.issue = self.gh.issue
        recovered.load(self.gh.comments)
        self.assertEqual(recovered.state["design_votes"], state["design_votes"])
        self.assertLessEqual(len(recovered.checkpoints.audit_body(recovered.state).encode()), 60000)
