"""Oversized publication views render raw excerpts without damaging engine-owned Markdown."""

import json
import unittest
from copy import deepcopy

from markdown_it import MarkdownIt

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import reviews
from checkpoints import Checkpoints, legacy_state
from fakes import PLAN, FakeGitHub, intake
from pipeline_fixtures import PipelineFixture
from policy import digest
from protocol import PUBLICATION_BODY_BYTES
from publication import Publication
from test_pr_descriptions import DESCRIPTION


class RenderTests(unittest.TestCase):
    """Parse the actual oversized views, including Unicode and hostile model fences."""

    def assert_valid_html(self, body):
        """All controller disclosures remain real balanced HTML within the body allowance."""
        self.assertLessEqual(len(body.encode()), PUBLICATION_BODY_BYTES)
        html = MarkdownIt("commonmark").render(body)
        self.assertEqual(html.count("<details>"), html.count("</details>"))
        self.assertNotIn("&lt;details>&lt;summary>Diagnostics", body)
        self.assertNotIn("&lt;sub>met", body)
        return html

    def plan_store(self):
        """Use the artifact fake with the real canonical report store."""
        return Checkpoints(FakeGitHub(deepcopy(CONFIG)), CONFIG, 7)

    def test_plan_fallback_has_one_heading_and_intact_diagnostics(self):
        store = self.plan_store()
        plan = {**deepcopy(PLAN), "summary": "🌍 <script>not trusted</script> " * 6000}
        store.reports.put(plan)
        state = {"plan": plan, "plan_id": digest(plan), "base": "a" * 40}
        body = store.plan_body(state)
        html = self.assert_valid_html(body)
        self.assertEqual(html.count("<h1>Concurro implementation plan</h1>"), 1)
        self.assertIn("<summary>Diagnostics</summary>", html)
        self.assertNotIn("<script>", html)
        self.assertIn("Complete plan data, part", body)
        self.assertEqual(state["plan"], plan)

    def test_rejected_draft_takes_precedence_over_old_plan_and_closes_its_fence(self):
        store = self.plan_store()
        output = json.dumps({"summary": "`" * 70000 + "<details>untrusted"})
        draft = {"output": output, "reason": "Invalid fields", "attempt": 1, "truncated": False}
        key = store.reports.put(draft)
        state = {"plan": deepcopy(PLAN), "plan_draft": draft, "plan_id": "p", "base": "a" * 40}
        body = store.plan_body(state)
        html = self.assert_valid_html(body)
        self.assertEqual(html.count("<h1>Concurro implementation plan</h1>"), 1)
        self.assertIn("Rejected draft", html)
        self.assertIn("<summary>Rejected output</summary>", html)
        self.assertEqual(html.count("<pre>"), html.count("</pre>"))
        self.assertIn('class="language-json"', html)
        self.assertIn("Full rejected output", body)
        self.assertIn(str(store.reports.numbers[key][0]), body)
        self.assertEqual(draft["output"], output)

    def test_pr_fallback_keeps_controller_notices_links_and_closing(self):
        description = deepcopy(DESCRIPTION)
        description["lead"] = "Readable lead."
        description["changes"] = ["Long change 🌍 " * 10000]
        state = {
            "issue": 7,
            "head": "a" * 40,
            "base": "b" * 40,
            "plan": deepcopy(PLAN),
            "candidate": {
                "head": "a" * 40,
                "description": description,
                "validation": "Validated.",
                "changed_files": [".github/workflows/ci.yml", "SECURITY.md"],
                "dependencies": {
                    "Cargo.lock": {
                        "counts": {"changed": 1, "added": 0, "removed": 0},
                        "changed": [{"name": "serde", "change": "updated"}],
                    },
                    "package-lock.json": {"counts": {"changed": 0, "added": 2, "removed": 1}, "changed": []},
                },
            },
        }
        for lead in ("Readable lead.", "Long lead 🌍 " * 10000):
            with self.subTest(lead_bytes=len(lead.encode())):
                description["lead"] = lead
                before = deepcopy(state)
                body = Publication(None, None, CONFIG, state, {"number": 42}).pr_body()
                html = self.assert_valid_html(body)
                self.assertTrue(body.startswith(lead[:10]))
                self.assertIn("Full implementation evidence", body)
                self.assertIn("Implementation plan", body)
                self.assertIn("### Review focus", body)
                self.assertIn("Workflow and policy changes (2):", body)
                self.assertIn("`.github/workflows/ci.yml`", body)
                self.assertIn("`SECURITY.md`", body)
                self.assertIn("📦 Dependencies (Cargo.lock): serde updated", html)
                self.assertIn("📦 Dependencies (package-lock.json): 2 added; 1 removed", html)
                self.assertTrue(body.rstrip().endswith("Closes #7"))
                self.assertEqual(state, before)

    def test_review_fallback_preserves_marker_heading_and_diagnostics(self):
        report = {
            "verdict": "changes_requested",
            "summary": "Review complete.",
            "model": "m",
            "design_issue": None,
            "dispositions": [],
            "findings": [
                {"path": "long/" * 50 + "file.md", "line": 1, "start_line": None, "detail": "Evidence 🌍 " * 8}
                for _ in range(200)
            ],
        }
        state = {"issue": 7, "plan_id": "a" * 64}
        reviews.apply(state, "claude", report, "b" * 40)
        item = {"format": reviews.FORMAT, "head": "b" * 40, "sequence": 1, "provider": "claude"}
        body = reviews.review_body(
            state, CONFIG, item, report, state["findings"], {f["id"]: "body" for f in state["findings"]}
        )
        html = self.assert_valid_html(body)
        self.assertTrue(body.startswith(reviews.review_marker(state, CONFIG, 1)))
        self.assertIn("<summary>Diagnostics</summary>", html)
        self.assertIn("Full review and findings", body)
        self.assertEqual(len(state["findings"]), 200)


