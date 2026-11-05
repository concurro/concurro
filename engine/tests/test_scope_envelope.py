"""Scope envelope: a plan approves a design, an area and a budget; the implementer edits within them."""

from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from support import CONFIG, EXECUTION, setUpModule, tearDownModule  # unittest module fixtures
from test_findings import Harness, Reviewers
from test_pipeline import APPROVE, PLAN
import checks
from controller import DISPATCH_ALLOWANCE, Controller
from policy import Blocked, OutsideScope, validate_edits, validate_plan
from snapshot import inspection
from readiness import review_reasons

SCOPE = {"paths": ["docs/**"], "max_files": 3, "max_changed_lines": 50}


class EnvelopeAgents(Reviewers):
    """Plans with an envelope; the implementer first reaches outside it `outside` times, then edits a call site.

    A repair turn after that edit changes only the primary file.
    """

    def __init__(self, outside=1, stray="guides/elsewhere.md"):
        super().__init__(set())
        self.outside, self.stray = outside, stray

    def ask(self, provider, stage, context):
        if stage == "plan":
            self.calls.append((provider, stage, deepcopy(context)))
            return {**deepcopy(PLAN), "scope": deepcopy(SCOPE)}
        if stage == "develop":
            self.calls.append((provider, stage, deepcopy(context)))
            if self.outside:
                self.outside -= 1
                return {"summary": "Too wide.", "edits": [{"path": self.stray, "content": "x\n"},
                                                          {"path": "docs/guide.md", "content": "Updated\n"}],
                        "validation": "", "design_issue": None}
            if context.get("files", {}).get("docs/guide.md") == "Updated guide\n":
                return {"summary": "Repair the guide.", "validation": "", "design_issue": None,
                        "edits": [{"path": "docs/guide.md", "content": "Repaired guide\n"}]}
            return {"summary": "Explain the guide and update its caller.", "validation": "", "design_issue": None,
                    "edits": [{"path": "docs/guide.md", "content": "Updated guide\n"},
                              {"path": "docs/caller.md", "content": "See the updated guide.\n"}]}
        return super().ask(provider, stage, context)


class ContractTests(unittest.TestCase):
    """A plan's scope is validated, and its envelope decides the approved profiles."""

    def test_scope_validation(self):
        validate_plan({**deepcopy(PLAN), "scope": deepcopy(SCOPE)}, CONFIG, EXECUTION)
        validate_plan(deepcopy(PLAN), CONFIG, EXECUTION)
        for name, scope in {"no globs": {**SCOPE, "paths": []}, "absolute": {**SCOPE, "paths": ["/docs/**"]},
                            "traversal": {**SCOPE, "paths": ["docs/../src/**"]},
                            "outside the files": {**SCOPE, "paths": ["src/**"]},
                            "fewer files than planned": {**SCOPE, "max_files": 0},
                            "over the file cap": {**SCOPE, "max_files": 33},
                            "over the line cap": {**SCOPE, "max_changed_lines": 3001},
                            "no line budget": {**SCOPE, "max_changed_lines": None},
                            "extra key": {**SCOPE, "x": 1}}.items():
            with self.subTest(name=name), self.assertRaises(Blocked):
                validate_plan({**deepcopy(PLAN), "scope": scope}, CONFIG, EXECUTION)


    def test_out_of_envelope_proposals_are_refused_whole(self):
        plan = {**deepcopy(PLAN), "scope": deepcopy(SCOPE)}
        edits = [{"path": "docs/guide.md", "content": "a"}, {"path": "guides/x.md", "content": "b"}]
        with self.assertRaises(OutsideScope) as raised:
            validate_edits({"summary": "s", "edits": edits}, plan, {}, CONFIG, EXECUTION)
        self.assertEqual(raised.exception.paths, ["guides/x.md"])
        validate_edits({"summary": "s", "edits": [edits[0], {"path": "docs/new.md", "content": "c"}]},
                       plan, {}, CONFIG, EXECUTION)


