"""Description repairs preserve code approvals and recover mistaken design reopens."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import commands
import test_bundle_lifecycle as lifecycle
from github import Transient
from policy import Blocked, approval_valid, digest
from test_pr_descriptions import COMMIT, DESCRIPTION, DescribedAgents


class DescriptionRepairTests(unittest.TestCase):
    """Exercise publication and recovery through real saved checkpoints."""

    setUp = lifecycle.BundleLifecycleTests.setUp
    controller = lifecycle.BundleLifecycleTests.controller
    loaded = lifecycle.BundleLifecycleTests.loaded
    tick = lifecycle.BundleLifecycleTests.tick
    reach = lifecycle.BundleLifecycleTests.reach

    def ready(self):
        """Reach a reviewed head with one description finding and another approval."""
        self.agents = DescribedAgents()
        c = self.reach("gates")
        c.state["stage"] = "implement"
        c.state["round"] = 1
        c.state["reviews"]["claude"]["verdict"] = "changes_requested"
        c.state["findings"] = [
            {
                "id": "F1",
                "provider": "claude",
                "status": "open",
                "head": c.state["head"],
                "path": None,
                "start_line": None,
                "line": None,
                "detail": "Name ensure_connection, not with_socket.",
                "thread": "body",
                "comment": None,
                "resolution": "none",
                "review": 1,
            }
        ]
        c.save()
        return c

    def report(self, edits=None):
        """Return corrected presentation with no source changes."""
        return {
            "summary": "Correct the function name.",
            "validation": "Description checked; code unchanged.",
            "edits": [] if edits is None else edits,
            "design_issue": None,
            "description": {**deepcopy(DESCRIPTION), "review_focus": ["Check `ensure_connection`/`rpc_request`."]},
            "commit": deepcopy(COMMIT),
        }

    def implement(self, c):
        """Use a deterministic report while exercising controller validation and persistence."""
        with patch.object(c, "ask", return_value=self.report()):
            c.implement()

    def reopen(self):
        """Reproduce #35: a metadata-only repair reopens an exhausted design."""
        c = self.ready()
        c.state["plan_revisions"] = c.config["max_plan_revisions"]
        c.save()
        before = deepcopy(c.state)
        c.state["calls"] += 2
        c.state["credentials"] = {"fixture": {"calls": 2}}
        c.reopen_design(
            {
                "kind": "plan",
                "explanation": "Correct the description function name.",
                "evidence": ["The code and plan are unchanged."],
            },
            "implementation",
        )
        return c, before

    def test_description_only_keeps_head_approval_findings_and_other_review(self):
        c = self.ready()
        before = deepcopy(c.state)
        self.implement(c)
        c = self.loaded()
        writes = []
        repo = self.gh.repo

        def record(path, method="GET", data=None):
            """Record mutations while leaving the fake transport intact."""
            if method != "GET":
                writes.append((path, method))
            return repo(path, method, data)

        with patch.object(self.gh, "repo", side_effect=record):
            c.publish()
        for key in ("head", "approval", "design_votes", "plan", "plan_id", "plan_revisions", "calls", "findings"):
            self.assertEqual(c.state[key], before[key], key)
        self.assertTrue(approval_valid(c.state, c.config))
        self.assertEqual(c.state["reviews"], {"codex": before["reviews"]["codex"]})
        self.assertEqual(c.state["stage"], "review")
        self.assertIn("`ensure_connection`/`rpc_request`", self.gh.pr["body"])
        self.assertFalse(any(p.startswith("git/") for p, _ in writes), writes)
        c.review()
        self.assertEqual(c.state["stage"], "gates")
        self.assertEqual(c.state["findings"][0]["status"], "fixed")
        self.assertEqual(self.agents.calls[-1][:2], ("claude", "review"))
        self.assertEqual(c.state["reviews"]["codex"], before["reviews"]["codex"])

    def test_lost_body_reply_replays_without_new_commit_or_model_call(self):
        c = self.ready()
        self.implement(c)
        head, calls = c.state["head"], len(self.agents.calls)
        repo = self.gh.repo
        patches = []

        def lose(path, method="GET", data=None):
            """Commit one body update, then lose the transport reply."""
            result = repo(path, method, data)
            if path == "pulls/9" and method == "PATCH" and "body" in data:
                patches.append(data)
                raise Transient("response_lost")
            return result

        with patch.object(self.gh, "repo", side_effect=lose), self.assertRaises(Transient):
            c.publish()
        c = self.loaded()
        c.publish()
        self.assertEqual(len(patches), 1)
        self.assertEqual(c.state["head"], head)
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(c.state["findings"][0]["status"], "open")
        self.assertNotIn("description_update", c.state)

    def test_description_update_refuses_changed_or_closed_head(self):
        for change in ({"state": "closed"}, {"head": {"sha": "f" * 40}}):
            with self.subTest(change=change):
                self.setUp()
                c = self.ready()
                self.implement(c)
                body = self.gh.pr["body"]
                self.gh.pr.update(change)
                with self.assertRaises(Blocked):
                    c.publish()
                self.assertEqual(self.gh.pr["body"], body)

    def test_human_body_is_not_overwritten_and_finding_stays_open(self):
        c = self.ready()
        self.implement(c)
        self.gh.pr["body"] = "Maintainer's description"
        c.publish()
        self.assertEqual(self.gh.pr["body"], "Maintainer's description")
        self.assertTrue(c.state["pr_description"]["human_edited"])
        self.assertEqual(c.state["findings"][0]["status"], "open")
        self.assertEqual(c.state["stage"], "review")

    def test_empty_initial_implementation_still_requires_code(self):
        self.agents = DescribedAgents()
        c = self.reach("implement")
        with patch.object(c, "ask", return_value=self.report()), self.assertRaises(Blocked):
            c.implement()
        self.assertNotIn("description_update", c.state)

    def test_recovery_restores_evidence_not_budgets_and_replays_once(self):
        c, before = self.reopen()
        spent = {
            k: deepcopy(c.state.get(k)) for k in ("calls", "credentials", "plan_revisions", "round", "design_reopens")
        }
        self.gh.comment("/concurro resume --description-only")
        c.commands(self.gh.comments)
        self.assertEqual(c.state["stage"], "implement")
        self.assertTrue(approval_valid(c.state, c.config))
        for key in ("approval", "design_votes", "design_generation", "plan", "head", "reviews", "findings"):
            self.assertEqual(c.state[key], before[key], key)
        self.assertEqual({k: c.state.get(k) for k in spent}, spent)
        self.assertNotIn("revision_wait", c.state)
        self.assertTrue(c.state["reopen_history"])
        recovered = deepcopy(c.state)
        c.commands(self.gh.comments)
        self.assertEqual(c.state, recovered)
        c = self.loaded()
        self.assertTrue(approval_valid(c.state, c.config))
        self.implement(c)
        c.publish()
        c.review()
        self.assertEqual(c.state["stage"], "gates")

    def test_recovery_lost_save_preserves_command_and_spending(self):
        c, before = self.reopen()
        self.gh.comment("/concurro resume --description-only")
        save = c.save

        def lost(*args, **kwargs):
            """Lose the reply after committing recovery and its writer receipt."""
            save(*args, **kwargs)
            raise Transient("response_lost")

        with patch.object(c, "save", side_effect=lost), self.assertRaises(Transient):
            c.commands(self.gh.comments)
        c = self.loaded()
        calls = c.state["calls"]
        c.commands(self.gh.comments)
        self.assertEqual(c.state["stage"], "implement")
        self.assertEqual(c.state["calls"], calls)
        self.assertEqual(c.state["approval"], before["approval"])

    def test_recovery_does_not_restore_changed_design_or_head(self):
        for change in ("plan", "head", "requirements", "missing"):
            with self.subTest(change=change):
                self.setUp()
                c, _ = self.reopen()
                if change == "plan":
                    c.state["plan"]["steps"].append("Different design")
                elif change == "head":
                    self.gh.pr["head"]["sha"] = "f" * 40
                elif change == "requirements":
                    self.gh.issue["body"] += "New requirements"
                    c.issue = self.gh.issue
                else:
                    record = self.gh.artifacts[7][c.checkpoints.audit["number"]]
                    record["versions"] = record["versions"][:1]
                before = deepcopy(c.state)
                self.gh.comment("/concurro resume --description-only")
                c.commands(self.gh.comments)
                for key in ("stage", "approval", "findings", "calls", "plan_revisions"):
                    self.assertEqual(c.state.get(key), before.get(key), key)
                self.assertTrue(any("Resume not applied" in x["body"] for x in self.gh.comments))

    def test_command_parse_is_exact(self):
        self.assertEqual(commands.parse("/concurro resume --description-only").argument, "--description-only")
        self.assertIsNone(commands.parse("/concurro resume --description-only extra"))

    def test_code_repair_still_publishes_commit_and_rechecks_every_reviewer(self):
        c = self.ready()
        head = c.state["head"]
        report = self.report([{"path": "docs/guide.md", "content": "A real implementation repair.\n"}])
        with patch.object(c, "ask", return_value=report):
            c.implement()
        self.assertNotIn("description_update", c.state)
        c.publish()
        self.assertNotEqual(c.state["head"], head)
        self.assertEqual(c.state["reviews"], {})
        self.assertIn("`ensure_connection`/`rpc_request`", self.gh.pr["body"])

    def test_reviewer_can_keep_description_finding_open(self):
        c = self.ready()
        self.implement(c)
        c.publish()
        report = {
            "verdict": "changes_requested",
            "summary": "The wording remains wrong.",
            "findings": [],
            "dispositions": [{"id": "F1", "status": "open", "evidence": "The corrected body is still inaccurate."}],
            "design_issue": None,
        }
        with patch.object(c, "ask", return_value=report):
            c.review()
        self.assertEqual(c.state["findings"][0]["status"], "open")
        self.assertNotEqual(c.state["stage"], "gates")

    def test_recovery_preserves_old_bundle_approval_when_adopting_new_instructions(self):
        c, before = self.reopen()
        path = self.root / "bundle/skills/concurro-implement/SKILL.md"
        path.write_text(path.read_text() + "\nSupport description-only repairs.\n")
        c = self.loaded()
        self.gh.comment("/concurro resume --description-only")
        c.commands(self.gh.comments)
        self.assertEqual(c.state["approval"], before["approval"])
        self.assertTrue(approval_valid(c.state, c.config))
        self.assertEqual(c.state["bundle_resume"]["new_bundle"], c.bundle.identity)
        self.assertNotEqual(c.state["bundle_id"], c.bundle.identity)

    def test_recovery_does_not_discard_new_findings_or_owner_dispositions(self):
        for change in ("finding", "review"):
            with self.subTest(change=change):
                self.setUp()
                c, _ = self.reopen()
                if change == "finding":
                    c.state["findings"][0]["status"] = "fixed"
                else:
                    c.state["reviews"]["claude"]["summary"] = "New review evidence"
                before = deepcopy(c.state)
                self.gh.comment("/concurro resume --description-only")
                c.commands(self.gh.comments)
                for key in ("findings", "reviews", "calls", "stage"):
                    self.assertEqual(c.state[key], before[key])
                self.assertNotIn("approval", c.state)

    def test_recovery_removes_only_unsent_withdrawal_for_recovered_generation(self):
        c, _ = self.reopen()
        stale = {"kind": "withdrawn", "generation": c.state["design_generation"], "findings": ["F1"]}
        other = {"kind": "withdrawn", "generation": 100, "findings": ["F99"]}
        c.state["pr_outbox"] = [stale, other]
        self.gh.comment("/concurro resume --description-only")
        with patch.object(c, "save"):
            c.commands(self.gh.comments)
        self.assertEqual(c.state["pr_outbox"], [other])
        self.assertEqual(c.state["findings"][0]["status"], "open")

    def test_recovery_transient_history_read_never_consumes_command(self):
        c, _ = self.reopen()
        before = deepcopy(c.state)
        self.gh.comment("/concurro resume --description-only")
        with patch.object(c.checkpoints, "history", side_effect=Transient("unavailable")), self.assertRaises(Transient):
            c.commands(self.gh.comments)
        self.assertEqual(c.state, before)

    def test_recovery_requires_writer_and_unchanged_checkpoint_attribution(self):
        c, _ = self.reopen()
        self.gh.comment("/concurro resume --description-only", login="outsider")
        before = deepcopy(c.state)
        c.commands(self.gh.comments)
        self.assertEqual(c.state, before)
        record = self.gh.artifacts[7][c.checkpoints.audit["number"]]
        for version in record["versions"]:
            if version["version"] < c.checkpoints.audit["revision"]:
                version["actor"]["login"] = "outsider"
        self.gh.comment("/concurro resume --description-only")
        c.commands(self.gh.comments)
        self.assertEqual(c.state["stage"], "escalated")
        self.assertNotIn("approval", c.state)

    def test_real_call_retention_charges_only_repair_and_owning_reviewer(self):
        c = self.ready()
        before = deepcopy(c.state)
        ask = self.agents.ask

        def respond(provider, stage, context, *args, **kwargs):
            """Supply one description correction using the normal paid-call path."""
            if stage == "develop":
                self.agents.calls.append((provider, stage, deepcopy(context)))
                return self.report()
            return ask(provider, stage, context, *args, **kwargs)

        with patch.object(self.agents, "ask", side_effect=respond):
            c.implement()
        c = self.loaded()
        c.publish()
        c.review()
        self.assertEqual(c.state["stage"], "gates")
        self.assertEqual(c.state["calls"], before["calls"] + 2)
        self.assertEqual(c.state["plan_revisions"], before["plan_revisions"])
        self.assertEqual(c.state["approval"], before["approval"])
        self.assertEqual(c.state["head"], before["head"])
        self.assertEqual(c.state["candidate"]["validation"], before["candidate"]["validation"])

    def test_recovery_retires_obsolete_feedback_but_keeps_attempt_counts(self):
        c, _ = self.reopen()
        c.state["implementation_feedback"] = {"0:1": "implementation must contain edits"}
        c.state["implementation_attempts"] = {"0:1": 1}
        self.gh.comment("/concurro resume --description-only")
        c.commands(self.gh.comments)
        self.assertNotIn("implementation_feedback", c.state)
        self.assertEqual(c.state["implementation_attempts"], {"0:1": 1})
        self.assertTrue(any(x.get("field") == "implementation_feedback" for x in c.state["resume_history"]))

    def test_recovered_generation_is_not_reused_for_later_withdrawals(self):
        """Posted pending reviews and legacy replies keep unique identities after recovery."""
        import dashboard_runs
        import reviews

        for publication, format_number in (("pending-v1", 5), ("rest", 1)):
            with self.subTest(publication=publication):
                self.setUp()
                c = self.ready()
                finding = c.state["findings"][0]
                finding.update(path="docs/guide.md", comment=1001, format=1, text_digest=digest(finding["detail"]))
                opening = {
                    "id": 1001,
                    "path": finding["path"],
                    "body": reviews.finding_body(finding),
                    "user": {"login": self.config["bot_login"]},
                }
                self.gh.review_comments.append(opening)
                self.gh.thread(opening)
                finding["thread"] = self.gh.thread_list[-1]["id"]
                c.state["plan_revisions"] = c.config["max_plan_revisions"]
                c.save()
                approved = c.state["design_generation"]
                issue = {"kind": "plan", "explanation": "Description wording.", "evidence": ["Unchanged code."]}
                with patch.object(reviews, "PUBLICATION", publication), patch.object(reviews, "FORMAT", format_number):
                    c.reopen_design(issue, "implementation")
                    burned = c.state["design_generation"]
                    self.assertTrue(any(f"F1:{burned} -->" in x["body"] for x in self.gh.review_comments))
                    # Already-deployed reopens have no high-water field yet.
                    c.state.pop("design_generation_high", None)
                    c.save()
                    self.gh.comment("/concurro resume --description-only")
                    c.commands(self.gh.comments)
                    c = self.loaded()
                    self.assertEqual(c.state["design_generation"], approved)
                    self.assertTrue(approval_valid(c.state, c.config))
                    next_boundary = dashboard_runs.start(c.state)["generation"]
                    c.reopen_design({**issue, "explanation": "A genuine architecture change."}, "implementation")
                    next_generation = c.state["design_generation"]
                    self.assertGreater(next_generation, burned)
                    self.assertEqual(next_boundary, next_generation)
                    self.assertTrue(any(f"F1:{next_generation} -->" in x["body"] for x in self.gh.review_comments))
                    self.assertFalse(c.state.get("pr_outbox"))
                    self.assertNotEqual(
                        dashboard_runs.display_round("Design review", burned + 1, dashboard_runs.EMPTY),
                        dashboard_runs.display_round("Design review", next_generation + 1, dashboard_runs.EMPTY),
                    )
                    # Subsequent history records the new generation, never the abandoned identity.
                    c.invalidate_design("Archive the genuine generation")
                    self.assertEqual(c.state["design_history"][-1]["generation"], next_generation)
                    self.assertNotEqual(c.state["design_history"][-1]["generation"], burned)

    def test_repeated_recovery_keeps_the_highest_generation(self):
        """A skipped generation does not prevent a second evidence-preserving recovery."""
        c, before = self.reopen()
        for _ in range(2):
            burned = c.state["design_generation"]
            self.gh.comment("/concurro resume --description-only")
            c.commands(self.gh.comments)
            self.assertEqual(c.state["design_generation"], before["design_generation"])
            self.assertEqual(c.state["design_generation_high"], burned)
            c.invalidate_design("Another mistaken reopen")
            self.assertGreater(c.state["design_generation"], burned)
            c.state.setdefault("reopen_history", []).append(
                {
                    "source": "implementation",
                    "plan": c.state["plan_id"],
                    "head": c.state["head"],
                }
            )
            c.state.update(stage="escalated", retry_stage="plan")
            c.save()

    def test_resume_team_change_advances_past_burned_generations(self):
        """The other generation allocator shares the high-water rule."""
        c, _ = self.reopen()
        c.state["design_generation_high"] = c.state["design_generation"] + 5
        high = c.state["design_generation_high"]
        new = {**self.config, "qc_model": "different-model"}
        c.resume_target = lambda: (new, c.bundle)
        self.gh.comment("/concurro resume")
        c.commands(self.gh.comments)
        self.assertGreater(c.state["design_generation"], high)
        self.assertEqual(c.state["design_generation_high"], c.state["design_generation"])

    def test_large_description_gets_repair_feedback_before_publication(self):
        """Size admission includes the full existing diff and preserved validation."""
        c = self.ready()
        short, large = self.report(), self.report()
        large["description"]["changes"] = ["Long description. " * 2200]
        c.config["max_context_bytes"] = 20000
        original = deepcopy(c.state["candidate"])
        with (
            patch.object(c, "ask", side_effect=[large, short]) as ask,
            patch.object(c, "oversized", wraps=c.oversized) as size,
        ):
            c.implement()
        self.assertEqual(ask.call_count, 2)
        self.assertIn("too large to review", ask.call_args_list[1].args[2]["implementation_feedback"])
        self.assertEqual(size.call_count, 2)
        for call in size.call_args_list:
            self.assertEqual(set(call.args[1]), set(c.state["changed_files"]))
            self.assertEqual(set(call.args[2]), set(c.state["changed_files"]))
            self.assertEqual(call.kwargs["description"]["validation"], original["validation"])
        self.assertEqual(c.state["candidate"]["description"], short["description"])
        self.assertEqual(c.state["head"], original["head"])
