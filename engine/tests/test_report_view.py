"""Readable report archives retain complete data and historical readers."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import conversation
import report_view
from fakes import FakeGitHub
from test_rendering_helpers import samples


def views():
    """Fixed accepted and rejected reports in the readable archive format."""
    bodies = {}
    for name, (state, source, details) in samples().items():
        kind = "qc" if name.startswith("qc_") else name
        title = conversation.render_v2(state, kind, source, details)[0]
        event = {"id": "sample", "title": title, "kind": kind, "details": details}
        bodies[name] = report_view.render(
            event,
            conversation.source_value(state, source),
            "- [Full report](https://github.com/o/r/issues/7/artifacts/2)",
        )
    return bodies


class ReportViewTests(unittest.TestCase):
    """Exercise versioned rendering, capacity and lost-response recovery."""

    def test_golden_views(self):
        expected = Path(__file__).parent / "goldens/report-views.json"
        self.assertEqual(views(), json.loads(expected.read_text()))
        for body in views().values():
            self.assertIn("<summary>Raw data</summary>", body)
            self.assertNotIn("```json", body)

    def test_oversized_report_keeps_all_data_links(self):
        state, source, details = samples()["review"]
        report = conversation.source_value(state, source)
        report["summary"] = "</details>" * 50000
        body = report_view.render(
            {"id": "x", "kind": "review", "details": details}, report, "[Full report](https://example.com/data)"
        )
        self.assertLessEqual(len(body.encode()), 60000)
        self.assertIn("[Full report]", body)
        self.assertIn("F2", body)

    def test_lost_archive_response_reuses_readable_body_and_full_data(self):
        for name, sample in samples().items():
            with self.subTest(name=name):
                if name == "plan_summary":
                    continue  # Retired event kind remains covered by legacy renderer fixtures.
                state, source, details = deepcopy(sample)
                kind = "qc" if name.startswith("qc_") else name
                conversation.queue(state, None, source=source, kind=kind, details=details)
                self.assertEqual(state["conversation"]["pending"][0]["format"], 5)
                gh = FakeGitHub(CONFIG)
                original = gh.repo
                lost = []

                def repo(path, method="GET", data=None):
                    """Lose exactly the readable artifact's successful response."""
                    result = original(path, method, data)
                    if method == "POST" and "<summary>Raw data</summary>" in (data or {}).get("body", "") and not lost:
                        lost.append(True)
                        raise OSError("lost response")
                    return result

                with patch.object(gh, "repo", side_effect=repo):
                    with self.assertRaises(conversation.PublicationError):
                        conversation.publish(state, gh, CONFIG)
                conversation.publish(state, gh, CONFIG)
                readable = [
                    i["artifact"]["body"]
                    for i in gh.artifacts[7].values()
                    if "<summary>Raw data</summary>" in i["artifact"]["body"]
                ]
                self.assertEqual(len(readable), 1)
                self.assertFalse(state["conversation"]["pending"])
