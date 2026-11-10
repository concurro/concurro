"""Readable formats retain old publication bytes and escape model-controlled structure."""

import json
import unittest
from pathlib import Path

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import actions
import conversation
import format_samples
import markup
import reviews
from checkpoints import Checkpoints

GOLDENS = Path(__file__).parent / "goldens"


def samples():
    """Representative new review, action, plan and intake renderings."""
    result = format_samples.publication_cases(reviews, 4)
    state = {"issue": 7}
    conversation.queue_intake_concerns(state, ["Consider `<limit>` behavior."])
    result["intake"] = state["conversation"]["pending"][0]["body"].split("\n", 1)[1]
    result["blocked"] = actions.explain("The task cannot proceed. A dependency is unavailable.", actions.BLOCKED_RETRY)
    state.update(
        plan_id="a" * 64,
        base="b" * 40,
        plan={
            "summary": "Make the output readable.",
            "questions": [],
            "files": ["engine/markup.py"],
            "steps": ["Escape <script> tags", "Keep code spans"],
            "tests": ["Run renderer tests"],
        },
    )
    result["plan"] = Checkpoints.plan_body(None, state)
    return result


class ReadableRenderingTests(unittest.TestCase):
    """Exercise code-span edge cases and immutable renderer versions."""

    def test_golden_formats(self):
        self.assertEqual(samples(), json.loads((GOLDENS / "readable.json").read_text()))
        self.assertEqual(
            format_samples.publication_cases(reviews, 3), json.loads((GOLDENS / "format3-pr.json").read_text())
        )

    def test_inline_code_is_bounded_and_table_safe(self):
        for raw, expected in (
            ("Use `.clone()` now", "Use `.clone()` now"),
            ("`a|b`", "`a\\|b`"),
            ("``a`b``", "``a`b``"),
            ("<script> *bold*", "&lt;script&gt; \\*bold\\*"),
            ("Use `unfinished", "Use \\`unfinished"),
            (r"escaped \`tick", r"escaped \\\`tick"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(markup.inline(raw), expected)
        self.assertEqual(markup.inline("`abcdef`", 5), "\\`abc…")

    def test_operator_failure_does_not_offer_retry(self):
        body = actions.explain("Stored state conflicts; reconcile manually", actions.BLOCKED_RETRY)
        self.assertNotIn("/concurro retry", body)
        self.assertIn("An operator must", body)

    def test_existing_queued_body_is_unchanged(self):
        event = {"body": "old body\n\n<sub>Event 1</sub>"}
        self.assertEqual(conversation.event_body({}, event), event["body"])
