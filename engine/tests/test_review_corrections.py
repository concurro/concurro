"""Regressions for the readable-rendering review's requested presentation and description fixes."""

import json
import re
import unittest
from copy import deepcopy
from unittest.mock import patch

from markdown_it import MarkdownIt

from support import CONFIG, ROOT, setUpModule as setUpModule, tearDownModule as tearDownModule

import actions
import conversation
import markup
import pr_description
import pr_template
import report_view
import reviews
from controller import Controller
from dashboard import Dashboard
from fakes import FakeAgents, FakeGitHub
from policy import Blocked
from test_pr_descriptions import COMMIT, DESCRIPTION, DescribedAgents
from test_rendering_helpers import samples


class RenderingCorrections(unittest.TestCase):
    """Model content stays inert in each new rendering context."""

    def test_1_whitespace_code_spans_never_expose_html(self):
        md = MarkdownIt().enable("table")
        for whitespace in (" ", "  ", "\t", "\n"):
            for tail in ("``a`<img src=x>``", "``a`<script>x</script>``", "`a|b` <img src=x>"):
                raw = "Fix `" + whitespace + "` " + tail
                finding = {
                    "id": "F1",
                    "title": raw,
                    "detail": raw,
                    "format": 4,
                    "severity": "low",
                    "provider": "claude",
                    "path": None,
                }
                outputs = [
                    reviews.finding_body(finding),
                    markup.table(["Finding"], [[markup.finding_title_v2(finding, cell=True)]]),
                    report_view.render(
                        {"id": "x", "kind": "review", "details": {}},
                        {"verdict": "approve", "summary": raw, "findings": [finding]},
                        "",
                    ),
                ]
                for body in outputs:
                    with self.subTest(raw=raw, body=body):
                        html = md.render(body)
                        self.assertNotIn("<img", html)
                        self.assertNotIn("<script", html)

    def test_13_pipes_are_literal_outside_tables(self):
        md = MarkdownIt().enable("table")
        self.assertIn("<code>a|b</code>", md.render(markup.inline("Use `a|b`")))
        self.assertIn("<code>a|b</code>", md.render(markup.table(["Title"], [[markup.inline("Use `a|b`", cell=True)]])))

    def test_14_report_summaries_and_objections_keep_code_spans(self):
        body = report_view.render(
            {"id": "x", "kind": "qc", "details": {}},
            {
                "decision": "approve",
                "rationale": "Check `read()`.",
                "objection_updates": [{"id": "O1", "status": "open", "detail": "Define `<limit>`."}],
            },
            "",
        )
        html = MarkdownIt().render(body)
        self.assertIn("<code>read()</code>", html)
        self.assertIn("<code>&lt;limit&gt;</code>", html)

    def test_9_intake_does_not_copy_or_limit_requirement_labels(self):
        state, source, details = deepcopy(samples()["qc_intake"])
        state["requirements"] = {"body": "\n\n".join("Long requirement " + "x" * 600 for _ in range(40))}
        with patch("conversation.design.criteria", side_effect=AssertionError("intake must not compute design labels")):
            for _ in range(4):
                conversation.queue(state, None, source=source, kind="qc", details=details)
        self.assertLess(len(json.dumps(state["conversation"]).encode()), 6000)
        self.assertTrue(all("criterion_labels" not in e["details"] for e in state["conversation"]["pending"]))
        sequence = state["conversation"]["sequence"]
        with patch("conversation.render_v2", side_effect=Blocked("bad event")), self.assertRaises(Blocked):
            conversation.queue(state, None, source=source, kind="qc", details=details)
        self.assertEqual(state["conversation"]["sequence"], sequence)

    def test_7_gate_activity_distinguishes_human_action(self):
        for reason in (
            "PR returned to draft; a human must mark it ready",
            "CI suite unsuccessful: 23",
            "PR is closed",
            "unresolved review threads",
            "head changed outside the controller",
        ):
            view = object.__new__(Dashboard)
            view.state = {"stage": "gates", "reason": reason}
            with self.subTest(reason=reason):
                self.assertIn("[!WARNING]", view.activity_line())
                self.assertNotIn("No action needed", view.activity_line())
        view.state["reason"] = "GitHub test-merge commit is not current"
        self.assertIn("[!NOTE]", view.activity_line())
        self.assertIn("No action needed", view.activity_line())

    def test_8_operator_guidance_matches_reasons_not_model_words(self):
        for reason in ("The `?` operator needs explanation.", "Manual reconciliation needs documentation."):
            body = actions.explain(reason, "Use `/concurro retry --plan-revisions 1`.", {"stage": "escalated"})
            self.assertIn("/concurro retry --plan-revisions 1", body)
            self.assertNotIn("An operator must", body)
        for reason in (
            "prepared publication must be reconciled before reopening design",
            "cannot reconcile accumulated PR changes",
            "PR review publication failed repeatedly; inspect the PR and its review threads",
        ):
            body = actions.explain(reason, actions.BLOCKED_RETRY, {"stage": "blocked"})
            self.assertNotIn("/concurro retry", body)
            self.assertIn("An operator must", body)

    def test_5_6_and_11_model_fields_cannot_issue_github_actions(self):
        raw = "Fixes #12 and owner/repo#34; @org/team https://github.com/o/r/issues/5"
        candidate = {
            "head": "a" * 40,
            "description": {**DESCRIPTION, "lead": raw, "changes": [raw]},
            "validation": "- [x] Security reviewed\n## Maintainer approval\n" + raw,
        }
        parts = pr_description.sections(candidate, candidate["head"], [raw])
        body = pr_template.render(
            parts, {"base": "b", "text": "## Checks\n- [ ] Verified", "sections": {"## Checks": ["testing"]}}, "b"
        )
        self.assertNotIn("<h2>Maintainer approval", MarkdownIt().render(body))
        self.assertNotIn("- [x]", body)
        self.assertNotIn("@org/team", body)
        self.assertNotIn("Fixes #12", body)
        self.assertNotIn("owner/repo#34", body)
        self.assertNotIn("/issues/5", body)
        for repair in (False, True):
            state = {"issue": 7, "round": 1, "plan": {"headline": raw}, **({"head": "a"} if repair else {})}
            msg = pr_description.commit_message(
                state,
                {"repository": "issues/repo", "upstream": "upstream/repo"},
                {**COMMIT, "subject": raw, "changes": [raw]},
            )
            self.assertNotIn("Fixes #12", msg)
            self.assertNotIn("@org/team", msg)
            self.assertTrue(msg.endswith("Refs https://github.com/issues/repo/issues/7"))


