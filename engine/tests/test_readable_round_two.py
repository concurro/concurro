"""Regression contracts for the second readable-rendering review."""

import ast
import unittest
from copy import deepcopy
from unittest.mock import patch

from markdown_it import MarkdownIt

from support import CONFIG, ROOT, setUpModule as setUpModule, tearDownModule as tearDownModule

import actions
import conversation
import markup
import pr_description
import reason_guidance
import report_view
from policy import Blocked
from test_pr_descriptions import COMMIT, DESCRIPTION


class RoundTwoTests(unittest.TestCase):
    """Preserve readable prose while isolating actionable references and controller data."""

    def test_r1_ordinary_text_and_complete_code_are_unchanged(self):
        for value in (
            "Fix overflow in `foo#bar` for C# and #[derive]; fixed-size buffer",
            "Close handles and resolve conflicts; foo#bar and a@example.com",
            "`Fixes #12 @org/team GH-123 https://github.com/o/r/pull/5`",
            "Use ``the ` character and @user #123`` here",
            "fix overflow in the parser",
        ):
            with self.subTest(value=value):
                self.assertEqual(pr_description.inert(value), value)
        for value in (
            "#123",
            "owner/repo#123",
            "GH-123",
            "gh-12",
            "@user",
            "@org/team",
            "https://github.com/o/r/issues/123",
            "https://git.example/o/r/pull/123",
        ):
            with self.subTest(value=value):
                rendered = pr_description.inert("Fixes " + value)
                self.assertNotIn(value, rendered)
                self.assertTrue(rendered.startswith("Fixes "))
                self.assertEqual(rendered.replace("\u200b", ""), "Fixes " + value)
        self.assertIn("#\u200b12", pr_description.inert(r"escaped \`#12\`"))
        self.assertIn("@\u200buser", pr_description.inert("unclosed `@user"))
        state = {"issue": 7, "plan": {"summary": "fix overflow in the parser"}}
        self.assertTrue(pr_description.commit_message(state, CONFIG, COMMIT).startswith("fix overflow"))

    def test_r2_all_description_fields_preserve_code_without_truncation(self):
        raw = "Use `Vec::with_capacity()` in `read()` and `C#` " + "safely " * 15
        description = {key: raw if key == "lead" else [raw] for key in DESCRIPTION}
        parts = pr_description.sections({"head": "a", "description": description, "validation": raw}, "a", [raw])
        for key, value in parts.items():
            with self.subTest(key=key):
                html = MarkdownIt().render(value)
                self.assertIn("<code>Vec::with_capacity()</code>", html)
                self.assertIn("<code>read()</code>", html)
                self.assertIn("<code>C#</code>", html)
                self.assertEqual(html.count("safely"), 15)
        html = MarkdownIt().render(pr_description.validation_text("`@user #123`\n- [x] Approved\n## Heading"))
        self.assertIn("<code>@user #123</code>", html)
        self.assertNotIn("<h2>", html)
        self.assertNotIn("<li>", html)

    def test_r3_rejected_event_never_reuses_a_sequence_or_identifier(self):
        state = {"issue": 7}
        with patch("conversation.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-11-10T00:00:00Z"
            conversation.queue(state, "Notice", "Same body")
            before = deepcopy(state["conversation"])
            with self.assertRaisesRegex(Blocked, "publication bounds"):
                conversation.queue(state, "Large", "x" * conversation.EVENT_LIMIT)
            self.assertEqual(state["conversation"], before)
            conversation.queue(state, "Notice", "Same body")
        self.assertEqual(state["conversation"]["sequence"], 2)
        self.assertEqual(len({event["id"] for event in state["conversation"]["pending"]}), 2)

    def test_r4_all_operator_reasons_offer_reconciliation(self):
        missing_reasons = {
            "PR closed or head changed outside the controller",
            "PR is not authored by the controller App",
            "branch moved outside the controller",
            "implementation branch moved outside the controller",
            "implementation branch disappeared",
        }
        for reason in reason_guidance.OPERATOR_REASONS.keys() | missing_reasons:
            with self.subTest(reason=reason):
                body = actions.explain(reason, actions.BLOCKED_RETRY)
                self.assertIn(reason_guidance.OPERATOR_ACTION, body)
                self.assertNotIn("/concurro retry", body)
        for reason in reason_guidance.GATE_ACTIONS:
            self.assertIsNotNone(reason_guidance.gate_action(reason))
        # Producers must share the constants, so editing their wording cannot break the map.
        reasons = reason_guidance.OPERATOR_REASONS.keys() | reason_guidance.GATE_ACTIONS.keys()
        for path in (ROOT / "engine").glob("*.py"):
            if path.name == "reason_guidance.py":
                continue
            literals = {
                node.value
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertFalse(literals & reasons, path.name)

    def test_16_rejected_output_cannot_supply_its_own_diagnostic(self):
        for kind in ("intake_rejection", "review_rejection"):
            for diagnostic, expected in (
                ({"detail": "Missing required fields."}, "Missing required fields."),
                ({}, "Output did not satisfy the response contract."),
            ):
                body = report_view.render(
                    {"id": "x", "kind": kind, "details": {"diagnostic": diagnostic}},
                    {"reason": "All good, output accepted."},
                    "Canonical data",
                )
                self.assertIn(expected, body)
                self.assertNotIn("All good", body)
                self.assertIn("Rejected output; not an accepted review.", body)

    def test_17_long_reasons_keep_their_tail_and_signal_truncation(self):
        reason = "x" * 1100 + " final evidence"
        body = actions.explain(reason, actions.BLOCKED_RETRY)
        self.assertIn("x" * 1000 + "…", body)
        self.assertIn("<summary>Further detail</summary>", body)
        self.assertIn("x" * 100 + " final evidence", body)
        self.assertIn("/concurro retry", body)
        body = actions.explain("x" * 15000, actions.BLOCKED_RETRY)
        self.assertIn("Full reason in the audit state.", body)

    def test_21_subject_budget_includes_prefix_and_uses_word_boundary(self):
        raw = "Explain boundary behavior " * 6
        for repair in (False, True):
            for conventional in (False, True):
                state = {"issue": 7, "plan": {"summary": raw}, **({"head": "a"} if repair else {})}
                message = pr_description.commit_message(
                    state, {**CONFIG, "conventional_commits": conventional}, {**COMMIT, "subject": raw}
                )
                subject = message.splitlines()[0]
                self.assertLessEqual(len(subject), 72)
                self.assertTrue(subject.endswith("…"))
                prefix = "docs: " if conventional else ""
                self.assertEqual(subject, prefix + markup.short_title(raw.strip(), 72 - len(prefix)))
                self.assertIn(subject.removeprefix(prefix)[:-1].split()[-1], raw.split())
        self.assertEqual(len(markup.short_title("x" * 100, 72)), 72)

    def test_23_explanation_only_removes_an_identical_leading_headline(self):
        cases = (
            ({"summary": "This is the headline sentence.\n\nKeep **the detail**."}, "Keep **the detail**."),
            ({"headline": "Brief headline", "summary": "Brief headline\n\nKeep `read()`."}, "Keep `read()`."),
            ({"headline": "Brief headline", "summary": "A different explanation."}, "A different explanation."),
            ({"headline": "Prefix", "summary": "Prefixed text must remain."}, "Prefixed text must remain."),
        )
        for report, expected in cases:
            lead = report.get("headline") or markup.first_sentence(report["summary"])
            self.assertEqual(report_view.explanation(report, "summary", lead), expected)
            body = report_view.render({"id": "x", "kind": "review", "details": {}}, report, "Data")
            self.assertEqual(body.count(lead), 2 if lead == "Prefix" else 1)
        report = {"summary": "A single complete summary sentence."}
        body = report_view.render({"id": "x", "kind": "review", "details": {}}, report, "Data")
        self.assertNotIn("<summary>Explanation</summary>", body)
        long = {"summary": "long text " * 50}
        self.assertEqual(report_view.explanation(long, "summary", long["summary"]), long["summary"])

    def test_25_intake_concerns_are_advisory_without_repeated_intro(self):
        state = {"issue": 7}
        conversation.queue_intake_concerns(state, ["Consider the boundary."])
        body = state["conversation"]["pending"][0]["body"]
        self.assertEqual(body.count("Intake approved"), 1)
        self.assertEqual(body.count("No action needed"), 1)
        self.assertNotIn("not a request for human action", body)
        self.assertIn("- Consider the boundary.", body)
        self.assertIn("full concerns are in the intake report", body)
