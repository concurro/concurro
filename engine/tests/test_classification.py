"""Advisory ratings never authorize work, consume extra calls, or change published PR text."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, TARGET, setUpModule as setUpModule, tearDownModule as tearDownModule

import classification as ratings
import conversation
import design
import reviews
from bundle import ENGINE_RESOURCES, Bundle
from checkpoints import LIMIT, Checkpoints
from fakes import APPROVE, ClassifiedAgents, FakeGitHub, assessment, design_report, intake, named_config
from model_config import resolve
from pipeline_fixtures import PipelineFixture
from policy import (
    MODEL_RESPONSE_LIMIT,
    Blocked,
    approval_binding,
    consensus_valid,
    digest,
    validate_pr_review,
    validate_qc,
)


class ContractTests(unittest.TestCase):
    """Classification normalization and projection are independent of core validity."""

    def test_missing_invalid_and_oversized_metadata_never_change_core_validity(self):
        for value in (
            None,
            [],
            {},
            assessment(risk="critical"),
            assessment(unknowns="bad"),
            assessment(rationale="\ud800"),
            assessment(rationale="<details>" * 20000, unknowns=["a" * 900] * 50),
        ):
            with self.subTest(value=type(value).__name__):
                report = intake(classification=value)
                validate_qc(report, "intake")
                normalized = ratings.normalize(value)
                self.assertLessEqual(ratings.size(normalized), ratings.MAX_BYTES)
                self.assertIn("diagnostic", normalized)
                self.assertEqual(ratings.core(report), intake())
        with self.assertRaisesRegex(Blocked, "fields"):
            validate_qc(intake(unexpected=True, classification=assessment()), "intake")
        with self.assertRaises(Blocked):
            validate_qc(intake(questions=["Human decision?"], classification=assessment()), "intake")

    def test_core_size_limits_exclude_only_classification(self):
        report = intake()
        report["rationale"] = "x" * (5999 - len(json.dumps(report)) + len(report["rationale"]))
        validate_qc({**report, "classification": assessment(rationale="x" * 50000)}, "intake")
        report["rationale"] += "xx"
        with self.assertRaises(Blocked):
            validate_qc(report, "intake")
        review = {**APPROVE, "dispositions": []}
        review["summary"] = "x" * (MODEL_RESPONSE_LIMIT - len(json.dumps(review)) + len(review["summary"]))
        classified = {**review, "classification": assessment(rationale="x" * 100000)}
        validate_pr_review(classified, [], [])
        review["summary"] += "x"
        validate_pr_review(review, [], [])

    def test_normalization_clips_evidence_and_never_trusts_provenance(self):
        value = assessment(rationale="🌍" * 1000, unknowns=["é" * 300] * 9)
        normalized = ratings.normalize(value)
        self.assertLessEqual(ratings.size(normalized), ratings.MAX_BYTES)
        self.assertLessEqual(len(normalized["rationale"]), 600)
        self.assertLessEqual(len(normalized["unknowns"]), 5)
        report = intake(classification={**value, "source": {"agent": "forged"}})
        state = {"operation": {"id": "op", "attempt": "attempt"}, "requirements": {}, "base": "base"}
        result = ratings.attach(report, state, {"profile": "real"}, "intake", 4096)
        self.assertEqual(result["classification"]["source"]["agent"], {"profile": "real"})
        self.assertEqual(result["classification"]["source"]["report"], "op:attempt")
        self.assertNotIn("classification", ratings.attach(report, state, {}, "intake", 0))

    def test_bundle_composes_one_required_self_contained_fragment(self):
        bundle = Bundle(target=TARGET)
        self.assertIn("bundle/schemas/classification.json", ENGINE_RESOURCES)
        for stage in ("intake", "plan_qc", "review"):
            schema = bundle.schema(stage)
            self.assertIn("classification", schema["required"])
            field = schema["properties"]["classification"]
            self.assertEqual(field["type"], "object")
            self.assertFalse(field["additionalProperties"])
            self.assertEqual(set(field["required"]), ratings.FIELDS)
            self.assertNotIn("$ref", json.dumps(schema))
        for stage in ("plan", "develop"):
            self.assertNotIn("classification", bundle.schema(stage)["properties"])
        self.assertEqual(bundle.schema("intake")["required"].count("classification"), 1)

    def test_reducer_coverage_maxima_disagreement_and_lowering(self):
        binding = {"stage": "review", "head": "one"}
        a = {**assessment("low", "high"), "source": {"report": "a"}}
        b = {**assessment("high", "medium", confidence="medium"), "source": {"report": "b"}}
        result = ratings.reduce([a, b], 2, binding)
        self.assertEqual(result, ratings.reduce([b, a], 2, binding))
        self.assertTrue(result.complete)
        self.assertTrue(result.disagreement)
        self.assertEqual(
            (result.estimate.complexity, result.estimate.change_risk, result.estimate.confidence),
            ("high", "high", "medium"),
        )
        previous = result.estimate.record()
        current = {"stage": "review", "head": "two"}
        partial = ratings.reduce([assessment("low", "low")], 2, current, previous)
        self.assertTrue(partial.provisional)
        self.assertEqual(partial.estimate.change_risk, "high")
        self.assertEqual(partial.previous_binding, binding)
        unknown = ratings.reduce([assessment("unknown", "low"), assessment("low", "low")], 2, current, previous)
        self.assertFalse(unknown.complete)
        complete = ratings.reduce([assessment("low", "low")] * 2, 2, current, previous)
        self.assertEqual(complete.estimate.change_risk, "low")
        self.assertFalse(complete.provisional)


class LifecycleTests(PipelineFixture, unittest.TestCase):
    """Exercise report capture, publication, independent contexts and revision bindings."""

    def setUp(self):
        """Use the normal autonomous lifecycle with classified deterministic reports."""
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = True
        self.gh = FakeGitHub(self.config)
        self.agents = ClassifiedAgents()

    def ready(self):
        """Run a bounded number of normal ticks without changing the task's inputs."""
        states = []
        for _ in range(20):
            controller = self.tick()
            states.append(deepcopy(controller.state))
            if controller.state.get("handoff_ready"):
                return controller, states
        self.fail("fixture did not reach readiness")

    def test_simple_pipeline_classifies_without_extra_calls_or_pr_text(self):
        controller, states = self.ready()
        self.assertEqual(len(self.agents.calls), 7)
        self.assertFalse(self.gh.pr["draft"])
        snapshot = controller.state["classification_snapshot"]
        self.assertEqual(snapshot["binding"]["head"], controller.state["head"])
        self.assertEqual(snapshot["change_risk"], "high")
        self.assertTrue(all(s.get("artifact") for s in snapshot["sources"]))
        self.assertIn("Change risk: high", controller.summary_body())
        self.assertIn("reviewers disagree", controller.summary_body())
        self.assertTrue(any('"classification"' in body for body in self.gh.archives()))
        self.assertTrue(consensus_valid(controller.state, self.config))
        before = approval_binding(controller.state, self.config)
        for vote in controller.state["design_votes"].values():
            vote["report"]["classification"]["change_risk"] = "low"
        self.assertEqual(before, approval_binding(controller.state, self.config))
        self.assertTrue(consensus_valid(controller.state, self.config))
        for provider, report in controller.state["reviews"].items():
            item = {"format": 2, "provider": provider, "sequence": report["sequence"], "head": report["head"]}
            self.assertEqual(
                reviews.review_body(controller.state, self.config, item, report, [], {}),
                reviews.review_body(controller.state, self.config, item, ratings.core(report), [], {}),
            )
            self.assertNotIn("agent", report)  # Legacy comment formatting remains byte-identical.
            self.assertEqual(
                report["classification"]["source"]["agent"], resolve(self.config, provider, "review").record()
            )
        history = deepcopy(controller.state["classification_snapshot"])
        controller.state["stage"] = "observed_merged"
        controller.record_outcome()
        outcome = deepcopy(controller.state["outcomes"][-1]["classification_snapshot"])
        self.assertTrue(outcome.pop("fresh"))
        self.assertEqual(outcome, history)
        controller.record_outcome()
        self.assertEqual(len(controller.state["outcomes"]), 1)

    def test_repair_pipeline_retains_prior_head_until_both_reviewers_reassess(self):
        self.agents.fix = True
        controller, states = self.ready()
        self.assertEqual(len(self.agents.calls), 10)
        self.assertEqual(controller.state["round"], 1)
        old = next(s for s in states if s.get("round") == 1 and s["stage"] == "implement")
        self.assertTrue(ratings.view(old, self.config).provisional)
        new = next(s for s in states if s.get("round") == 1 and s["stage"] == "review" and len(s["reviews"]) == 1)
        view = ratings.view(new, self.config)
        self.assertTrue(view.provisional)
        self.assertEqual(view.estimate.change_risk, "high")
        self.assertNotEqual(view.previous_binding["head"], new["head"])
        self.assertEqual(len(controller.state["review_history"]), 1)
        self.assertTrue(ratings.view(controller.state, self.config).complete)
        self.assertEqual(controller.state["classification_snapshot"]["change_risk"], "low")
        for _, stage, context in self.agents.calls:
            if stage in ratings.STAGES:
                self.assertNotIn("classification_snapshot", json.dumps(context))
                self.assertNotIn("classification", context)

    def test_stale_plan_phase_and_bundle_do_not_count_as_current(self):
        controller, _ = self.ready()
        state = controller.state
        for key, value in [("head", "different"), ("input_revision", 999), ("bundle_id", "other")]:
            changed = {**state, key: value}
            view = ratings.view(changed, self.config)
            self.assertEqual(view.assessed, 0)
            self.assertTrue(view.provisional)
        state = deepcopy(state)
        state["stage"] = "plan_qc"
        state["design_phase"] = "fresh"
        self.assertEqual(ratings.view(state, self.config).assessed, 0)
        state["stage"] = "plan"
        state["plan_id"] = "reopened"
        self.assertEqual(ratings.view(state, self.config).assessed, 0)

    def test_named_profiles_and_effort_are_recorded_without_peer_hints(self):
        self.config = named_config()
        self.config["autonomous"] = True
        self.gh = FakeGitHub(self.config)
        controller, _ = self.ready()
        self.assertEqual(set(controller.state["reviews"]), {"sol-high", "astra-high"})
        for profile, report in controller.state["reviews"].items():
            self.assertEqual(
                report["classification"]["source"]["agent"], resolve(self.config, profile, "review").record()
            )
        controller.state["prior_outcomes"] = [
            {"classification_snapshot": {"change_risk": "high"}, "reason": "Useful lesson"}
        ]
        controller.state["stage"] = "intake"
        controller.quality_control("intake")
        context = self.agents.calls[-1][2]
        self.assertEqual(context["prior_outcomes_untrusted"], [{"reason": "Useful lesson"}])

    def test_invalid_metadata_and_missing_legacy_data_do_not_add_calls(self):
        original = self.agents.ask

        def ask(provider, stage, context):
            """Return invalid advisory data beside an otherwise valid core."""
            value = original(provider, stage, context)
            if stage in ratings.STAGES:
                value["classification"] = {"change_risk": "invented"}
            return value

        with patch.object(self.agents, "ask", side_effect=ask):
            controller, _ = self.ready()
        self.assertEqual(len(self.agents.calls), 7)
        self.assertNotIn("classification_snapshot", controller.state)
        self.assertTrue(ratings.view(controller.state, self.config).provisional)
        self.assertIn("Assessment incomplete", controller.summary_body())
        for report in controller.state["reviews"].values():
            self.assertIn("diagnostic", report["classification"])
        old = ratings.without_hints(controller.state)
        self.assertEqual(ratings.view(old, self.config).assessed, 0)

    def test_high_qc_risk_still_escalates(self):
        self.agents.qc_reports = [intake(risk="high", classification=assessment("low", "low"))]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "escalated")
        self.assertEqual(len(self.agents.calls), 1)

    def test_lost_report_archive_response_recovers_the_same_assessment(self):
        original, lost = self.gh.repo, []

        def repo(path, method="GET", data=None):
            """Persist the first report and lose only its acknowledgement."""
            result = original(path, method, data)
            if method == "POST" and "Intake QC" in (data or {}).get("body", "") and not lost:
                lost.append(result)
                raise RuntimeError("lost archive response")
            return result

        with patch.object(self.gh, "repo", side_effect=repo), self.assertRaises(conversation.PublicationError):
            self.tick()
        original_body = self.gh.archives()[0]
        controller = self.tick()
        self.assertEqual(sum(stage == "intake" for _, stage, _ in self.agents.calls), 1)
        self.assertEqual(self.gh.archives()[0], original_body)
        self.assertEqual(sum("Intake QC" in body for body in self.gh.archives()), 1)
        self.assertEqual(controller.state["classification_snapshot"]["sources"][0]["artifact"], lost[0]["number"])

    def test_compaction_and_oversized_metadata_preserve_core_and_replay(self):
        original = self.agents.ask

        def ask(provider, stage, context):
            """A huge optional rationale must not force a paid correction."""
            value = original(provider, stage, context)
            if stage == "intake":
                value["rationale"] = "Explained. " * 150
                value["classification"] = assessment(rationale="<details>" * 20000)
            return value

        with patch.object(self.agents, "ask", side_effect=ask):
            controller = self.tick()
        report = controller.state["qc"]["intake"]
        self.assertEqual(report["rationale"], "Explained. " * 150)
        metadata = deepcopy(report["classification"])
        conversation.compact(controller.state)
        self.assertEqual(report["classification"], metadata)
        self.assertEqual(len(self.agents.calls), 1)
        self.assertNotIn("<details><details>", controller.summary_body())
        self.assertIn("Classification evidence shortened", self.gh.archives()[0])
        self.assertLess(len(controller.checkpoints.audit_body(controller.state).encode()), LIMIT)

    def test_design_size_check_projects_metadata_before_paid_repairs(self):
        controller = self.tick()
        controller = self.tick()  # Plan ready for QC.
        context = controller.context()
        context["review_criteria"] = design.criteria(controller.state["requirements"])
        report = design_report(intake(decision="approve"), context)
        context["review_output_budget"] = {"max_json_bytes": ratings.size(report)}
        report["classification"] = assessment(rationale="x" * 100000)
        controller.validate_design_report(report, context)
        report["rationale"] += "a"
        controller.validate_design_report(report, context)

    def test_no_metadata_is_attached_when_core_uses_its_report_allowance(self):
        controller = self.tick()
        report = intake(classification=assessment())
        result = controller.classify(report, self.config["qc"], "intake", ratings.size(ratings.core(report)))
        self.assertEqual(result, ratings.core(report))

    def test_durable_completed_result_replays_without_another_assessment_call(self):
        original, lost = Checkpoints.save, []

        def save(checkpoints, state):
            """Crash after checkpointing the model response, before validating its verdict."""
            result = original(checkpoints, state)
            if state.get("operation", {}).get("status") == "completed" and not lost:
                lost.append(deepcopy(state["operation"]))
                raise RuntimeError("crashed after completion")
            return result

        with patch.object(Checkpoints, "save", save), self.assertRaises(RuntimeError):
            self.tick()
        controller = self.tick()
        self.assertEqual(len(self.agents.calls), 1)
        source = controller.state["qc"]["intake"]["classification"]["source"]
        self.assertEqual(source["report"], lost[0]["id"] + ":" + lost[0]["attempt"])
        self.assertEqual(controller.state["operation"]["status"], "consumed")

    def test_checkpoint_pressure_sheds_only_archived_metadata(self):
        controller, _ = self.ready()
        state = controller.state
        core_budget = controller.review_output_budget()["max_json_bytes"]
        self.assertGreater(core_budget, 0)
        before = deepcopy(state)
        archive_bodies = self.gh.archives()
        ratings.make_room(state, 0, ratings.size)
        self.assertNotIn("classification_snapshot", state)
        active = list(before["reviews"].values()) + list(before.get("qc", {}).values())
        active += [vote["report"] for vote in before["design_votes"].values()]
        protected = {r.get("classification", {}).get("source", {}).get("report") for r in active}
        self.assertEqual(state["reviews"], before["reviews"])
        self.assertEqual(state["design_votes"], before["design_votes"])
        projected = ratings.without_hints(before)
        for entry in projected["reports"]:
            if entry.get("assessment") not in protected:
                entry.pop("assessment", None)
        self.assertEqual(projected, ratings.without_hints(state))
        self.assertEqual(controller.review_output_budget()["max_json_bytes"], core_budget)
        self.assertEqual(archive_bodies, self.gh.archives())
        self.assertTrue(consensus_valid(state, self.config))
        # Historical-but-unarchived and pending reports are protected independently of current slots.
        for pending_kind in ("unarchived", "conversation", "pr_outbox"):
            with self.subTest(pending_kind=pending_kind):
                pending = deepcopy(before)
                provider = next(iter(pending["reviews"]))
                report = pending["reviews"][provider]
                expected = deepcopy(report)
                pending["reviews"] = {}
                pending["review_history"] = [{provider: report}]
                if pending_kind == "unarchived":
                    source = report["classification"]["source"]["report"]
                    pending["reports"] = [r for r in pending["reports"] if r.get("assessment") != source]
                elif pending_kind == "conversation":
                    pending["conversation"]["pending"] = [{"source": ["review_history", 0, provider]}]
                else:
                    pending["pr_outbox"] = [{"kind": "review", "provider": provider, "sequence": report["sequence"]}]
                ratings.make_room(pending, 0, ratings.size)
                self.assertEqual(pending["review_history"][0][provider], expected)

    def test_high_operational_risk_still_invalidates_design_consensus(self):
        controller, _ = self.ready()
        vote = next(iter(controller.state["design_votes"].values()))
        vote["report"]["risk"] = "high"
        self.assertFalse(consensus_valid(controller.state, self.config))

    def test_resolving_archive_links_updates_outcome_without_an_extra_event(self):
        controller, _ = self.ready()
        controller.state["stage"] = "observed_merged"
        snapshot = controller.state["classification_snapshot"]
        for source in snapshot["sources"]:
            source.pop("artifact", None)
        controller.record_outcome()
        count = len(controller.state["outcomes"])
        controller.refresh_classification()
        controller.record_outcome()
        self.assertEqual(len(controller.state["outcomes"]), count)
        self.assertTrue(
            all(s.get("artifact") for s in controller.state["outcomes"][-1]["classification_snapshot"]["sources"])
        )

    def test_metadata_pressure_cannot_discard_an_otherwise_retainable_result(self):
        controller = self.tick()
        state = controller.state
        state["operation"] = {
            "id": "next",
            "attempt": "a",
            "status": "completed",
            "stage": "intake",
            "response": intake(classification=ratings.normalize(assessment(rationale="x" * 600))),
        }
        controller.consuming = None
        state["padding"] = "x" * (LIMIT - 3900 - len(controller.checkpoints.audit_body(state).encode()))
        self.assertGreater(len(controller.checkpoints.audit_body(state).encode()), LIMIT - 4000)
        controller.checkpoint()
        self.assertIn("response", state["operation"])
        self.assertEqual(ratings.core(controller.recover(state["operation"])), intake())
        self.assertLessEqual(len(controller.checkpoints.audit_body(state).encode()), LIMIT - 1000)

    def test_legacy_design_provenance_uses_each_members_actual_model(self):
        self.config["qc_model"] = "separate-qc-model"
        controller, _ = self.ready()
        models = set()
        for vote in controller.state["design_votes"].values():
            model = vote["identity"]["model"]
            models.add(model)
            self.assertEqual(vote["report"]["classification"]["source"]["agent"]["model"], model)
        self.assertIn("separate-qc-model", models)
        self.assertGreater(len(models), 1)

    def test_design_escalation_displays_the_design_rating_not_old_intake(self):
        controller = self.tick()
        self.tick()  # Produce the plan.
        self.agents.qc_reports = [intake(decision="escalate", risk="high")]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "escalated")
        self.assertEqual(controller.state["retry_stage"], "intake")
        view = ratings.view(controller.state, self.config)
        self.assertEqual(view.estimate.binding["stage"], "plan_qc")
        self.assertEqual(view.assessed, 1)
        self.assertTrue(view.provisional)
        self.assertIn("Design assessment", controller.summary_body())

    def test_compacting_feedback_does_not_change_operation_identity_or_source_files(self):
        controller = self.tick()
        context = {
            "prior_reviews": {"claude": {**APPROVE, "classification": assessment()}},
            "files": {"classification": "This file is real source content."},
        }
        first = controller.operation("codex", "develop", context)
        first.update(status="completed", attempt="a", response={"summary": "retained"})
        projected = ratings.context_core(context)
        self.assertEqual(projected["files"], context["files"])
        self.assertIs(controller.operation("codex", "develop", projected), first)
        self.assertEqual(controller.recover(first), {"summary": "retained"})
        projected["prior_reviews"]["claude"]["summary"] = "New core feedback"
        self.assertNotEqual(controller.operation("codex", "develop", projected)["id"], first["id"])

    def test_optional_feedback_and_historical_ratings_do_not_exhaust_context(self):
        controller = self.tick()
        context = controller.context()
        context["design_feedback"] = [
            {"rationale": "Useful core feedback", "classification": assessment(rationale="x" * 100000)}
        ]
        controller.config["max_context_bytes"] = 20000
        controller.ask(self.config["planner"], "plan", context)
        sent = self.agents.calls[-1][2]
        self.assertEqual(sent["design_feedback"], [{"rationale": "Useful core feedback"}])
        controller.state["prior_outcomes"] = [{"classification_snapshot": {"rationale": "x" * 100000}}]
        review = controller.pr_review_context("claude", {"a.rs": "a"}, {"a.rs": "b"}, "0" * 40)
        self.assertEqual(review["prior_outcomes_untrusted"], [{}])
        self.assertIsNone(controller.oversized(None, {"a.rs": "a"}, {"a.rs": "b"}))