class DescriptionRecovery(unittest.TestCase):
    """Exercise migration and model repair through real controller checkpoints."""

    def run_to(self, gh, agents, config, stage):
        """Advance a bounded simulated task to one stage."""
        for _ in range(35):
            c = Controller(gh, agents, config)
            c.advance(7)
            if c.state and c.state["stage"] == stage:
                return c
        self.fail(c.state.get("reason"))

    def test_3_legacy_body_survives_invalidation_and_refreshes(self):
        config = {**deepcopy(CONFIG), "autonomous": True}
        for edited in (False, True):
            with self.subTest(edited=edited):
                gh = FakeGitHub(config)
                c = self.run_to(gh, FakeAgents(), config, "gates")
                self.assertNotIn("pr_description", c.state)
                if edited:
                    gh.pr["body"] = "Human description"
                gh.comment("/concurro invalidate")
                # A fresh generation reaches intake before its new structured implementation.
                c = Controller(gh, DescribedAgents(), config)
                c.advance(7)
                c = self.run_to(gh, DescribedAgents(), config, "gates")
                if edited:
                    self.assertEqual(gh.pr["body"], "Human description")
                    self.assertTrue(c.state["pr_description"]["human_edited"])
                else:
                    self.assertIn("### Problem", gh.pr["body"])
                    self.assertFalse(c.state["pr_description"].get("human_edited"))

    def test_4_invalid_metadata_gets_bounded_model_feedback(self):
        config = {**deepcopy(CONFIG), "autonomous": True}
        for bad in ("null-description", "null-commit", "whitespace"):

            class Repair(DescribedAgents):
                """Return one invalid description before correcting it."""

                def ask(self, provider, stage, context):
                    result = super().ask(provider, stage, context)
                    if stage == "develop" and not context.get("implementation_feedback"):
                        if bad == "whitespace":
                            result["description"]["lead"] = "   "
                        else:
                            result[bad.removeprefix("null-")] = None
                    return result

            with self.subTest(bad=bad):
                gh, agents = FakeGitHub(config), Repair()
                self.run_to(gh, agents, config, "gates")
                calls = [ctx for _, stage, ctx in agents.calls if stage == "develop"]
                self.assertEqual(len(calls), 2)
                self.assertIn("corrected implementation response", calls[-1]["implementation_feedback"])
        schema = json.loads((ROOT / "bundle/schemas/develop.json").read_text())
        for key in ("description", "commit"):
            self.assertEqual(schema["properties"][key]["type"], "object")
        field = schema["properties"]["description"]["properties"]["lead"]
        self.assertIsNone(re.fullmatch(field["pattern"], "   "))
