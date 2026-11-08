"""Review corrections remain inspectable without obscuring live task progress."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from checkpoints import LIMIT
from conversation import PublicationError
from fakes import CONFIG, FakeAgents, FakeGitHub
from pipeline_fixtures import PipelineFixture
from policy import approval_valid, digest
from report_store import Store


class DiagnosticsTests(PipelineFixture, unittest.TestCase):
    """Exercise dashboard history and rejected reports through durable controller restarts."""

    def setUp(self):
        """Start an ordinary task with deterministic provider results."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def reach_review(self):
        """Stop immediately before the first design-review call."""
        for _ in range(5):
            controller = self.tick()
            if controller.state["stage"] == "plan_qc":
                return controller
        self.fail("design review not reached")

    def reject_review(self, large=False):
        """Return approval contradicted by an outstanding concern."""
        original = self.agents.ask
        captured = []

        def invalid(provider, stage, context):
            report = original(provider, stage, context)
            report["concerns"] = ["An unresolved correctness problem."]
            if large:
                report["rationale"] = "FULL-REJECTED-REPORT " + "x" * 85000
            captured.append(deepcopy(report))
            return report

        with patch.object(self.agents, "ask", side_effect=invalid):
            controller = self.tick()
        return controller, captured[0]

    def test_specific_reason_archive_and_recovery_are_visible_below_live_status(self):
        """A correction is evidence, never a vote; a successful replacement marks it recovered."""
        self.reach_review()
        controller, report = self.reject_review()
        reason = "QC acceptance contradicts outstanding questions or concerns"
        self.assertEqual(controller.state["review_repair"]["feedback"]["detail"], reason)
        self.assertFalse(controller.state["design_votes"])
        self.assertFalse(approval_valid(controller.state, self.config))
        self.assertNotIn("review_draft", controller.state)
        diagnostic = next(r for r in controller.state["reports"] if r.get("diagnostic"))
        archive = self.gh.artifacts[7][diagnostic["artifact"]]["artifact"]["body"]
        self.assertIn(reason, archive)
        self.assertIn("not an accepted review", archive)
        self.assertEqual(controller.checkpoints.reports.get(digest(report)), report)
        summary = self.gh.summary()["body"]
        self.assertLess(summary.index("| Design review |"), summary.index("<summary>Diagnostics</summary>"))
        self.assertIn("Correction requested", summary)
        self.assertIn("to correct its iterative design review — correction 1 of", summary)
        self.assertIn(reason, summary)
        self.assertNotIn(report["concerns"][0], summary)
        controller = self.tick()
        self.assertTrue(controller.state["design_votes"])
        self.assertNotIn("review_repair", controller.state)
        self.assertIn("Recovered", self.gh.summary()["body"])
        last = next(context for _, stage, context in reversed(self.agents.calls) if stage == "plan_qc")
        self.assertEqual(last["review_validation_feedback"]["detail"], reason)

    def test_large_rejected_output_is_stored_once_and_reloadable(self):
        """Full rejection evidence does not consume the audit's inline capacity."""
        self.reach_review()
        controller, report = self.reject_review(large=True)
        self.assertLess(len(controller.checkpoints.audit["body"].encode()), LIMIT)
        store = Store(self.gh, 7, self.config["bot_login"])
        self.assertEqual(store.get(digest(report)), report)
        artifacts = [a["artifact"]["body"] for a in self.gh.artifacts[7].values()]
        self.assertEqual(sum(body.count("FULL-REJECTED-REPORT") for body in artifacts), 1)
        self.assertTrue(self.load().state["reports"][-1]["diagnostic"])

    def test_decorated_goal_id_feedback_lists_exact_ids_and_recovers(self):
        """Schema bypasses receive the pinned choices in both diagnostics and the next review request."""
        controller = self.reach_review()
        original = self.agents.ask

        def decorated(provider, stage, context):
            """Reproduce the pilot's ID plus explanation rather than an exact key."""
            report = original(provider, stage, context)
            report["scope"]["goal_ids"][0] += ": improves validation"
            return report

        with patch.object(self.agents, "ask", side_effect=decorated):
            self.tick()
        state = self.load().state
        detail = state["review_repair"]["feedback"]["detail"]
        self.assertIn(json.dumps(sorted(controller.bundle.charter["goals"])), detail)
        self.assertIn("scope.fit", detail)
        self.assertNotIn("improves validation", detail)
        self.assertFalse(state["design_votes"])
        self.assertIn("copy exact IDs", self.gh.summary()["body"])
        self.tick()
        context = next(c for _, stage, c in reversed(self.agents.calls) if stage == "plan_qc")
        self.assertEqual(context["review_validation_feedback"]["detail"], detail)
        self.assertTrue(self.load().state["design_votes"])
        self.assertIn("Recovered", self.gh.summary()["body"])

    def test_evidence_validation_reason_survives_archival_reload_and_correction(self):
        """The reviewer and humans see the actual evidence failure rather than generic coverage advice."""
        for failure, reason in (
            ("empty_quote", "invalid design evidence source"),
            ("missing_criterion", "design review must assess every supplied criterion exactly once"),
            ("uncertain", "approval contradicts incomplete criterion evidence"),
        ):
            with self.subTest(failure=failure):
                self.setUp()
                self.reach_review()
                original = self.agents.ask

                def invalid(provider, stage, context):
                    """Break a single evidence requirement without changing the report shape."""
                    report = original(provider, stage, context)
                    if failure == "empty_quote":
                        report["sources"][0]["quote"] = ""
                    elif failure == "missing_criterion":
                        report["assessments"].pop()
                    else:
                        report["assessments"][0]["status"] = "uncertain"
                    return report

                with patch.object(self.agents, "ask", side_effect=invalid):
                    self.tick()
                controller = self.load()
                self.assertEqual(controller.state["review_repair"]["feedback"]["detail"], reason)
                self.assertFalse(controller.state["design_votes"])
                self.assertIn(reason, self.gh.summary()["body"])
                diagnostic = next(r for r in controller.state["reports"] if r.get("diagnostic"))
                self.assertIn(reason, self.gh.artifacts[7][diagnostic["artifact"]]["artifact"]["body"])
                self.tick()
                context = next(c for _, stage, c in reversed(self.agents.calls) if stage == "plan_qc")
                self.assertEqual(context["review_validation_feedback"]["detail"], reason)
                self.assertTrue(self.load().state["design_votes"])

    def test_lost_rejection_archive_response_recovers_without_duplicate_or_extra_review(self):
        """Pending evidence is recovered before a replacement model result can overwrite it."""
        self.reach_review()
        original, lost = self.gh.repo, []

        def repo(path, method="GET", data=None):
            result = original(path, method, data)
            if (
                method == "POST"
                and path.endswith("/artifacts")
                and not lost
                and "### Design review output rejected" in (data or {}).get("body", "")
            ):
                lost.append(True)
                raise RuntimeError("lost response")
            return result

        with patch.object(self.gh, "repo", side_effect=repo):
            with self.assertRaises(PublicationError):
                self.reject_review(large=True)
        before = self.load().state["calls"]
        self.assertTrue(lost)
        controller = self.tick()
        self.assertEqual(controller.state["calls"], before + 1)
        diagnostics = [r for r in controller.state["reports"] if r.get("diagnostic")]
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["diagnostic"]["status"], "Recovered")
        archives = [a["artifact"]["body"] for a in self.gh.artifacts[7].values()]
        self.assertEqual(sum("### Design review output rejected" in a for a in archives), 1)

    def test_exhaustion_is_visible_and_old_checkpoints_do_not_invent_evidence(self):
        """A zero correction allowance escalates with the rejected output linked."""
        self.config["max_review_repairs"] = 0
        self.reach_review()
        controller, _ = self.reject_review()
        self.assertEqual(controller.state["stage"], "escalated")
        self.assertIn("Escalated · 0/0 corrections", controller.diagnostics_table())
        self.assertIn(": Paused", self.gh.summary()["body"])
        self.assertNotIn(": Waiting", self.gh.summary()["body"])
        controller.state["reports"] = [r for r in controller.state["reports"] if not r.get("diagnostic")]
        self.assertIn("Not retained by the earlier engine", controller.diagnostics_table())

    def test_malformed_review_can_be_archived_without_a_valid_report_shape(self):
        """Even a non-object result remains inspectable without granting approval."""
        self.reach_review()
        with patch.object(self.agents, "ask", return_value=["not a review"]):
            controller = self.tick()
        self.assertFalse(controller.state["design_votes"])
        self.assertEqual(controller.checkpoints.reports.get(digest(["not a review"])), ["not a review"])

    def test_current_and_historical_rounds_live_under_their_stage(self):
        """Stage rows and their report links remain together, without a separate rounds table."""
        controller = self.reach_review()
        controller.state["reports"] = [
            {
                "step": "Design review",
                "round": "1 cooperative",
                "provider": "claude",
                "outcome": "approve",
                "artifact": 21,
            },
            {"step": "Design review", "round": "1 fresh", "provider": "codex", "outcome": "revise", "artifact": 22},
            {"step": "Code review", "round": "1", "provider": "claude", "outcome": "approve", "artifact": 23},
        ]
        summary = controller.summary_body()
        self.assertNotIn("<summary>Rounds", summary)
        self.assertLess(summary.index("| Design review |"), summary.index("↳ Round 1 iterative"))
        self.assertLess(summary.index("↳ Round 1 fresh"), summary.index("| Implement |"))
        self.assertIn("codex: 🛠️ Revise", summary)
        self.assertLess(summary.index("| Code review |"), summary.index("/artifacts/23"))
        self.assertNotIn("↳ Round", controller.summary_lines(compact=True))

    def test_audit_is_collapsed_and_plan_is_linked_only_in_its_stage(self):
        """Both summary sizes retain recovery metadata without repeating visible footer links."""
        from checkpoints import REF_MARKER

        controller = self.reach_review()
        for compact in (False, True):
            with self.subTest(compact=compact):
                summary = controller.summary_lines(compact)
                main, diagnostics = summary.split("<details><summary>Diagnostics</summary>", 1)
                content, footer = diagnostics.rsplit("</details>", 1)
                self.assertNotIn("[Automation audit state]", main)
                self.assertIn("[Automation audit state]", content)
                self.assertNotIn("[Automation audit state]", footer)
                self.assertEqual(summary.count("[Implementation plan]"), 1)
                self.assertIn("[Implementation plan]", main)
                self.assertNotIn(REF_MARKER, content)
                self.assertIn(REF_MARKER, footer)
        self.assertEqual(self.load().state["plan_id"], controller.state["plan_id"])

    def test_diagnostics_keeps_audit_link_before_a_plan_or_report_exists(self):
        """A new task still has a discoverable audit link without other diagnostic history."""
        self.tick()
        controller = self.load()
        controller.state.pop("plan", None)
        controller.state.pop("plan_draft", None)
        controller.state["reports"] = []
        summary = controller.summary_body()
        self.assertIn("<details><summary>Diagnostics</summary>", summary)
        self.assertIn("[Automation audit state]", summary)
        self.assertNotIn("[Implementation plan]", summary)

    def test_current_round_identifies_reviewing_and_waiting_agents(self):
        """A round with no submitted votes still shows its assigned reviewers."""
        controller = self.reach_review()
        from policy import design_roster

        roster = design_roster(self.config)
        controller.state["operation"] = {"provider": roster[0]["provider"], "status": "started"}
        summary = controller.summary_body()
        self.assertIn(roster[0]["provider"] + ": Reviewing", summary)
        self.assertIn(roster[1]["provider"] + ": Waiting", summary)
        controller.state.update(stage="escalated", retry_stage="plan_qc")
        self.assertNotIn(": Reviewing", controller.summary_body())
        self.assertNotIn(": Waiting", controller.summary_body())
        for member in roster:
            self.assertIn(member["provider"] + ": Paused", controller.summary_body())
        controller.state["reports"] = [
            {
                "step": "Design review",
                "round": "1 cooperative",
                "provider": roster[0]["provider"],
                "outcome": "approve",
                "artifact": 21,
            }
        ]
        self.assertIn(roster[0]["provider"] + ": ✅ Approve", controller.summary_body())
        self.assertIn(roster[1]["provider"] + ": Paused", controller.summary_body())
        controller.state.update(stage="review", round=0, pr=9, reviews={})
        controller.state.pop("operation")
        self.assertIn("| ↳ Round 1 | Current |", controller.summary_body())
        self.assertIn(": Waiting", controller.summary_body())
        controller.state.update(stage="blocked", retry_stage="review")
        self.assertIn(": Paused", controller.summary_body())
        self.assertNotIn(": Waiting", controller.summary_body())

    def test_now_line_distinguishes_paused_ci_and_human_review(self):
        """Old in-flight operation records cannot make a paused task look active."""
        controller = self.reach_review()
        controller.state.update(stage="escalated", retry_stage="plan_qc")
        self.assertIn("> [!WARNING]\n> Paused", controller.activity_line())
        controller.state.update(stage="gates", reason="CI pending", handoff_ready=False)
        self.assertIn("waiting on: CI pending", controller.activity_line())
        controller.state["handoff_ready"] = True
        self.assertIn("Ready for your review", controller.activity_line())
        self.assertNotIn("CI pending", controller.activity_line())
        controller.state["stage"] = "observed_merged"
        self.assertIn("has been merged", controller.activity_line())

    def test_status_alert_names_all_outstanding_reviewers(self):
        """Both review stages list pending models and remove each one once its vote arrives."""
        from policy import design_roster

        controller = self.reach_review()
        roster = design_roster(self.config)
        self.assertTrue(controller.activity_line().startswith("> [!TIP]\n> "))
        for member in roster:
            self.assertIn("`" + member["provider"] + "`", controller.activity_line())
        controller.state["design_votes"][roster[0]["id"]] = {"report": {"decision": "approve"}}
        self.assertNotIn("`" + roster[0]["provider"] + "`", controller.activity_line())
        self.assertIn("`" + roster[1]["provider"] + "`", controller.activity_line())
        controller.state.update(stage="review", reviews={})
        for provider in self.config["reviewers"]:
            self.assertIn("`" + provider + "`", controller.activity_line())
        controller.state["reviews"][self.config["reviewers"][0]] = {"verdict": "approve"}
        self.assertNotIn("`" + self.config["reviewers"][0] + "`", controller.activity_line())
        self.assertIn("`" + self.config["reviewers"][1] + "`", controller.activity_line())
        controller.state["reviews"][self.config["reviewers"][1]] = {"verdict": "approve"}
        self.assertIn("Checking review findings", controller.activity_line())

    def test_alert_severity_distinguishes_decisions_from_failures(self):
        """Human decisions are important; blocked or refused work is a warning."""
        controller = self.reach_review()
        for alert, stages in (
            ("IMPORTANT", ("planned", "clarify")),
            ("WARNING", ("blocked", "escalated", "rejected")),
            ("TIP", ("intake", "plan", "implement", "gates")),
        ):
            for stage in stages:
                with self.subTest(stage=stage):
                    controller.state["stage"] = stage
                    self.assertTrue(controller.activity_line().startswith(f"> [!{alert}]\n> "))

    def test_correcting_reviewer_is_named_once_alongside_pending_peer(self):
        """Correction progress remains specific without repeating the same reviewer."""
        from policy import approval_binding, design_roster

        controller = self.reach_review()
        roster = design_roster(self.config)
        for correcting in roster:
            with self.subTest(correcting=correcting["provider"]):
                controller.state["review_repair"] = {
                    "binding": digest([approval_binding(controller.state, self.config), correcting]),
                    "attempts": 1,
                }
                line = controller.activity_line()
                for member in roster:
                    self.assertEqual(line.count("`" + member["provider"] + "`"), 1)
                self.assertIn("`" + correcting["provider"] + "` to correct its iterative design review", line)
                self.assertIn("correction 1 of", line)
                self.assertIn("to complete its iterative design review", line)
                self.assertEqual(line.count("We're waiting for"), 1)

    def test_review_models_have_separate_lines_inside_one_table_cell(self):
        """Pending and archived reviewers keep their statuses and links on distinct lines."""
        from policy import design_roster

        controller = self.reach_review()
        roster = design_roster(self.config)
        summary = controller.summary_body()
        self.assertIn(roster[0]["provider"] + ": Waiting<br>" + roster[1]["provider"] + ": Waiting", summary)
        controller.state["reports"] = [
            {
                "step": "Design review",
                "round": "1 cooperative",
                "provider": member["provider"],
                "outcome": "approve",
                "artifact": 21 + i,
            }
            for i, member in enumerate(roster)
        ]
        summary = controller.summary_body()
        self.assertIn("/artifacts/21)<br>[", summary)
        self.assertIn("/artifacts/22)", summary)
        self.assertNotIn("/artifacts/21) · [", summary)

    def test_rejection_history_stays_bounded_through_the_call_budget(self):
        """Repeated invalid reviews retain links rather than copying raw output into the audit."""
        controller = self.reach_review()
        from design import ReviewError
        from policy import design_roster

        member = design_roster(self.config)[0]
        for i in range(48):
            controller.state["calls"] = i + 1
            controller.repair_review({"invalid": "x" * 3000, "attempt": i}, ReviewError("review_shape"), member)
            self.assertLess(len(controller.checkpoints.audit["body"].encode()), LIMIT)
            self.assertNotIn("review_draft", controller.state)
        self.assertLessEqual(len(controller.state["reports"]), 40)
        self.assertLess(len(controller.summary_body().encode()), 60000)
        self.assertEqual(self.load().state["calls"], 48)

    def test_team_uses_each_roles_actual_model_and_effort(self):
        """Distinct plan/PR reviewer assignments and effort settings are printed faithfully."""
        from fakes import named_config

        self.config = named_config()
        self.config["agents"]["plan_reviewers"] = ["opus-high", "sol-medium"]
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        self.tick()
        body = next(
            c["body"] for c in self.gh.comments if "I've picked up this issue and will start digging in!" in c["body"]
        )
        self.assertNotIn("### I'm on it", body)
        rows = [line for line in body.splitlines() if line.startswith("| ")]
        self.assertEqual(len(rows), 7, "header, separator and five roles")
        for row in (
            "| Intake | `gpt-6.1-sol` (medium) |",
            "| Planning | `claude-opus-5-5` (high) |",
            "| Design review | `claude-opus-5-5` (high) · `gpt-6.1-sol` (medium) |",
            "| Implementation | `claude-opus-5-5` (high) |",
            "| PR review | `gpt-6.1-sol` (high) · `gpt-6-astra` (high) |",
        ):
            self.assertIn(row, body)

    def test_welcome_is_sent_once_and_recovers_lost_response(self):
        """The team introduction is recoverable and cannot trigger duplicate intake calls."""
        original, lost = self.gh.repo, []

        def repo(path, method="GET", data=None):
            result = original(path, method, data)
            if (
                method == "POST"
                and path.endswith("/comments")
                and "I've picked up this issue and will start digging in!" in (data or {}).get("body", "")
                and not lost
            ):
                lost.append(True)
                raise RuntimeError("lost response")
            return result

        with patch.object(self.gh, "repo", side_effect=repo), self.assertRaises(PublicationError):
            self.tick()
        self.assertFalse(self.agents.calls)
        self.tick()
        self.tick()
        notices = [
            c["body"] for c in self.gh.comments if "I've picked up this issue and will start digging in!" in c["body"]
        ]
        self.assertEqual(len(notices), 1)
        self.assertIn("I've picked up this issue", notices[0])
        self.assertIn("Here's the team which has currently been assigned:", notices[0])
        self.assertIn("| Role | Model (effort) |", notices[0])
        self.assertIn("| PR review |", notices[0])

    def test_welcome_mentions_the_label_actor_not_the_issue_author(self):
        """Address the enrolling writer from verified enrollment, never inferred issue text."""
        self.gh.issue["user"] = {"login": "reporter"}
        self.tick()
        notice = next(c["body"] for c in self.gh.comments if "I've picked up this issue" in c["body"])
        self.assertIn("@writer I've picked up this issue and will start digging in!", notice)
        self.assertNotIn("@reporter", notice)

    def test_welcome_without_a_valid_label_actor_uses_no_mention(self):
        """Manual or missing enrollment does not guess a recipient or render arbitrary mentions."""
        controller = self.tick()
        for enrollment in (None, {}, {"actor": "@someone"}, {"actor": "writer\n@someone"}):
            with self.subTest(enrollment=enrollment):
                controller.state["enrollment"] = enrollment
                controller.state["conversation"]["pending"] = []
                controller.welcome()
                body = controller.state["conversation"]["pending"][0]["body"]
                self.assertIn("\nI've picked up this issue and will start digging in!", body)
                self.assertNotIn("@", body)
