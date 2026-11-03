"""Scope envelope: a plan approves a design, an area and a budget; the implementer edits within them."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import CONFIG, EXECUTION, setUpModule, tearDownModule  # unittest module fixtures
from test_findings import Harness, Reviewers
from test_pipeline import APPROVE, PLAN, FakeWorkspace
import checks
from controller import DISPATCH_ALLOWANCE, Controller
from local import LocalWorkspace
from policy import Blocked, OutsideScope, local_evidence_valid, merge_reasons, validate_edits, validate_plan
from repository import Repository, extract, overlay
from snapshot import inspection
from test_repository import archive

SCOPE = {"paths": ["docs/**"], "max_files": 3, "max_changed_lines": 50}


class EnvelopeAgents(Reviewers):
    """Plans with an envelope; the implementer first reaches outside it `outside` times, then edits a call site.

    A repair turn after that edit changes only the primary file.
    """

    def __init__(self, outside=1):
        super().__init__(set())
        self.outside = outside

    def ask(self, provider, stage, context):
        if stage == "plan":
            self.calls.append((provider, stage, deepcopy(context)))
            return {**deepcopy(PLAN), "scope": deepcopy(SCOPE)}
        if stage == "develop":
            self.calls.append((provider, stage, deepcopy(context)))
            if self.outside:
                self.outside -= 1
                return {"summary": "Too wide.", "edits": [{"path": "guides/elsewhere.md", "content": "x\n"},
                                                          {"path": "docs/guide.md", "content": "Updated\n"}],
                        "checks": [], "design_issue": None}
            if context["files"].get("docs/guide.md") == "Updated guide\n":
                return {"summary": "Repair the guide.", "checks": [], "design_issue": None,
                        "edits": [{"path": "docs/guide.md", "content": "Repaired guide\n"}]}
            return {"summary": "Explain the guide and update its caller.", "checks": [], "design_issue": None,
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

    def test_envelope_approves_the_profiles_of_its_base_files(self):
        plan = {**deepcopy(PLAN), "scope": {**SCOPE, "paths": ["docs/**", "src/**"]}}
        self.assertEqual(checks.approved_profiles(plan, {"docs/guide.md": {}, "src/lib.rs": {}}, EXECUTION), ["rust"])
        self.assertEqual(checks.approved_profiles(deepcopy(PLAN), {"src/lib.rs": {}}, EXECUTION), [],
                         "without an envelope, only the plan's files count")
        self.assertEqual(checks.contract_errors(plan, EXECUTION, ["rust"]), ["validation_checks"],
                         "an approved profile's mandatory checks must be selected")

    def test_out_of_envelope_proposals_are_refused_whole(self):
        plan = {**deepcopy(PLAN), "scope": deepcopy(SCOPE)}
        edits = [{"path": "docs/guide.md", "content": "a"}, {"path": "guides/x.md", "content": "b"}]
        with self.assertRaises(OutsideScope) as raised:
            validate_edits({"summary": "s", "edits": edits}, plan, {}, CONFIG, EXECUTION)
        self.assertEqual(raised.exception.paths, ["guides/x.md"])
        validate_edits({"summary": "s", "edits": [edits[0], {"path": "docs/new.md", "content": "c"}]},
                       plan, {}, CONFIG, EXECUTION)

    def test_local_evidence_requires_the_approved_profiles_checks(self):
        def state(names):
            checks = [{"name": n, "exit_code": 0, "tree": "t"} for n in names]
            return {"plan": deepcopy(PLAN), "execution": EXECUTION, "approved_profiles": ["rust"], "head": "h",
                    "execution_image": "i", "local_evidence": {"head": "h", "tree": "t", "local_commit": "c",
                                                               "image": "i", "checks": checks, "review": APPROVE}}
        planned = PLAN["validation"]["checks"]
        self.assertTrue(local_evidence_valid(state(list(dict.fromkeys(planned + checks.mandatory(["rust"], EXECUTION))))))
        self.assertFalse(local_evidence_valid(state(planned)), "the plan's files trigger no profile, the envelope does")


class ImplementationTests(Harness, unittest.TestCase):
    """The implementer edits inside the envelope; reaching outside it is feedback, not a block."""

    def test_call_site_inside_the_envelope_reaches_review_and_gates(self):
        agents = EnvelopeAgents()
        state = self.run_to(agents, "gates").state
        developments = [c for _, stage, c in agents.calls if stage == "develop"]
        self.assertIn("outside the approved scope: guides/elsewhere.md", developments[1]["local_feedback"]["scope"])
        self.assertEqual(sorted(state["changed_files"]), ["docs/caller.md", "docs/guide.md"])
        reviews = [c for _, stage, c in agents.calls if stage == "review"]
        self.assertEqual(sorted(reviews[0]["after"]), ["docs/caller.md", "docs/guide.md"], "reviews see every changed file")
        self.assertNotIn("unapproved path", self.reasons(state))
        self.assertNotIn("guides/elsewhere.md", json.dumps(self.gh.trees))

    def test_repair_turns_inspect_current_non_primary_edits_and_their_instructions(self):
        self.gh.trees[self.gh.base][".github/instructions/caller.instructions.md"] = (
            '---\napplyTo: "docs/caller.md"\n---\nReview callers.\n')
        agents = EnvelopeAgents(outside=0)
        sizes = iter(["The change is too large to review."])
        with patch.object(Controller, "oversized", lambda *_: next(sizes, None)):
            self.run_to(agents, "gates")
        first, repair = [c for _, stage, c in agents.calls if stage == "develop"][:2]
        rule = ".github/instructions/caller.instructions.md"
        self.assertNotIn(rule, first["policy"], "nothing has touched the call site yet")
        self.assertIn(rule, repair["policy"])
        self.assertEqual(repair["files"]["docs/caller.md"], "See the updated guide.\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extract(io.BytesIO(archive({"repo/docs/guide.md": b"Original guide\n"})), root)
            overlay(inspection(repair, "develop")[1], root)
            self.assertEqual(Repository(root).read("docs/caller.md"), "See the updated guide.\n")
        reviews = [c for _, stage, c in agents.calls if stage in ("local_review", "review")]
        self.assertTrue(reviews and all(rule in c["policy"] for c in reviews), "reviews apply the call site's rules")

    def test_repeated_refusals_exhaust_the_local_repair_budget(self):
        with self.assertRaisesRegex(Blocked, "local repair budget exhausted"):
            self.run_to(EnvelopeAgents(outside=10), "review")

    def test_merge_gate_refuses_a_path_outside_the_envelope(self):
        state = self.run_to(EnvelopeAgents(), "gates").state
        files = state["changed_files"] + ["src/other.md"]
        reasons = merge_reasons({**state, "changed_files": files}, self.gh.pr, files,
                                self.gh.pages("commits/x/check-runs"), self.gh.threads(9), [], self.config)
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


class WorkbenchTests(unittest.TestCase):
    """The approved base is always staged, docs-only work included."""

    def test_validation_stages_the_approved_base(self):
        with patch.dict(os.environ, {"CONCURRO_EXECUTION_IMAGE": "sha256:" + "a" * 64}):
            workspace = LocalWorkspace(CONFIG, b"", "concurro/test", EXECUTION, base_archive=b"base")
        with patch.object(workspace, "control", return_value={}) as control:
            workspace.configure_validation({"checks": [], "files": []}, {}, {"paths": ["docs/**"]})
            workspace.configure_validation({"checks": [], "files": []}, {}, {"paths": ["docs/**"]})
        self.assertEqual([c.args[0] for c in control.call_args_list], ["stage", "validation", "validation"])
        self.assertEqual(control.call_args_list[0].kwargs["data"], b"base")


if __name__ == "__main__":
    unittest.main()
