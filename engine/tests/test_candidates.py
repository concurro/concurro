"""Native candidate publication: cumulative budgets, deletion/rename and reviewed-head recovery."""

from copy import deepcopy
import unittest

from support import CONFIG, EXECUTION, setUpModule, tearDownModule
import candidates
from controller import Controller, changes
from policy import Blocked, OutsideScope, validate_edits
from test_pipeline import FakeAgents, FakeGitHub, PLAN


class CandidateTests(unittest.TestCase):
    """Controller byte checks replace workbench evidence without executing target code."""

    def setUp(self):
        self.plan = {**deepcopy(PLAN), "scope": {"paths": ["docs/**"], "max_files": 3}}

    def test_rename_validates_both_paths(self):
        before = {"docs/old.md": "one\ntwo\n", "docs/new.md": None}
        after = {"docs/old.md": None, "docs/new.md": "one\ntwo\n"}
        self.assertEqual(candidates.validate(before, after, self.plan, CONFIG, EXECUTION), sorted(before))
        proposal = {"summary": "rename", "edits": [{"path": p, "content": c} for p, c in after.items()]}
        self.assertEqual(len(validate_edits(proposal, self.plan, before, CONFIG, EXECUTION)), 2)
        proposal["edits"].append({"path": "AGENTS.md", "content": None})
        with self.assertRaises(OutsideScope):
            validate_edits(proposal, self.plan, before, CONFIG, EXECUTION)

    def test_follow_on_change_and_missing_deletion(self):
        # No mandatory primary-file edit: a valid follow-on change is still in scope.
        self.assertEqual(candidates.validate({}, {"docs/b.md": "x"}, self.plan, CONFIG, EXECUTION), ["docs/b.md"])
        with self.assertRaisesRegex(Blocked, "existing regular"):
            validate_edits({"summary": "x", "edits": [{"path": "docs/missing.md", "content": None}]},
                           self.plan, {}, CONFIG, EXECUTION)

    def test_newline_delimiters_are_source_lines(self):
        [item] = changes({"x": "a\u2028b\fc\n"}, {"x": "a\u2028d\fc\n"})
        self.assertEqual((item['added'], item['removed']), (1, 1))
        [item] = changes({"x": "x"}, {"x": "x\n"})
        self.assertEqual((item['added'], item['removed']), (1, 1))


class NativePublicationTests(unittest.TestCase):
    """Drive the real controller without a workspace factory or a local reviewer."""

    def test_delete_add_rename_reaches_reviews_and_ready_with_controller_objects(self):
        config = {**deepcopy(CONFIG), "autonomous": True}
        gh = FakeGitHub(config)
        class Rename(FakeAgents):
            def ask(self, provider, stage, context):
                if stage == "plan":
                    report = super().ask(provider, stage, context)
                    report["scope"] = {"paths": ["docs/**"], "max_files": 3}
                    return report
                if stage == "develop":
                    self.calls.append((provider, stage, deepcopy(context)))
                    return {"summary": "Rename the guide.", "validation": "Checked links.", "design_issue": None,
                            "edits": [{"path": "docs/guide.md", "content": None},
                                      {"path": "docs/new-guide.md", "content": "Moved guide\n"}]}
                return super().ask(provider, stage, context)
        original_pages = gh.pages
        def pages(path, *args):
            if path == "pulls/9/files":
                return [{"filename": "docs/new-guide.md", "previous_filename": "docs/guide.md", "status": "renamed"}]
            return original_pages(path, *args)
        gh.pages = pages
        agents = Rename()
        for _ in range(20):
            controller = Controller(gh, agents, config)
            controller.advance(7)
            if controller.state and controller.state.get("handoff_ready"):
                break
        state = controller.state
        self.assertTrue(state.get("handoff_ready"), state.get("reason"))
        self.assertEqual(state["changed_files"], ["docs/guide.md", "docs/new-guide.md"])
        self.assertNotIn("local_review", [stage for _, stage, _ in agents.calls])
        self.assertNotIn("local_evidence", state)
        self.assertEqual(state["candidate"]["validation"], "Checked links.")
        published = gh.trees[gh.commits[state["head"]]["tree"]["sha"]]
        self.assertNotIn("docs/guide.md", published)
        self.assertEqual(published["docs/new-guide.md"], "Moved guide\n")
        context = next(c for _, stage, c in agents.calls if stage == "review")
        self.assertEqual([c["status"] for c in context["changes"]], ["deleted", "added"])
        before = len(agents.calls)
        Controller(gh, agents, config).advance(7)
        self.assertEqual(len(agents.calls), before, "readiness polling starts no agent")

    def test_retained_development_result_survives_a_crash_without_spending_a_repair(self):
        config = {**deepcopy(CONFIG), "autonomous": True, "max_fix_rounds": 0}
        gh, agents = FakeGitHub(config), FakeAgents()
        original = gh.repo
        fail = [True]
        def api(path, method="GET", data=None):
            if path == "git/trees" and method == "POST" and fail[0]:
                fail[0] = False
                raise OSError("crash after the model completion checkpoint")
            return original(path, method, data)
        gh.repo = api
        crashed = False
        for _ in range(20):
            controller = Controller(gh, agents, config)
            try:
                controller.advance(7)
            except OSError:
                crashed = True
            if controller.state and controller.state.get("handoff_ready"):
                break
        self.assertTrue(crashed)
        self.assertTrue(controller.state.get("handoff_ready"), controller.state.get("reason"))
        self.assertEqual(sum(stage == "develop" for _, stage, _ in agents.calls), 1)

    def test_upstream_progress_does_not_rebase_or_replan_an_enrolled_task(self):
        """A task keeps its approved base even as the upstream default branch advances."""
        config = {**deepcopy(CONFIG), "autonomous": True}
        gh, agents = FakeGitHub(config), FakeAgents()
        for _ in range(15):
            controller = Controller(gh, agents, config)
            controller.advance(7)
            if controller.state["stage"] == "implement":
                break
        approved = controller.state["base"]
        gh.base = "b" * 40
        gh.trees[gh.base] = {**gh.trees[approved], "docs/unrelated.md": "Upstream only\n"}
        gh.commits[gh.base] = {"tree": {"sha": gh.base}}
        controller = Controller(gh, agents, config)
        controller.advance(7)
        self.assertEqual(controller.state["stage"], "publish")
        self.assertEqual(controller.state["base"], approved)
        self.assertEqual(controller.state["candidate"]["parent"], approved)
