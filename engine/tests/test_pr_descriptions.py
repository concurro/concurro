"""PR descriptions bind to candidates and survive refresh races without overwriting humans."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, ROOT, setUpModule as setUpModule, tearDownModule as tearDownModule

import candidates
import pr_description
import prompts
from bundle import Bundle
from controller import Controller, validate_config
from fakes import FakeAgents, FakeGitHub
from policy import Blocked, digest
from publication import Publication

DESCRIPTION = {
    "lead": "Define the guide's boundary behavior.",
    "problem": ["The guide leaves the boundary undefined."],
    "changes": ["docs/guide.md explains the boundary."],
    "compatibility": [],
}
COMMIT = {
    "subject": "Clarify the boundary example",
    "type": "docs",
    "changes": ["Explain the boundary in docs/guide.md."],
}


class DescribedAgents(FakeAgents):
    """Return current implementer fields while retaining the existing end-to-end scenarios."""

    def ask(self, provider, stage, context, *args, **kwargs):
        """Attach structured fields to implementation results."""
        result = super().ask(provider, stage, context, *args, **kwargs)
        if stage == "develop":
            result.update(description=deepcopy(DESCRIPTION), commit=deepcopy(COMMIT))
        return result


class DescriptionTests(unittest.TestCase):
    """Rendering, ownership and whole-pipeline integration."""

    def setUp(self):
        """Use the existing fake GitHub transport and a single owned PR body."""
        self.config = deepcopy(CONFIG)
        self.gh = FakeGitHub(self.config)
        self.gh.pr = {"number": 9, "body": "original"}
        self.state = {"pr_description": {"last": digest("original")}}
        self.saved = []

    def save(self):
        """Capture exact durable receipts at write boundaries."""
        self.saved.append(deepcopy(self.state))

    def test_human_edit_permanently_stops_refresh(self):
        self.gh.pr["body"] = "human wording"
        pr_description.refresh(self.state, self.gh, self.gh.pr, "new", self.save)
        self.assertEqual(self.gh.pr["body"], "human wording")
        self.assertTrue(self.state["pr_description"]["human_edited"])
        self.gh.pr["body"] = "original"
        pr_description.refresh(self.state, self.gh, self.gh.pr, "new", self.save)
        self.assertEqual(self.gh.pr["body"], "original")

    def test_lost_patch_response_recovers_intended_digest(self):
        original = self.gh.repo

        def repo(path, method="GET", data=None):
            """Lose the response after applying a body patch."""
            result = original(path, method, data)
            if method == "PATCH":
                self.assertEqual(self.saved[-1]["pr_description"]["intended"], digest("new"))
                raise OSError("lost response")
            return result

        with patch.object(self.gh, "repo", side_effect=repo), self.assertRaises(OSError):
            pr_description.refresh(self.state, self.gh, self.gh.pr, "new", self.save)
        pr_description.refresh(self.state, self.gh, self.gh.pr, "new", self.save)
        self.assertEqual(self.state["pr_description"]["last"], digest("new"))
        self.assertNotIn("human_edited", self.state["pr_description"])

    def test_bound_render_and_cross_repository_closing_link(self):
        state = {
            "issue": 7,
            "head": "d" * 40,
            "plan": {"summary": "Legacy", "publication": {"review_focus": ["Check the boundary."]}},
            "candidate": {
                "head": "d" * 40,
                "description": deepcopy(DESCRIPTION),
                "validation": "Read the guide. Tests not run.",
            },
        }
        publication = Publication(self.gh, self.gh, {**self.config, "upstream": "other/repo"}, state, {"number": 3})
        body = publication.pr_body()
        expected = json.loads((ROOT / "engine/tests/goldens/pr-description.json").read_text())
        self.assertEqual(body, expected["body"])
        self.assertIn("Closes https://github.com/", body)
        state["candidate"]["head"] = "e" * 40
        with self.assertRaisesRegex(Blocked, "candidate head"):
            publication.pr_body()

    def test_round_specific_commit_messages(self):
        state = {"issue": 7, "round": 1, "plan": {"headline": "Explain guide boundaries", "summary": "Dense plan"}}
        message = pr_description.commit_message(state, {}, COMMIT)
        self.assertTrue(message.startswith("Explain guide boundaries\n"))
        self.assertIn("- Explain the boundary in docs/guide.md.\n\nRefs #7", message)
        state["head"] = "d" * 40
        self.assertTrue(pr_description.commit_message(state, {}, COMMIT).startswith("Clarify the boundary example\n"))
        self.assertTrue(
            pr_description.commit_message(state, {"conventional_commits": True}, COMMIT).startswith("docs: Clarify")
        )
        with self.assertRaises(Blocked):
            validate_config({**self.config, "conventional_commits": "yes"})

    def test_bounds_and_legacy_retained_results(self):
        legacy = {"summary": "Changed", "validation": "Checked", "edits": [], "design_issue": None}
        candidates.development(legacy)
        report = {**legacy, "description": deepcopy(DESCRIPTION), "commit": deepcopy(COMMIT)}
        candidates.development(report)
        for field, value in (("lead", ""), ("changes", ["new\nheading"]), ("problem", [])):
            changed = deepcopy(report)
            changed["description"][field] = value
            with self.assertRaises(Blocked):
                candidates.development(changed)

    def test_implementer_prompt_requires_final_description_check(self):
        """The existing implementation turn checks its final description and discloses refinements."""
        request = {
            "stage": "develop",
            "context": {"repository_commit": "a" * 40, "repository": {"refs": {}, "head": ""}},
        }
        prompt = json.loads(prompts.prompt_for(request, Bundle()))
        instructions = " ".join(prompt["instructions"]["skill"].split())
        for requirement in (
            "Before returning your final report in this turn",
            "complete base-to-head diff and actual validation results",
            "Correct stale names, omitted changes and unsupported claims",
            "Highlight deviations from the plan in `description.review_focus`",
            "requirements, acceptance criteria, architecture or scope must change",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, instructions)

    def test_reviewer_prompt_distinguishes_refinements_from_design_changes(self):
        """The delivered review policy permits equivalent details without waiving design commitments."""
        request = {
            "stage": "review",
            "context": {"repository_commit": "a" * 40, "repository": {"refs": {}, "head": ""}},
        }
        prompt = json.loads(prompts.prompt_for(request, Bundle()))
        instructions = " ".join(prompt["instructions"]["skill"].split())
        for requirement in (
            "Accept implementation refinements that preserve requirements, acceptance criteria and approved design intent",
            "renamed helpers, equivalent implementation choices and corrected description wording",
            "Do not reopen solely because such details differ from the plan",
            "substantive departure from approved requirements, acceptance criteria, architecture or scope",
            "return `reopen` with structured `design_issue` evidence",
            "Request changes for implementation defects",
            "Check `published_pr_body`",
            "Raise a finding for a materially misleading or incomplete description",
            "Reopen only if the underlying change requires it, not solely because disclosure is missing",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, instructions)
        self.assertNotIn("drift from the approved design even within scope", instructions)

    def test_pipeline_reviewers_receive_actual_published_body(self):
        config = {**self.config, "autonomous": True}
        gh, agents = FakeGitHub(config), DescribedAgents()
        for _ in range(20):
            controller = Controller(gh, agents, config)
            controller.advance(7)
            if controller.state and controller.state.get("handoff_ready"):
                break
        self.assertTrue(controller.state.get("handoff_ready"), controller.state.get("reason"))
        self.assertEqual(controller.state["candidate"]["description"], DESCRIPTION)
        self.assertIn("### Testing", gh.pr["body"])
        contexts = [c for _, stage, c in agents.calls if stage == "review"]
        self.assertEqual(len(contexts), len(config["reviewers"]))
        self.assertTrue(all(c["plan"] == controller.state["plan"] for c in contexts))
        self.assertTrue(all(c["published_pr_body"] == gh.pr["body"] for c in contexts))
        gh.pr["body"] = "Human's actual description"
        context = controller.pr_review_context("claude", {}, {}, controller.state["head"])
        self.assertEqual(context["published_pr_body"], gh.pr["body"])
