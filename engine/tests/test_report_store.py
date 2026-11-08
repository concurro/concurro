"""Canonical report references, transport-sized parts and recovery without extra model calls."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
from pipeline_fixtures import PipelineFixture
from checkpoints import Checkpoints, CheckpointError, legacy_state
from policy import approval_valid, digest, validate_pr_review
from report_store import Store, MARKER, ReportDataError
from fakes import CONFIG, FakeAgents, FakeGitHub, sized_report
import conversation
import format_samples
import reviews


class StoreTests(unittest.TestCase):
    """A referenced report is usable only after every immutable part is verified."""

    def setUp(self):
        """Use the same issue-artifact API fake as the controller pipeline."""
        self.gh = FakeGitHub(CONFIG)
        self.store = self.fresh()

    def fresh(self):
        """Simulate a new process without any trusted in-memory report cache."""
        return Store(self.gh, 7, CONFIG["bot_login"])

    def test_unicode_fences_and_multiple_parts_round_trip(self):
        """Neither escaping nor Markdown fences change the recovered JSON or exceed the API cap."""
        report = {"verdict": "approve", "summary": "é😀" * 15000 + "`" * 20000}
        key = self.store.put(report)
        numbers = self.store.numbers[key]
        self.assertGreater(len(numbers), 1)
        self.assertEqual(self.fresh().get(key, numbers), report)
        for number in numbers:
            self.assertLessEqual(len(self.gh.artifacts[7][number]["artifact"]["body"].encode()), 60000)
        self.assertEqual(len(self.gh.artifacts[7]), len(numbers))
        self.fresh().put(report)
        self.assertEqual(len(self.gh.artifacts[7]), len(numbers))

    def test_annotations_and_state_copies_share_one_core_report(self):
        """The retained result, accepted report, vote and archive do not each copy its raw evidence."""
        report = {"decision": "approve", "rationale": "UNIQUE-EVIDENCE " + "z" * 7000}
        raw = self.store.put(report)
        accepted = {**report, "classification": {"complexity": "high"}}
        self.store.put({"qc": accepted, "vote": {"report": accepted}, "response": report})
        bodies = [i["artifact"]["body"] for i in self.gh.artifacts[7].values()]
        self.assertEqual(sum(b.count("UNIQUE-EVIDENCE") for b in bodies), 1)
        self.assertEqual(self.fresh().get(raw, self.store.numbers[raw]), report)

    def test_existing_format_three_archive_is_reused(self):
        """Published pre-upgrade reports remain the canonical copy when externalizing old audit data."""
        state, source, details = format_samples.review_state()
        conversation.queue(state, None, source=source, kind="review", details=format_samples.v2_details(details))
        event = state["conversation"]["pending"][0]
        event["format"] = 3
        body = conversation.archive(event, state)
        event["content_digest"] = digest(body)
        conversation.publish(state, self.gh, CONFIG)
        report = conversation.source_value(state, source)
        before = len(self.gh.artifacts[7])
        key = self.store.put(report)
        self.assertEqual(len(self.gh.artifacts[7]), before)
        self.assertEqual(self.fresh().get(key, self.store.numbers[key]), report)

    def test_lost_creation_response_recovers_without_duplicate_parts(self):
        """A new process discovers a successful write even when its response was lost."""
        original, lost = self.gh.repo, []

        def write(path, method="GET", data=None):
            """Lose exactly the first immutable-part response after GitHub accepts it."""
            result = original(path, method, data)
            if method == "POST" and (data or {}).get("body", "").startswith(MARKER) and not lost:
                lost.append(result)
                raise RuntimeError("lost response")
            return result

        value = {"summary": "large " * 10000}
        with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
            self.store.put(value)
        store = self.fresh()
        key = store.put(value)
        self.assertEqual(len(self.gh.artifacts[7]), len(store.numbers[key]))
        self.assertEqual(store.get(key), value)

    def test_archive_core_reuse_and_single_digest_index(self):
        """Annotated older archives also satisfy core references; discovery parses them once."""
        report = {
            "decision": "approve",
            "rationale": "old evidence " * 300,
            "classification": {"complexity": "high"},
            "model": "recorded-model",
        }
        for i in range(4):
            self.gh.repo(
                "issues/7/artifacts",
                "POST",
                {
                    "type": "generic",
                    "name": f"Old review {i}",
                    "body": f"<!-- concurro:event:v1:{i} -->\n```json\n{json.dumps({**report, 'risk': str(i)})}\n```",
                },
            )
        core = {k: v for k, v in {**report, "risk": "0"}.items() if k not in ("classification", "model")}
        with patch("report_store.json.loads", wraps=json.loads) as loads:
            key = self.store.put(core)
            self.assertEqual(len(self.gh.artifacts[7]), 4)
            for i in range(3):
                self.store.put({"summary": f"New {i}"})
            self.assertEqual(loads.call_count, 4)
        self.assertEqual(self.fresh().get(key, self.store.numbers[key]), core)

    def test_missing_edited_replaced_and_duplicate_parts_fail_closed(self):
        """A digest alone never substitutes for verified artifact data and authorship."""
        for failure in ("missing", "edited", "version", "foreign", "replacement", "duplicate"):
            with self.subTest(failure=failure):
                self.setUp()
                value = {"summary": "complete evidence " * 5000}
                key = self.store.put(value)
                numbers = self.store.numbers[key]
                item = self.gh.artifacts[7][numbers[-1]]
                if failure == "missing":
                    del self.gh.artifacts[7][numbers[-1]]
                elif failure == "edited":
                    item["artifact"]["body"] = item["artifact"]["body"].replace("evidence", "tampered")
                    item["versions"][0]["body"] = item["artifact"]["body"]
                elif failure == "version":
                    item["versions"][0]["version"] = 2
                elif failure == "foreign":
                    item["artifact"]["creator"]["login"] = "someone-else"
                elif failure == "replacement":
                    item["artifact"]["number"] += 1000
                else:
                    clone = deepcopy(item)
                    clone["artifact"]["id"] += 1000
                    clone["artifact"]["number"] += 1000
                    self.gh.artifacts[7][clone["artifact"]["number"]] = clone
                if failure == "duplicate":
                    # Bound identities remain authoritative; discovery must reject ambiguous markers.
                    self.assertEqual(self.fresh().get(key, numbers), value)
                    with self.assertRaises(ReportDataError):
                        self.fresh().get(key)
                else:
                    with self.assertRaises(ReportDataError):
                        self.fresh().get(key, numbers)


class CapacityTests(PipelineFixture, unittest.TestCase):
    """Large valid evidence completes real controller transitions without output repair."""

    def setUp(self):
        """Seed a fresh enrolled fixture and permit enough model input for the large-report test."""
        self.config = deepcopy(CONFIG)
        self.config["max_context_bytes"] = 500000
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        self.gh.issue["body"] = "\n\n".join(f"- Requirement {i}" for i in range(12))

    def wordy(self, provider, stage, context):
        """Return meaningful-shaped complete evidence far beyond the old aggregate limit."""
        report = self.original(provider, stage, context)
        if stage == "plan_qc":
            report = sized_report(context)
            report["rationale"] = "The complete design is supported."
            for entry in report["assessments"]:
                entry["reason"] = "Detailed source evidence. " * 300
            self.assertGreater(len(json.dumps(report).encode()), 60000)
        return report

    def test_both_full_reviews_survive_restart_and_authorize_without_repairs(self):
        """The audit holds references; complete evidence remains available to semantic validation."""
        self.tick()
        self.tick()
        self.original = self.agents.ask
        with patch.object(self.agents, "ask", side_effect=self.wordy):
            self.tick()
            controller = self.tick()
        self.assertEqual(controller.state["stage"], "implement")
        self.assertTrue(approval_valid(controller.state, self.config))
        self.assertFalse(controller.state.get("review_repairs"))
        self.assertEqual(sum(stage == "plan_qc" for _, stage, _ in self.agents.calls), 2)
        body = controller.checkpoints.audit["body"]
        self.assertLess(len(body.encode()), 30000)
        self.assertNotIn("Detailed source evidence.", body)
        self.assertIn("https://github.com/", body)
        raw = legacy_state(body)
        self.assertTrue(raw["report_data"])
        recovered = Checkpoints(self.gh, self.config, 7).load(self.gh.comments)
        self.assertEqual(recovered, controller.state)
        for vote in recovered["design_votes"].values():
            self.assertEqual(len(vote["report"]["assessments"]), 13)
            self.assertEqual(vote["report"]["assessments"][0]["reason"], "Detailed source evidence. " * 300)
        for item in self.gh.artifacts[7].values():
            self.assertLessEqual(len(item["artifact"]["body"].encode()), 60000)

    def test_retained_large_result_replays_after_crash_without_another_call(self):
        """The completed response itself is durable before the vote is recorded."""
        self.tick()
        self.tick()
        self.original = self.agents.ask
        original, lost = Checkpoints.save, []

        def save(checkpoints, state):
            """Crash immediately after the completed review response is committed."""
            result = original(checkpoints, state)
            operation = state.get("operation", {})
            if operation.get("stage") == "plan_qc" and operation.get("status") == "completed" and not lost:
                lost.append(True)
                raise RuntimeError("process exited")
            return result

        with patch.object(self.agents, "ask", side_effect=self.wordy):
            with patch.object(Checkpoints, "save", save), self.assertRaises(RuntimeError):
                self.tick()
            calls = len(self.agents.calls)
            controller = self.tick()
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(len(controller.state["design_votes"]), 1)
        self.assertEqual(controller.state["operation"]["status"], "consumed")
        refs = legacy_state(controller.checkpoints.audit["body"])["report_data"]
        number = refs[0]["artifacts"][0]
        del self.gh.artifacts[7][number]
        with self.assertRaises(CheckpointError):
            self.tick()
        self.assertEqual(len(self.agents.calls), calls)

    def test_long_finding_keeps_full_evidence_but_bounds_its_github_comment(self):
        """Publication excerpts are display-only; finding identity and repair context retain the detail."""
        detail = "<*&> " * 20000
        report = {
            "verdict": "changes_requested",
            "summary": "A real defect.",
            "design_issue": None,
            "dispositions": [],
            "findings": [{"path": None, "line": None, "start_line": None, "detail": detail}],
        }
        validate_pr_review(report, [], [])
        state = {"issue": 7}
        reviews.apply(state, "claude", report, "a" * 40, "https://github.com/o/r/issues/7")
        finding = state["findings"][0]
        body = reviews.finding_body(finding)
        self.assertLessEqual(len(body.encode()), reviews.REVIEW_BODY_LIMIT)
        self.assertIn("https://github.com/o/r/issues/7", body)
        self.assertEqual(finding["detail"], detail)
        self.assertEqual(reviews.open_findings(state)[0]["detail"], detail)
        self.assertEqual(finding["opening_digest"], digest(body))

    def test_large_invalid_evidence_still_needs_a_real_correction(self):
        """External storage never turns an unsupported citation into an approval."""
        self.tick()
        self.tick()
        self.original = self.agents.ask

        def invalid(provider, stage, context):
            """Keep the full report but make its evidence demonstrably false."""
            report = self.wordy(provider, stage, context)
            report["sources"][0]["quote"] = "not in the pinned source"
            return report

        with patch.object(self.agents, "ask", side_effect=invalid):
            controller = self.tick()
        self.assertFalse(controller.state["design_votes"])
        self.assertFalse(approval_valid(controller.state, self.config))
        self.assertEqual(controller.state["review_repair"]["feedback"]["code"], "review_citation")

    def test_long_disposition_and_reopen_evidence_render_within_github_limits(self):
        """Display limits cannot reject complete evidence about a fixed finding or design defect."""
        detail = "<*&> " * 20000
        state = {
            "issue": 7,
            "plan_id": "a" * 64,
            "findings": [{"id": "F1", "report_url": "https://github.com/o/r/issues/7"}],
        }
        item = {"format": 3, "head": "b" * 40, "sequence": 2, "provider": "claude"}
        disposition = {"id": "F1", "status": "fixed", "evidence": detail}
        review = {
            "verdict": "approve",
            "summary": "Fixed.",
            "findings": [],
            "dispositions": [disposition],
            "design_issue": None,
        }
        validate_pr_review(review, ["F1"], [])
        body = reviews.disposition_body(state, item, disposition)
        self.assertLessEqual(len(body.encode()), reviews.REVIEW_BODY_LIMIT)
        self.assertIn("https://github.com/o/r/issues/7", body)
        reopened = {
            "verdict": "reopen",
            "summary": "Design defect.",
            "findings": [],
            "dispositions": [],
            "design_issue": {"kind": "plan", "explanation": detail, "evidence": [detail]},
        }
        validate_pr_review(reopened, [], [])
        body = reviews.review_body(state, {"repository": "o/r"}, item, {**reopened, "model": "m"}, [], {})
        self.assertLessEqual(len(body.encode()), reviews.REVIEW_BODY_LIMIT)
        self.assertIn(reviews.OMITTED, body)
        self.assertEqual(reopened["design_issue"]["explanation"], detail)

    def test_long_review_questions_and_reasons_do_not_break_notification_publication(self):
        """Humans get a bounded notice and the complete question remains in request state."""
        import actions

        question = "<*&> " * 20000
        notice = actions.explain(question, "Please retry.")
        self.assertLess(len(notice.encode()), conversation.EVENT_LIMIT)
        state = {"issue": 7, "issue_digest": "d", "mode": "autonomous"}
        questions = [{"provider": "claude", "question": question}] * 8
        with patch.object(actions, "questions", return_value=questions):
            actions.request(state, self.config, "writer")
        self.assertEqual(state["requests"][0]["questions"], questions)
        conversation.publish(state, self.gh, self.config)
        self.assertLess(len(self.gh.comments[-1]["body"].encode()), conversation.EVENT_LIMIT)