class HistoryTests(PipelineFixture, unittest.TestCase):
    """Histories retain complete evidence through artifact storage and restart."""

    def test_complete_index_survives_dashboard_shortening(self):
        """Only the rendered view shrinks; the full report index survives save and reload."""
        controller = self.reach("planned")
        entry = controller.state["reports"][0]
        controller.state["reports"] = [
            {**deepcopy(entry), "artifact": i + 1000, "round": str(i + 1)} for i in range(600)
        ]
        before = deepcopy(controller.state["reports"])
        controller.checkpoints.save(controller.state)
        body = controller.summary_body()
        self.assertLess(len(body.encode()), PUBLICATION_BODY_BYTES)
        self.assertNotIn("↳ Round 600", body)
        self.assertEqual(controller.state["reports"], before)
        self.assertEqual(self.loaded().state["reports"], before)

    def test_many_short_history_records_round_trip_by_reference(self):
        """Small records in long collections cannot crowd completed responses out of the audit."""
        controller = self.reach("planned")
        fields = ("qc_history", "design_history", "review_history", "reports")
        histories = {
            field: [{"round": i, "provider": "codex", "summary": "Saved evidence " * 20} for i in range(250)]
            for field in fields
        }
        controller.state.update(deepcopy(histories))
        controller.checkpoints.save(controller.state)
        recovered = self.loaded().state
        for field in fields:
            self.assertEqual(recovered[field], histories[field])
        body = controller.checkpoints.audit["body"]
        self.assertLess(len(body.encode()), PUBLICATION_BODY_BYTES)
        paths = [ref["path"] for ref in legacy_state(body)["report_data"]]
        for field in fields:
            self.assertIn([field], paths)

    def test_history_text_and_repeated_outcomes_are_not_silently_cut(self):
        controller = self.reach("planned")
        summary, reason, title = "Summary 🌍 " * 7000, "Reason 🌍 " * 7000, "Title " * 100
        controller.state["plan"]["summary"] = summary
        controller.state["requirements"]["title"] = title
        report = intake(rationale=reason)
        history = controller._quality_history(controller.state, "intake", "claude", None, None, report)
        controller.state["qc_history"].append(history)
        controller.invalidate_design(reason)
        self.assertEqual(controller.state["design_history"][-1]["summary"], summary)
        self.assertEqual(controller.state["design_history"][-1]["reason"], reason)
        controller.state["stage"] = "blocked"
        for i in range(8):
            controller.state["reason"] = reason + str(i)
            controller.record_outcome()
        controller.save(publish_events=False, outboxes=False)
        saved = self.loaded().state
        self.assertEqual(saved["qc_history"][-1]["rationale"], reason)
        self.assertEqual(saved["design_history"][-1]["summary"], summary)
        self.assertEqual(saved["design_history"][-1]["reason"], reason)
        self.assertGreaterEqual(len(saved["outcomes"]), 8)
        self.assertEqual(saved["outcomes"][-1]["title"], title)
        self.assertEqual(saved["outcomes"][-1]["reason"], reason + "7")
        self.assertEqual(saved["outcomes"][-1]["plan_summary"], summary)
