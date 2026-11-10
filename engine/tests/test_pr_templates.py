"""Explicit template mapping preserves task ownership, source pinning and checklist evidence."""

import unittest
from copy import deepcopy
from unittest.mock import Mock

from support import CONFIG, ROOT, setUpModule as setUpModule, tearDownModule as tearDownModule

import pr_description
import pr_template
from controller import Controller, validate_config
from fakes import FakeGitHub
from policy import Blocked
from test_pr_descriptions import COMMIT, DESCRIPTION, DescribedAgents

TEMPLATE = """<!-- Repository guidance; not public content -->
## Why

## What changed

## Checks

- [x] Tests pass
- [ ] Maintainer approval received

## Maintainer notes

Keep the migration note here.
<script>unsafe()</script>
"""
MAPPING = {"## Why": ["problem"], "## What changed": ["changes", "compatibility"], "## Checks": ["testing"]}


class TemplateTests(unittest.TestCase):
    """Pure mapping contracts and full publication from the pinned source."""

    def parts(self):
        """One complete structured candidate, including authoritative validation."""
        return pr_description.sections(
            {"head": "d" * 40, "description": DESCRIPTION, "validation": "Read the guide."},
            "d" * 40,
            ["Check the boundary."],
            CONFIG["repository"],
        )

    def snapshot(self, text=TEMPLATE):
        """A template bound to the approved base and explicit configuration."""
        return {"base": "a" * 40, "text": text, "sections": deepcopy(MAPPING)}

    def test_mapped_golden_preserves_sections_and_unchecks(self):
        body = pr_template.render(self.parts(), self.snapshot(), "a" * 40)
        expected = (ROOT / "engine/tests/goldens/pr-template.md").read_text()
        self.assertEqual(body + "\n", expected)
        self.assertNotIn("[x]", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn("Repository guidance", body)
        self.assertIn("Keep the migration note here.", body)
        self.assertEqual(body.count("Read the guide."), 1)
        self.assertIn("### Review focus", body)

    def test_missing_ambiguous_or_fenced_heading_is_rejected(self):
        for text in (
            TEMPLATE.replace("## Why", "## Reason"),
            TEMPLATE + "\n## Why\n",
            "```text\n" + TEMPLATE + "\n```",
            TEMPLATE + "\n```text",
        ):
            with self.subTest(text=text), self.assertRaises(Blocked):
                pr_template.render(self.parts(), self.snapshot(text), "a" * 40)
        with self.assertRaisesRegex(Blocked, "approved base"):
            pr_template.render(self.parts(), self.snapshot(), "b" * 40)

    def test_mapping_never_guesses_or_duplicates_fields(self):
        for mapping in (
            {},
            {"Summary": ["lead"]},
            {"## Summary": ["unknown"]},
            {"## A": ["lead"], "## B": ["lead"]},
            {"## A": ["changes", "changes"]},
        ):
            with self.subTest(mapping=mapping), self.assertRaises(Blocked):
                validate_config({**CONFIG, "pr_template_sections": mapping})

    def test_pin_reuses_approved_base_snapshot_and_missing_template_defaults(self):
        state, config = {"base": "a" * 40}, {**CONFIG, "pr_template_sections": MAPPING}
        read, save = Mock(return_value={pr_template.PATH: TEMPLATE}), Mock()
        pr_template.pin(state, config, read, save)
        read.assert_called_once_with("a" * 40, [pr_template.PATH], inspection=True)
        state["head"] = "d" * 40
        read.side_effect = AssertionError("must use the saved source")
        pr_template.pin(state, config, read, save)
        self.assertEqual(save.call_count, 1)
        missing = {**self.snapshot(), "text": None}
        self.assertEqual(pr_template.render(self.parts(), missing, "a" * 40), pr_description.layout(self.parts()))
        with self.assertRaisesRegex(Blocked, "16000"):
            pr_template.pin({"base": "b" * 40}, config, Mock(return_value={pr_template.PATH: "x" * 16001}), save)

    def test_pipeline_renders_template_from_approved_base(self):
        config = {**deepcopy(CONFIG), "autonomous": True, "pr_template_sections": MAPPING}
        gh, agents = FakeGitHub(config), DescribedAgents()
        gh.trees[gh.base][pr_template.PATH] = TEMPLATE
        for _ in range(25):
            controller = Controller(gh, agents, config)
            controller.advance(7)
            if controller.state and controller.state.get("handoff_ready"):
                break
        self.assertTrue(controller.state.get("handoff_ready"), controller.state.get("reason"))
        self.assertEqual(controller.state["pr_template"]["base"], controller.state["base"])
        self.assertIn("## What changed", gh.pr["body"])
        self.assertNotIn("[x]", gh.pr["body"])
        self.assertEqual(controller.state["candidate"]["commit"], COMMIT)
        self.assertIn("Implementation plan", gh.pr["body"])
