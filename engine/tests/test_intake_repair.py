"""Invalid intake output is visible, recoverable and corrected without authorizing work."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

from checkpoints import LIMIT, Checkpoints
from controller import INTAKE_OUTPUT_REPAIRS
from conversation import ARCHIVE_LIMIT, PublicationError
from fakes import FakeAgents, FakeGitHub
from pipeline_fixtures import PipelineFixture
from policy import Blocked


class IntakeRepairTests(PipelineFixture, unittest.TestCase):
    """Exercise correction and archive recovery through real controller ticks."""

    def setUp(self):
        """Start with a fresh enrolled task and deterministic provider."""
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()

    def report(self, **changes):
        """Build valid intake output with optional malformed fields."""
        return {
            "decision": "proceed",
            "rationale": "The requested behavior is clear.",
            "risk": "low",
            "questions": [],
            "concerns": [],
            "scope": {"goal_ids": ["G-USABILITY"], "fit": "Improve usability.", "conflicts": []},
            **changes,
        }

    def test_contradiction_is_archived_then_corrected_without_writer_retry(self):
        """Rejected proceed never starts planning; feedback produces a separately charged turn."""
        self.agents.qc_reports = [self.report(questions=["Which partial-read behavior is intended?"]), self.report()]
        rejected = self.tick()
        self.assertEqual(rejected.state["stage"], "intake")
        self.assertEqual(rejected.outcome(True).kind, "continue")
        self.assertNotIn("intake", rejected.state.get("qc", {}))
        self.assertNotIn("approval", rejected.state)
        self.assertNotIn("plan", rejected.state)
        self.assertEqual(len(self.gh.archives()), 1)
        archive = self.gh.archives()[0]
        self.assertIn("Intake output rejected", archive)
        self.assertIn("QC acceptance contradicts", archive)
        self.assertIn("Which partial-read behavior is intended?", archive)
        self.assertIn("Output rejected", self.gh.summary()["body"])
        self.assertIn("/artifacts/", self.gh.summary()["body"])
        accepted = self.tick()
        self.assertEqual(accepted.state["stage"], "plan")
        self.assertNotIn("intake_draft", accepted.state)
        self.assertEqual(accepted.state["calls"], 2)
        self.assertEqual(accepted.state["plan_revisions"], 0)
        self.assertEqual(len(self.gh.archives()), 2)
        self.assertEqual(self.gh.archives()[0], archive)
        feedback = self.agents.calls[1][2]["intake_validation_feedback"]
        self.assertIn("contradicts", feedback["reason"])
        self.assertIn("Which partial-read behavior is intended?", feedback["output"])
        self.assertIn("do not remove real questions", feedback["instruction"])
        self.assertEqual(feedback["attempt"], 1)

    def test_corrected_intake_completes_the_autonomous_pipeline(self):
        """An output repair preserves the independent planning, review and CI gates."""
        self.config["autonomous"] = True
        self.agents.qc_reports = [self.report(questions=["Which partial-read behavior is intended?"]), self.report()]
        for _ in range(15):
            controller = self.tick()
            if controller.state.get("handoff_ready"):
                break
        self.assertTrue(controller.state.get("handoff_ready"))
        self.assertFalse(self.gh.pr["draft"])
        self.assertEqual(self.gh.merges, 0)
        self.assertEqual(len(controller.state["reviews"]), 2)
        self.assertEqual(sum(stage == "intake" for _, stage, _ in self.agents.calls), 2)
        self.assertEqual(sum("Intake output rejected" in body for body in self.gh.archives()), 1)

    def test_corrected_genuine_questions_enter_clarification(self):
        """Correction cannot turn a real unanswered question into silent approval."""
        self.agents.qc_reports = [
            self.report(questions=["Which behavior is intended?"]),
            self.report(decision="clarify", questions=["Which behavior is intended?"]),
        ]
        self.tick()
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "clarify")
        self.assertEqual(controller.outcome(True).kind, "await-human")
        self.assertNotIn("plan", controller.state)
        self.assertTrue(
            any("Input needed" in c["body"] and "Which behavior is intended?" in c["body"] for c in self.gh.comments)
        )

    def test_valid_human_decisions_are_never_output_repairs(self):
        """Valid clarify, rejection and high-risk escalation retain their existing transitions."""
        for fields, expected in (
            ({"decision": "clarify", "questions": ["What is intended?"]}, "clarify"),
            ({"decision": "reject", "concerns": ["Outside scope."]}, "rejected"),
            ({"decision": "escalate", "concerns": ["Needs human judgment."]}, "escalated"),
            ({"risk": "high"}, "escalated"),
        ):
            with self.subTest(fields=fields):
                self.setUp()
                self.agents.qc_reports = [self.report(**fields)]
                controller = self.tick()
                self.assertEqual(controller.state["stage"], expected)
                self.assertNotIn("intake_draft", controller.state)
                self.assertEqual(controller.state["calls"], 1)

    def test_exhaustion_survives_restart_and_retry_renews_only_corrections(self):
        """Every invalid report is archived; only a writer renews the spent correction allowance."""
        bad = self.report(questions=["Unanswered question?"])
        self.agents.qc_reports = [bad] * (INTAKE_OUTPUT_REPAIRS + 2)
        for _ in range(INTAKE_OUTPUT_REPAIRS + 1):
            controller = self.tick()
        self.assertEqual(controller.state["stage"], "escalated")
        self.assertEqual(controller.state["retry_stage"], "intake")
        self.assertEqual(controller.state["calls"], 3)
        self.assertEqual(len(self.gh.archives()), 3)
        self.assertEqual(self.tick().state["calls"], 3)
        self.gh.comment("/concurro retry", login="stranger")
        self.assertEqual(self.tick().state["stage"], "escalated")
        self.gh.comment("/concurro retry")
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "intake")
        self.assertEqual(controller.state["intake_draft"]["attempt"], 1)
        self.assertEqual(controller.state["calls"], 4)
        self.assertEqual(controller.state["plan_revisions"], 0)
        self.assertEqual(len(self.gh.archives()), 4)
        self.assertEqual(len({r["round"] for r in controller.state["reports"]}), 4)
        self.assertEqual(self.tick().state["stage"], "plan")

    def test_malformed_and_oversized_reports_publish_bounded_excerpts(self):
        """Malformed shapes and hostile fences cannot break the archive or checkpoint."""
        for report in (
            [],
            self.report(decision=[]),
            self.report(scope={"goal_ids": []}),
            self.report(rationale="`" * 30000),
            self.report(rationale="~`<details>\n" * 30000),
        ):
            with self.subTest(kind=type(report)):
                self.setUp()
                with patch.object(self.agents, "ask", return_value=report):
                    controller = self.tick()
                self.assertEqual(controller.state["stage"], "intake")
                archive = self.gh.archives()[0]
                self.assertIn("Intake output rejected", archive)
                self.assertLessEqual(len(archive.encode()), ARCHIVE_LIMIT)
                self.assertLessEqual(len(controller.checkpoints.audit_body(controller.state).encode()), LIMIT)
                if controller.state["intake_draft"]["truncated"]:
                    self.assertIn("Output excerpted", archive)

    def test_completed_response_is_consumed_once_after_crash(self):
        """A crash before recording the rejection replays the retained response without a model call."""
        original = Checkpoints.save
        interrupted = []

        class Crash(BaseException):
            """Simulate process death before the rejection is committed."""

        def save(store, state):
            """Leave the completed response durable while losing the first rejection save."""
            if state.get("intake_draft") and not interrupted:
                interrupted.append(True)
                raise Crash()
            return original(store, state)

        with patch.object(self.agents, "ask", return_value=self.report(questions=["Question?"])) as ask:
            with patch.object(Checkpoints, "save", save), self.assertRaises(Crash):
                self.tick()
            recovered = self.tick()
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(recovered.state["intake_draft"]["attempt"], 1)
        self.assertEqual(recovered.state["operation"]["status"], "consumed")
        self.assertEqual(len(self.gh.archives()), 1)

    def test_lost_archive_response_recovers_before_next_correction(self):
        """A stored archive with a lost POST reply is found by marker, never duplicated."""
        original = self.gh.repo
        lost = []

        def repo(path, method="GET", data=None):
            """Lose the first rejected-report creation response after the write succeeds."""
            result = original(path, method, data)
            if method == "POST" and "Intake output rejected" in (data or {}).get("body", "") and not lost:
                lost.append(True)
                raise RuntimeError("lost response")
            return result

        self.agents.qc_reports = [self.report(questions=["Question?"]), self.report()]
        with patch.object(self.gh, "repo", side_effect=repo), self.assertRaises(PublicationError):
            self.tick()
        self.assertTrue(lost)
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan")
        self.assertEqual(controller.state["calls"], 2)
        self.assertEqual(len(self.gh.archives()), 2)
        self.assertEqual(sum("Intake output rejected" in b for b in self.gh.archives()), 1)

    def test_changed_archive_is_a_conflict_not_permission_to_correct(self):
        """Recovery never overwrites altered public evidence or calls the model past it."""
        original = self.gh.repo
        lost = []

        def repo(path, method="GET", data=None):
            """Persist an archive, alter it, then lose its creation response."""
            result = original(path, method, data)
            if method == "POST" and "Intake output rejected" in (data or {}).get("body", "") and not lost:
                lost.append(True)
                self.gh.artifacts[7][result["number"]]["artifact"]["body"] += "\nEdited"
                raise RuntimeError("lost response")
            return result

        self.agents.qc_reports = [self.report(questions=["Question?"])]
        with patch.object(self.gh, "repo", side_effect=repo), self.assertRaises(PublicationError):
            self.tick()
        with self.assertRaisesRegex(PublicationError, "edited or duplicated"):
            self.tick()
        self.assertEqual(len(self.agents.calls), 1)
        self.assertEqual(len(self.gh.archives()), 1)

    def test_transport_errors_and_call_budget_do_not_become_output_corrections(self):
        """Failures outside validation retain their original retry or blocking policy."""
        with patch.object(self.agents, "ask", side_effect=RuntimeError("transport failed")):
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                self.tick()
        self.assertFalse(self.gh.archives())
        self.setUp()
        self.config["max_model_calls"] = 1
        self.agents.qc_reports = [self.report(questions=["Question?"])]
        controller = self.tick()
        self.assertEqual(controller.state["intake_draft"]["attempt"], 1)
        with self.assertRaisesRegex(Blocked, "model-call budget exhausted"):
            self.tick()
        self.assertEqual(len(self.gh.archives()), 1)

    def advisory_comments(self):
        """Return only the new human-facing intake notices."""
        return [c for c in self.gh.comments if "### Intake approved with concerns" in c["body"]]

    def test_advisories_reach_planner_and_both_review_stages_without_extra_calls(self):
        """Proceed with concerns reaches ready using the same calls and independent review gates."""
        self.config["autonomous"] = True
        concerns = ["Check streaming callers.", "Demo CI covers only stacks-common."]
        self.agents.qc_reports = [self.report(concerns=concerns)]
        for _ in range(15):
            controller = self.tick()
            if controller.state.get("handoff_ready"):
                break
        self.assertTrue(controller.state.get("handoff_ready"))
        self.assertEqual(controller.state["calls"], 7)
        self.assertNotIn("intake_draft", controller.state)
        self.assertEqual(len(self.advisory_comments()), 1)
        self.assertIn("not a request for human action", self.advisory_comments()[0]["body"])
        for concern in concerns:
            self.assertIn(concern, self.advisory_comments()[0]["body"])
        downstream = [(stage, context) for _, stage, context in self.agents.calls if stage != "intake"]
        self.assertEqual({stage for stage, _ in downstream}, {"plan", "plan_qc", "develop", "review"})
        for stage, context in downstream:
            with self.subTest(stage=stage):
                self.assertEqual(context["intake_concerns"], concerns)
        self.tick()
        self.assertEqual(len(self.advisory_comments()), 1)

    def test_hypothetical_compatibility_hazard_is_advisory_but_actual_conflict_blocks(self):
        """The same domain can carry a planning guardrail or an incompatible requested outcome."""
        hazard = "Changing padding normalization could alter consensus encoding; preserve the existing encoding."
        self.agents.qc_reports = [self.report(concerns=[hazard])]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan")
        self.assertEqual(controller.context()["intake_concerns"], [hazard])
        self.assertNotIn("intake_draft", controller.state)

        self.setUp()
        scope = {
            "goal_ids": ["G-USABILITY"],
            "fit": "Requested encoding change.",
            "conflicts": ["The requested consensus encoding change violates the charter non-goal."],
        }
        self.agents.qc_reports = [self.report(scope=scope), self.report(decision="reject", scope=scope)]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "intake")
        self.assertNotIn("plan", controller.state)
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "rejected")
        self.assertFalse(self.advisory_comments())

    def test_questions_conflicts_and_high_risk_never_announce_proceed(self):
        """Advisory concerns cannot bypass any existing human or charter boundary."""
        for fields, expected in (
            ({"questions": ["What behavior?"]}, "intake"),
            (
                {"scope": {"goal_ids": ["G-USABILITY"], "fit": "Change.", "conflicts": ["A charter non-goal."]}},
                "intake",
            ),
            ({"risk": "high"}, "escalated"),
            ({"decision": "clarify", "questions": ["What behavior?"]}, "clarify"),
            ({"decision": "reject"}, "rejected"),
        ):
            with self.subTest(fields=fields):
                self.setUp()
                self.agents.qc_reports = [self.report(concerns=["Consider compatibility."], **fields)]
                controller = self.tick()
                self.assertEqual(controller.state["stage"], expected)
                self.assertFalse(self.advisory_comments())
                self.assertNotIn("plan", controller.state)

    def test_design_approval_still_refuses_unresolved_concerns(self):
        """Only intake changes semantics; review objections still require a revise verdict."""
        from policy import validate_qc

        report = self.report(decision="approve", concerns=["The design loses data."])
        report.update(sources=[], assessments=[], objection_updates=[])
        with self.assertRaisesRegex(Blocked, "contradicts"):
            validate_qc(report, "plan_qc")

    def test_clean_intake_adds_no_notification(self):
        """Ordinary approvals retain the quiet dashboard-only flow."""
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan")
        self.assertFalse(self.advisory_comments())
        self.assertNotIn("intake_concerns", controller.context())

    def test_changed_requirements_do_not_reuse_old_advisories(self):
        """Only concerns about the same requirements and base enter downstream contexts."""
        self.agents.qc_reports = [self.report(concerns=["Check the old interface."])]
        controller = self.tick()
        self.assertEqual(controller.context()["intake_concerns"], ["Check the old interface."])
        controller.state["requirements"]["body"] = "A different task."
        self.assertNotIn("intake_concerns", controller.context())

    def test_notice_recovers_a_lost_response_without_duplicate_or_model_call(self):
        """A successful comment POST with a lost response recovers before planning."""
        original = self.gh.repo
        lost = []

        def repo(path, method="GET", data=None):
            """Lose the first advisory comment response after GitHub applies it."""
            result = original(path, method, data)
            if (
                method == "POST"
                and path.endswith("/comments")
                and "Intake approved with concerns" in (data or {}).get("body", "")
                and not lost
            ):
                lost.append(True)
                raise RuntimeError("lost response")
            return result

        self.agents.qc_reports = [self.report(concerns=["Check callers."])]
        with patch.object(self.gh, "repo", side_effect=repo), self.assertRaises(PublicationError):
            self.tick()
        self.assertTrue(lost)
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan_qc")
        self.assertEqual(controller.state["calls"], 2)
        self.assertEqual(sum(stage == "intake" for _, stage, _ in self.agents.calls), 1)
        self.assertEqual(len(self.advisory_comments()), 1)
        self.assertEqual(sum("Intake QC" in b for b in self.gh.archives()), 1)

    def test_notice_preserves_markdown_and_contains_unclosed_code_blocks(self):
        """Concern formatting survives without swallowing later bullets or the footer."""
        concerns = [
            "Check `BitVec` and **compatibility**.\n\n```rust\nzeros(0);\n```",
            "An unfinished example:\n\n```rust\nones(0);",
            "Check `<T>`; <details>must stay text</details>.",
        ]
        self.agents.qc_reports = [self.report(concerns=concerns)]
        controller = self.tick()
        body = self.advisory_comments()[0]["body"]
        self.assertIn("### Intake approved with concerns", body)
        self.assertNotIn("advisory concerns", body)
        self.assertIn("- Check `BitVec` and **compatibility**.\n  \n  ```rust\n  zeros(0);\n  ```", body)
        self.assertIn("  ones(0);\n  ```\n- Check `<T>`;", body)
        self.assertIn("&lt;details>must stay text&lt;/details>", body)
        self.assertEqual(controller.context()["intake_concerns"], concerns)

    def test_shortened_notice_closes_its_code_fence(self):
        """Truncating an example keeps subsequent concerns outside its code block."""
        concern = "Example:\n\n```rust\n" + "x" * 1100 + "\n```"
        self.agents.qc_reports = [self.report(concerns=[concern, "Next concern."])]
        controller = self.tick()
        body = self.advisory_comments()[0]["body"]
        self.assertIn("…\n  ```\n- Next concern.", body)
        self.assertIn("Some text is shortened.", body)
        self.assertEqual(controller.context()["intake_concerns"][0], concern)

    def test_notice_escapes_and_bounds_model_text_without_losing_context(self):
        """Comments cannot exceed GitHub bounds; complete concerns stay in context and archives."""
        from conversation import EVENT_LIMIT

        concerns = ["<details>\\`unsafe</details>"] + ["<" * 940] * 6
        self.agents.qc_reports = [self.report(concerns=concerns)]
        controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan")
        self.assertEqual(controller.context()["intake_concerns"], concerns)
        body = self.advisory_comments()[0]["body"]
        self.assertEqual(body.count("<details>"), 1)
        self.assertIn("&lt;", body)
        self.assertIn("Some text is shortened.", body)
        self.assertLessEqual(len(body.encode()), EVENT_LIMIT)
        self.assertIn("<" * 940, self.gh.archives()[0])