class ImplementationTests(Harness, unittest.TestCase):
    """The implementer edits inside the envelope; reaching outside it is feedback, not a block."""

    def test_call_site_inside_the_envelope_reaches_review_and_gates(self):
        agents = EnvelopeAgents()
        state = self.run_to(agents, "gates").state
        developments = [c for _, stage, c in agents.calls if stage == "develop"]
        self.assertIn("outside the approved scope or of a protected path: guides/elsewhere.md",
                      developments[1]["implementation_feedback"])
        self.assertEqual(sorted(state["changed_files"]), ["docs/caller.md", "docs/guide.md"])
        reviews = [c for _, stage, c in agents.calls if stage == "review"]
        self.assertEqual([c["path"] for c in reviews[0]["changes"]], ["docs/caller.md", "docs/guide.md"],
                         "reviews see every changed file")
        self.assertNotIn("unapproved path", self.reasons(state))
        self.assertNotIn("guides/elsewhere.md", json.dumps(self.gh.trees))

    def test_a_protected_path_is_feedback_even_where_the_workspace_cannot_read_it(self):
        agents = EnvelopeAgents(stray=".gitignore")
        self.run_to(agents, "gates")
        developments = [c for _, stage, c in agents.calls if stage == "develop"]
        self.assertIn("outside the approved scope or of a protected path: .gitignore",
                      developments[1]["implementation_feedback"])


    def test_repeated_refusals_exhaust_the_local_repair_budget(self):
        with self.assertRaisesRegex(Blocked, "repair allowance exhausted"):
            self.run_to(EnvelopeAgents(outside=10), "review")

    def test_merge_gate_refuses_a_path_outside_the_envelope(self):
        state = self.run_to(EnvelopeAgents(), "gates").state
        files = state["changed_files"] + ["src/other.md"]
        reasons = review_reasons({**state, "changed_files": files}, self.gh.pr, files, self.gh.threads(9), self.config)
        self.assertIn("unapproved path", reasons)


class CapacityTests(Harness, unittest.TestCase):
    """Review capacity is measured on complete contexts, and dispatch still enforces the limit."""

    def test_a_reviewers_findings_can_push_its_context_over(self):
        controller = self.run_to(EnvelopeAgents(), "gates")
        before = {"docs/guide.md": "Original guide\n"}
        after = {"docs/guide.md": "Updated guide\n"}
        local = controller.context()
        sizes = [len(json.dumps(controller.pr_review_context(p, before, after, "0" * 40)).encode())
                 for p in self.config["reviewers"]] + [len(json.dumps(local).encode())]
        controller.config["max_context_bytes"] = max(sizes) + DISPATCH_ALLOWANCE + 100
        self.assertIsNone(controller.oversized(local, before, after), "the files fit")
        controller.state.setdefault("findings", []).append(
            {"id": "F9", "provider": "claude", "path": None, "start_line": None, "line": None, "detail": "d" * 2000,
             "status": "open", "text_digest": "x", "head": "h", "review": 1})
        message = controller.oversized(local, before, after)
        self.assertIn("claude PR review", message)
        self.assertNotIn("codex PR review", message)
        self.assertIn("design_issue", message)

    def test_measured_contexts_carry_the_changed_files_instructions(self):
        self.gh.trees[self.gh.base][".github/instructions/caller.instructions.md"] = (
            '---\napplyTo: "docs/caller.md"\n---\nReview callers.\n')
        controller = self.run_to(EnvelopeAgents(), "gates")
        controller.state["changed_files"] = []
        context = controller.pr_review_context("claude", {"docs/caller.md": None}, {"docs/caller.md": "x"}, "0" * 40)
        self.assertIn(".github/instructions/caller.instructions.md", context["policy"],
                      "before publication, the candidate's files select the instructions")

    def test_dispatch_names_the_stage(self):
        controller = self.run_to(EnvelopeAgents(), "review")
        controller.config["max_context_bytes"] = 100
        with self.assertRaisesRegex(Blocked, "review context exceeds the size limit"):
            controller.ask("claude", "review", controller.context())


if __name__ == "__main__":
    unittest.main()