class CompactionReceiptTests(unittest.TestCase):
    """Metadata pressure cannot orphan a published report's compaction receipt."""

    def published(self):
        """Publish a long classified intake report through the actual archive outbox."""
        gh = FakeGitHub(CONFIG)
        report = intake(
            rationale="Published evidence. " * 250,
            classification={
                **assessment(rationale="Rating evidence. " * 25),
                "source": {"report": "operation:attempt"},
            },
        )
        state = {"issue": 7, "qc": {"intake": report}, "reason": report["rationale"]}
        conversation.queue(
            state,
            None,
            source=("qc", "intake"),
            kind="qc",
            details={
                "provider": "claude",
                "model": "model",
                "phase": "intake",
                "round": 1,
                "plan": "intake",
                "reviewer": "reviewer",
            },
        )
        queued = state["conversation"]["pending"][0]
        queued["format"] = 3
        queued["content_digest"] = digest(conversation.archive(queued, state))
        event = deepcopy(state["conversation"]["pending"][0])
        conversation.publish(state, gh, CONFIG)
        receipt = state["conversation"]["receipts"]["qc/intake"]
        self.assertEqual(receipt["content_digest"], event["content_digest"])
        self.assertEqual(receipt["source_digest"], digest(report))
        self.assertEqual(receipt["core_digest"], digest(ratings.core(report)))
        return state, gh

    def pressured(self, remove):
        """Trim or remove a rating while an unrelated issue event holds back compaction."""
        state, gh = self.published()
        report = state["qc"]["intake"]
        original, archive, receipt = deepcopy(report), gh.archives()[0], deepcopy(state["conversation"]["receipts"])
        conversation.queue(state, "Another event", "An unrelated pending notification.")
        # Historical ratings can be reduced; current slots must remain intact.
        state["qc"] = {}
        state["review_history"] = [{"intake": report}]
        trimmed = deepcopy(state)
        trimmed["review_history"][0]["intake"]["classification"].update(rationale="", unknowns=[])
        ratings.make_room(state, 0 if remove else ratings.size(trimmed), ratings.size)
        state["qc"] = {"intake": state.pop("review_history")[0]["intake"]}
        if remove:
            self.assertNotIn("classification", report)
        else:
            self.assertEqual(report["classification"]["rationale"], "")
            self.assertEqual(report["classification"]["change_risk"], "high")
        self.assertNotEqual(digest(report), receipt["qc/intake"]["source_digest"])
        conversation.compact(state)
        self.assertEqual(report["rationale"], original["rationale"])
        # The new match must not bypass the PR outbox's own guard either.
        conversation.publish(state, gh, CONFIG)
        state["pr_outbox"] = [{"kind": "withdrawn", "findings": []}]
        conversation.compact(state)
        self.assertEqual(report["rationale"], original["rationale"])
        state["pr_outbox"] = []
        # Reconstruct the checkpoint, including a distinct copy of the same accepted vote.
        state = json.loads(json.dumps(state))
        state["design_votes"] = {"vote": {"report": deepcopy(state["qc"]["intake"])}}
        conversation.compact(state)
        for compacted in (state["qc"]["intake"], state["design_votes"]["vote"]["report"]):
            self.assertLess(len(compacted["rationale"]), 1000)
            self.assertTrue(compacted["rationale"].endswith(conversation.ARCHIVED))
            self.assertEqual(compacted["scope"], original["scope"])
        self.assertEqual(state["reason"], state["qc"]["intake"]["rationale"])
        self.assertEqual(state["conversation"]["receipts"], receipt)
        self.assertEqual(gh.archives(), [archive])
        before = deepcopy(state)
        conversation.compact(state)
        self.assertEqual(state, before)

    def test_trimmed_rating_does_not_prevent_later_text_compaction(self):
        """An archived rating may be shortened before its report becomes eligible for compaction."""
        self.pressured(remove=False)

    def test_removed_rating_does_not_prevent_later_text_compaction(self):
        """Dropping the entire rating still leaves a trustworthy receipt for the core text."""
        self.pressured(remove=True)

    def test_modified_or_unpublished_core_text_is_not_compacted(self):
        """A core receipt never covers new narrative or an unrelated unpublished report."""
        state, _ = self.published()
        report = state["qc"]["intake"]
        report.pop("classification")
        report["rationale"] += "New unpublished evidence."
        state["qc"]["plan_qc"] = intake(rationale="Unpublished report. " * 250)
        before = deepcopy(state["qc"])
        conversation.compact(state)
        self.assertEqual(state["qc"], before)

    def test_legacy_receipts_keep_their_exact_source_matching(self):
        """Old receipts retain their original guarantees without inferred or backfilled hashes."""
        for artifact in (False, True):
            with self.subTest(artifact=artifact):
                report = intake(rationale="Legacy published evidence. " * 250)
                receipt = {"source_digest": digest(report)}
                if artifact:
                    receipt["artifact"] = 1
                state = {"qc": {"intake": report}, "conversation": {"receipts": {"qc/intake": receipt}}}
                conversation.compact(state)
                self.assertLess(len(report["rationale"]), 1000)
                self.assertIn("report archive" if artifact else "published issue conversation", report["rationale"])
                self.assertNotIn("core_digest", receipt)
        # A legacy receipt cannot authorize a newly inferred core digest after metadata changes.
        state, _ = self.published()
        state["conversation"]["receipts"]["qc/intake"].pop("core_digest")
        state["qc"]["intake"].pop("classification")
        before = deepcopy(state["qc"])
        conversation.compact(state)
        self.assertEqual(state["qc"], before)

    def test_core_receipts_never_relax_exact_publication_recovery(self):
        """Even an advisory-only edit still invalidates an already queued archive body."""
        state, _ = self.published()
        conversation.queue(
            state,
            None,
            source=("qc", "intake"),
            kind="qc",
            details={
                "provider": "claude",
                "model": "model",
                "phase": "intake",
                "round": 2,
                "plan": "intake",
                "reviewer": "reviewer",
            },
        )
        event = state["conversation"]["pending"][0]
        state["qc"]["intake"]["classification"]["change_risk"] = "low"
        with self.assertRaises(conversation.PublicationError):
            conversation.event_body(state, event)
