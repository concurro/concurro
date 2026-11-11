"""Pinned writer-command contracts before extracting the handlers and revision budget."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import conversation
from design import ReviewError
from design_batch import Design
from fakes import decision
from pipeline_fixtures import PipelineFixture
from policy import Blocked, design_roster

GOLDEN = Path(__file__).parent / "goldens/commands.json"
COMMANDS = (
    "start",
    "approve_match",
    "approve_other",
    "stop",
    "retry",
    "extend_0",
    "extend_1",
    "extend_4",
    "invalidate",
)
RECORDS = (
    "plan",
    "plan_id",
    "base",
    "branch",
    "qc",
    "approval",
    "sessions",
    "objections",
    "design_votes",
    "design_feedback",
    "candidate",
    "approved_accumulated_files",
    "design_parent",
    "operation",
    "intake_draft",
    "plan_draft",
    "review_repair",
    "review_batch",
    "requirements",
    "issue_digest",
)


class CommandCharacterizationTests(PipelineFixture, unittest.TestCase):
    """Real lifecycle snapshots, real command handling and saves, only fake external services."""

    @classmethod
    def setUpClass(cls):
        """Build each starting point once; every matrix cell restores an independent copy."""
        builder = cls()
        cls.scenarios = builder.build_scenarios()

    def setUp(self):
        """Each test restores one of the already-driven lifecycle snapshots explicitly."""

    def snapshot(self):
        """Copy the shared fake graph together so configuration and GitHub references agree."""
        return deepcopy((self.config, self.gh, self.agents))

    def restore(self, name):
        """Restore durable state without advancing it or reusing a permission cache."""
        self.config, self.gh, self.agents = deepcopy(self.scenarios[name])
        self.gh.counter = 1000
        return self.loaded()

    def build_scenarios(self):
        """Drive healthy stages, decisions, corrections and revision exhaustion through real methods."""
        scenarios = {}
        for mode in ("autonomous", "manual"):
            PipelineFixture.setUp(self)
            self.config["autonomous"] = mode == "autonomous"
            self.gh.comments = []
            prefix = mode + "/"
            scenarios[prefix + "uninitialized"] = self.snapshot()
            c = self.loaded()
            c.initialize([], mode, enrollment=c.enrollment() if mode == "autonomous" else None)
            scenarios[prefix + "intake"] = self.snapshot()
            stages = ["plan", "plan_qc", "implement", "publish", "review", "gates"]
            if mode == "manual":
                stages.insert(2, "planned")
            for stage in stages:
                c = self.loaded()
                if c.state["stage"] != stage:
                    c = self.reach(stage)
                scenarios[prefix + stage] = self.snapshot()
                if stage == "planned":
                    self.gh.comment("/concurro approve " + c.state["plan_id"])
                    c.commands(self.gh.comments)
            for name, verdict in (("clarify", "clarify"), ("rejected", "reject"), ("escalated", "escalate")):
                self.config, self.gh, self.agents = deepcopy(scenarios[prefix + "intake"])
                self.agents.qc_reports = [
                    decision(verdict, questions=["Which behavior is intended?"] if verdict == "clarify" else [])
                ]
                self.reach(name)
                scenarios[prefix + name] = self.snapshot()
            for name, source, stage in (
                ("blocked_implement", "implement", "blocked"),
                ("blocked_plan_qc", "plan_qc", "blocked"),
                ("blocked_plan", "plan", "blocked"),
                ("stopped", "plan", "stopped"),
            ):
                self.config, self.gh, self.agents = deepcopy(scenarios[prefix + source])
                self.loaded().suspend(stage, "Fixture interruption.")
                scenarios[prefix + name] = self.snapshot()
            self.config, self.gh, self.agents = deepcopy(scenarios[prefix + "gates"])
            c = self.loaded()
            c.finish_quality_control("plan_qc", decision("clarify", questions=["Which behavior is intended?"]))
            c.save()
            scenarios[prefix + "clarify_with_pr"] = self.snapshot()
            self.config, self.gh, self.agents = deepcopy(scenarios[prefix + "review"])
            self.loaded().suspend("blocked", "Fixture interruption.")
            scenarios[prefix + "blocked_with_pr"] = self.snapshot()
            self.config, self.gh, self.agents = deepcopy(scenarios[prefix + "plan_qc"])
            c = self.loaded()
            c.state["plan_revisions"] = self.config["max_plan_revisions"]
            c.save()
            self.agents.qc_reports = [
                decision("approve"),
                decision("revise", concerns=["Rollback must discard aborted writes."]),
            ]
            self.reach("escalated")
            scenarios[prefix + "escalated_waiting"] = self.snapshot()
        # Corrections cover all four output_repair clauses, including judged-stage retries.
        for kind in ("intake", "plan", "review", "batch"):
            source = kind if kind in ("intake", "plan") else "plan_qc"
            self.config, self.gh, self.agents = deepcopy(scenarios["autonomous/" + source])
            c = self.loaded()
            member = design_roster(self.config)[0]
            if kind == "batch":
                adapter = Design(c)
                adapter.select(adapter.open()[0])
            correcting = None
            for attempt in range(3 if kind in ("intake", "plan") else 2):
                if kind == "intake":
                    c.repair_intake({"invalid": True}, "Fixture output is incomplete.", "codex")
                elif kind == "plan":
                    c.repair_plan({"invalid": True}, "Fixture output is incomplete.")
                else:
                    c.repair_review({"invalid": True}, ReviewError("review_shape"), member)
                if attempt == 0:
                    correcting = self.snapshot()
            self.assertEqual(c.state["stage"], "escalated")
            scenarios["autonomous/escalated_" + kind + "_output"] = self.snapshot()
            self.config, self.gh, self.agents = deepcopy(correcting)
            self.loaded().suspend("blocked", "Fixture interruption.")
            scenarios["autonomous/blocked_" + kind + "_output"] = self.snapshot()
        return scenarios

    def body(self, command, controller):
        """Expand readable matrix command names without bypassing the actual parser."""
        if command.startswith("approve_"):
            plan = (controller.state or {}).get("plan_id", "a" * 64)
            if command == "approve_other":
                plan = "0" * 64 if plan != "0" * 64 else "1" * 64
            return "/concurro approve " + plan
        if command.startswith("extend_"):
            return "/concurro retry --plan-revisions " + command.removeprefix("extend_")
        return "/concurro " + command

    def apply(self, controller, commands, actor="writer"):
        """Observe commands, preserving real saves, exceptions, notices and permission checks."""
        before = deepcopy(controller.state or {})
        for command in commands:
            self.gh.comment(self.body(command, controller), login=actor)
        comments = list(self.gh.comments)
        old_ids = {c["id"] for c in comments}
        calls = len(self.agents.calls)
        result, error = None, None
        original = controller.save
        saves = []

        def save(*args, **kwargs):
            """Record the command and stage at each persistence boundary before doing the save."""
            saves.append(
                [
                    controller.state["stage"],
                    controller.state.get("command_id"),
                    controller.state.get("retry_stage"),
                    deepcopy(controller.state.get("revision_wait")),
                ]
            )
            return original(*args, **kwargs)

        with patch.object(controller, "save", side_effect=save):
            try:
                result = controller.commands(comments)
            except Blocked as refusal:
                error = str(refusal)
        state = controller.state or {}
        self.assertEqual(len(self.agents.calls), calls, "commands alone must not contact models")
        changed = {}
        for key in RECORDS:
            if (key in before) != (key in state) or before.get(key) != state.get(key):
                changed[key] = "removed" if key not in state else "added" if key not in before else "changed"
        # Strip only the hidden event identity; keep the complete visible notice verbatim.
        notices = [
            c["body"].split("\n", 1)[1]
            for c in self.gh.comments
            if c["id"] not in old_ids and c["body"].startswith(conversation.MARKER)
        ]
        return {
            "stage": state.get("stage"),
            "command_id": state.get("command_id"),
            "retry_stage": state.get("retry_stage"),
            "revision_wait": state.get("revision_wait"),
            "saves": saves,
            "return": result,
            "blocked": error,
            "notices": notices,
            "revisions": [state.get("plan_revisions", 0), state.get("plan_revision_extra", 0)],
            "grant": state.get("plan_revision_grant"),
            "calls_delta": state.get("calls", 0) - before.get("calls", 0),
            "generation_delta": state.get("design_generation", 0) - before.get("design_generation", 0),
            "changed": changed,
        }

    def matrix(self):
        """Return every scenario × command × authority observation for explicit fixture capture."""
        results = {}
        for scenario in self.scenarios:
            for actor in ("writer", "outsider"):
                for command in COMMANDS:
                    c = self.restore(scenario)
                    results[f"{scenario}/{actor}/{command}"] = self.apply(c, [command], actor)
        return results

    def test_command_stage_matrix(self):
        """Pin every reviewed command outcome without automatic fixture regeneration."""
        expected = json.loads(GOLDEN.read_text())
        actual = self.matrix()
        self.assertEqual(actual.keys(), expected.keys())
        for case, value in actual.items():
            with self.subTest(case=case):
                self.assertEqual(value, expected[case])

    def test_nonwriters_cannot_mutate_any_scenario(self):
        """An independent invariant prevents a regenerated fixture from legitimizing lost authority checks."""
        for scenario in self.scenarios:
            for command in COMMANDS:
                with self.subTest(scenario=scenario, command=command):
                    c = self.restore(scenario)
                    before = deepcopy(c.state)
                    result = self.apply(c, [command], "outsider")
                    self.assertEqual(c.state, before)
                    for key in ("saves", "notices", "changed"):
                        self.assertFalse(result[key])
                    self.assertIsNone(result["return"])
                    self.assertIsNone(result["blocked"])

    def test_retry_then_stop_runs_both_commands_in_order(self):
        """A normal retry falls through, letting a later stop pause the resumed stage."""
        c = self.restore("autonomous/blocked_implement")
        result = self.apply(c, ["retry", "stop"])
        self.assertEqual(result["saves"], [["implement", 1001, None, None], ["stopped", 1002, "implement", None]])
        self.assertEqual((result["stage"], result["command_id"]), ("stopped", 1002))
        self.assertIsNone(result["return"])
        self.assertIsNone(result["blocked"])
        self.assertEqual(self.apply(self.loaded(), [])["saves"], [])

    def test_invalidate_returns_before_later_retry(self):
        """Invalidation stops this invocation; replay skips it and ignores retry during intake."""
        c = self.restore("autonomous/blocked_implement")
        before = c.state["design_generation"]
        result = self.apply(c, ["invalidate", "retry"])
        self.assertTrue(result["return"])
        self.assertEqual(result["saves"], [["intake", 1001, None, None]])
        restored = self.loaded()
        replay = self.apply(restored, [])
        self.assertEqual(replay["saves"], [])
        self.assertEqual(restored.state["command_id"], 1001)
        self.assertEqual(restored.state["design_generation"], before + 1)
        self.assertEqual(len(restored.state["invalidations"]), 1)

    def test_two_queued_grants_do_not_apply_twice(self):
        """The first grant returns immediately; the later grant becomes inapplicable next tick."""
        c = self.restore("autonomous/escalated_waiting")
        before = deepcopy(c.state)
        first = self.apply(c, ["extend_1", "extend_1"])
        self.assertTrue(first["return"])
        self.assertEqual(first["saves"], [["plan", 1001, None, None]])
        restored = self.loaded()
        second = self.apply(restored, [])
        self.assertIsNone(second["return"])
        self.assertEqual(second["saves"], [["plan", 1002, None, None]])
        self.assertIn("No exhausted plan revision is waiting", second["notices"][0])
        self.assertEqual(restored.state["plan_revision_extra"], 1)
        self.assertEqual(restored.state["plan_revisions"], before["plan_revisions"] + 1)
        self.assertEqual(restored.state["objections"], before["objections"])
        self.assertEqual(restored.state["calls"], before["calls"])
        self.assertEqual(self.apply(self.loaded(), [])["notices"], [])

    def test_refusal_notice_does_not_prevent_later_stop(self):
        """An inapplicable grant is consumed with a notice and continues in the same invocation."""
        c = self.restore("autonomous/plan")
        result = self.apply(c, ["extend_1", "stop"])
        self.assertEqual(result["saves"], [["plan", 1001, None, None], ["stopped", 1002, "plan", None]])
        self.assertIsNone(result["blocked"])
        self.assertEqual(len(result["notices"]), 1)
        self.assertIn("No exhausted plan revision is waiting", result["notices"][0])
        self.assertEqual(self.apply(self.loaded(), [])["notices"], [])

    def test_raised_grant_refusal_allows_a_later_valid_grant_after_reload(self):
        """A raised refusal ends the tick but its consumed ID cannot wedge the next command."""
        c = self.restore("autonomous/escalated_waiting")
        before = deepcopy(c.state)
        refused = self.apply(c, ["extend_0", "extend_1"])
        self.assertEqual(refused["blocked"], "Grant a positive integer number of additional plan revisions.")
        self.assertEqual(refused["saves"], [["escalated", 1001, "plan", {"stage": "plan"}]])
        c.suspend("blocked", refused["blocked"], outboxes=False)
        restored = self.loaded()
        accepted = self.apply(restored, [])
        self.assertTrue(accepted["return"])
        self.assertEqual(accepted["saves"], [["plan", 1002, None, None]])
        self.assertEqual(restored.state["plan_revision_extra"], 1)
        self.assertEqual(restored.state["plan_revisions"], before["plan_revisions"] + 1)
        self.assertEqual(restored.state["objections"], before["objections"])
        self.assertEqual(restored.state["calls"], before["calls"])
        self.assertEqual(self.apply(self.loaded(), [])["saves"], [])

    def test_refused_invalidate_is_consumed_by_the_callers_blocked_save(self):
        """Invalidation's no-save refusal still permits the queued retry after the worker saves."""
        c = self.restore("autonomous/publish")
        pending = deepcopy(c.state["pending"])
        refused = self.apply(c, ["invalidate", "retry"])
        self.assertEqual(
            refused["blocked"],
            "Publication is pending. Use /concurro retry to finish the recorded publication, "
            "then /concurro invalidate to reassess it.",
        )
        self.assertEqual(refused["saves"], [])
        self.assertEqual(c.state["command_id"], 1001)
        self.assertNotEqual(self.loaded().state["command_id"], 1001, "refusal is not durable until the caller saves")
        c.suspend("blocked", refused["blocked"], outboxes=False)
        restored = self.loaded()
        retried = self.apply(restored, [])
        self.assertEqual(retried["saves"], [["publish", 1002, None, None]])
        self.assertEqual(restored.state["pending"], pending)
        self.assertNotIn("invalidations", restored.state)
        self.assertEqual(self.apply(self.loaded(), [])["saves"], [])

    def test_lost_save_reply_replays_each_command_without_duplicate_effects(self):
        """Real durable saves followed by process loss cannot replay grants, resets or notices."""
        cases = [
            ("manual/uninitialized", "start"),
            ("manual/planned", "approve_match"),
            ("autonomous/blocked_implement", "retry"),
            ("autonomous/plan", "stop"),
            ("autonomous/escalated_waiting", "extend_1"),
            ("autonomous/gates", "invalidate"),
            ("autonomous/plan", "extend_1"),
        ]
        for scenario, command in cases:
            with self.subTest(scenario=scenario, command=command):
                c = self.restore(scenario)
                self.gh.comment(self.body(command, c))
                original = c.save

                def save_then_crash(*args, **kwargs):
                    """Lose the reply only after the complete real save and notice publication."""
                    original(*args, **kwargs)
                    raise RuntimeError("lost command save reply")

                with patch.object(c, "save", side_effect=save_then_crash):
                    with self.assertRaisesRegex(RuntimeError, "^lost command save reply$"):
                        c.commands(list(self.gh.comments))
                restored = self.loaded()
                before = deepcopy(restored.state)
                comments = deepcopy(self.gh.comments)
                replay = self.apply(restored, [])
                self.assertIsNone(replay["return"])
                self.assertIsNone(replay["blocked"])
                self.assertEqual(replay["saves"], [])
                self.assertEqual(restored.state, before)
                self.assertEqual(self.gh.comments, comments)

    def test_filters_run_before_permission_checks_and_keep_supplied_order(self):
        """Stale IDs and malformed text are ignored before authorization; comments are not sorted here."""
        c = self.restore("autonomous/plan")
        c.state["command_id"] = 100
        comments = [
            {"id": 100, "body": "/concurro invalidate", "user": {"login": "stale"}},
            {"id": 101, "body": "Please /concurro stop", "user": {"login": "malformed"}},
            {"id": 102, "body": "/concurro stop", "user": {"login": "outsider"}},
            {"id": 104, "body": " \n/concurro stop\n ", "user": {"login": "writer"}},
            {"id": 103, "body": "/concurro invalidate", "user": {"login": "older"}},
        ]
        with (
            patch.object(c, "permitted", wraps=c.permitted) as permitted,
            patch.object(c, "save", wraps=c.save) as saved,
        ):
            self.assertIsNone(c.commands(comments))
        self.assertEqual([call.args for call in permitted.call_args_list], [("outsider",), ("writer",)])
        saved.assert_called_once()
        self.assertEqual((c.state["stage"], c.state["command_id"]), ("stopped", 104))
        self.assertNotIn("invalidations", c.state)

    def test_malformed_commands_never_reach_authorization(self):
        """Pin exact parser boundaries before moving the regex into a separate module."""
        bodies = [
            "/concurro",
            "/concurro  stop",
            "/Concurro stop",
            "/concurro stop please",
            "/concurro retry --plan-revisions -1",
            "/concurro retry --plan-revisions 1000",
            "/concurro retry --plan-revisions 1.0",
            "/concurro retry --plan-revisions 1 extra",
            "/concurro approve " + "a" * 63,
            "/concurro approve " + "A" * 64,
            "/concurro stop\n/concurro retry",
            "```text\n/concurro stop\n```",
        ]
        for body in bodies:
            with self.subTest(body=body):
                c = self.restore("autonomous/plan")
                before = deepcopy(c.state)
                self.gh.comment(body)
                with patch.object(c, "permitted", side_effect=AssertionError("malformed command was authorized")):
                    self.assertIsNone(c.commands([self.gh.comments[-1]]))
                self.assertEqual(c.state, before)

    def test_approval_cannot_waive_missing_consensus(self):
        """A matching plan ID is insufficient; pin refusal consumption and persistence independently."""
        c = self.restore("manual/planned")
        c.state["design_votes"] = {}
        prior_id = c.state["command_id"]
        result = self.apply(c, ["approve_match"])
        self.assertEqual(result["blocked"], "writer command cannot waive design consensus")
        self.assertEqual(result["command_id"], prior_id)
        self.assertEqual(result["saves"], [])
        self.assertEqual(result["stage"], "planned")
        self.assertNotIn("approval", c.state)

    def test_retry_resets_correction_allowances_without_reassessment(self):
        """Every output-repair clause preserves spent budgets and the current design generation."""
        for kind, stage in (("intake", "intake"), ("plan", "plan"), ("review", "plan_qc"), ("batch", "plan_qc")):
            for paused in ("blocked", "escalated"):
                with self.subTest(kind=kind, paused=paused):
                    c = self.restore(f"autonomous/{paused}_{kind}_output")
                    before = deepcopy(c.state)
                    result = self.apply(c, ["retry"])
                    self.assertEqual(result["stage"], stage)
                    self.assertIsNone(result["blocked"])
                    for key in (
                        "calls",
                        "credentials",
                        "plan_revisions",
                        "design_generation",
                        "objections",
                        "design_eligibility",
                    ):
                        self.assertEqual(c.state.get(key), before.get(key), key)
                    self.assertNotIn("operation", c.state)
                    if kind in ("intake", "plan"):
                        self.assertEqual(c.state[kind + "_draft"]["attempt"], 0)
                    else:
                        repairs = (
                            [c.state["review_repair"]]
                            if kind == "review"
                            else [
                                m["review_repair"]
                                for m in c.state["review_batch"]["members"].values()
                                if m.get("review_repair")
                            ]
                        )
                        self.assertTrue(repairs)
                        for repair in repairs:
                            self.assertNotIn("attempts", repair)
                            self.assertNotIn("signature", repair)

    def test_intake_restarts_keep_their_distinct_cleanup_and_base_rules(self):
        """Manual start, terminal retry and autonomous refresh must not be merged by resemblance."""
        keys = ("plan", "plan_id", "branch", "qc", "design_parent", "approved_accumulated_files")
        for path, removed in (("start", set(keys)), ("retry", set()), ("refresh", set(keys[:4]))):
            with self.subTest(path=path):
                c = self.restore("manual/blocked_plan_qc" if path == "start" else "autonomous/plan_qc")
                if path == "retry":
                    c.finish_quality_control("plan_qc", decision("escalate"))
                c.state.update(
                    branch="fixture-existing-branch",
                    design_parent=c.state["base"],
                    approved_accumulated_files=["docs/guide.md"],
                )
                before = deepcopy(c.state)
                self.gh.issue["body"] += " Clarification from the writer."
                original_repo = self.gh.repo

                def repo(endpoint, method="GET", data=None):
                    """Make refetching the base observable without changing other fake Git operations."""
                    if endpoint == "git/ref/heads/main":
                        return {"object": {"sha": "b" * 40}}
                    return original_repo(endpoint, method, data)

                with (
                    patch.object(self.gh, "repo", side_effect=repo) as requests,
                    patch.object(c, "save", wraps=c.save) as saved,
                ):
                    if path == "refresh":
                        self.assertTrue(c.refresh_autonomous(list(self.gh.comments)))
                    else:
                        result = self.apply(c, [path])
                        self.assertIsNone(result["return"])
                        self.assertIsNone(result["blocked"])
                self.assertEqual(
                    sum(call.args[0] == "git/ref/heads/main" for call in requests.call_args_list),
                    1 if path == "start" else 0,
                )
                saved.assert_called_once()
                self.assertEqual(c.state["base"], "b" * 40 if path == "start" else before["base"])
                self.assertEqual(c.state["stage"], "intake")
                self.assertEqual(c.state["requirements"]["body"], self.gh.issue["body"])
                self.assertEqual(c.state["issue_digest"], c.issue_digest())
                self.assertEqual(c.state["calls"], before["calls"])
                self.assertEqual(c.state["plan_revisions"], before["plan_revisions"] + 1)
                self.assertEqual(c.state["design_generation"], before["design_generation"] + 1)
                for key in keys:
                    if key in removed:
                        self.assertNotIn(key, c.state)
                    else:
                        self.assertEqual(c.state[key], before[key])
                if path == "retry":
                    self.assertNotIn("operation", c.state)
                    self.assertNotIn("retry_stage", c.state)
                else:
                    self.assertEqual(c.state.get("operation"), before.get("operation"))
                    self.assertEqual(c.state.get("retry_stage"), before.get("retry_stage"))

    def test_zero_padded_revision_grant_is_parsed_as_an_integer(self):
        """The current one-to-three-digit grammar accepts leading zeroes."""
        c = self.restore("autonomous/escalated_waiting")
        result = self.apply(c, ["extend_001"])
        self.assertTrue(result["return"])
        self.assertIsNone(result["blocked"])
        self.assertEqual(c.state["plan_revision_extra"], 1)
        self.assertEqual(c.state["plan_revision_grant"]["added"], 1)

    def paused_publication(self, stage, kind, mode="manual"):
        """Suspend a real PR/publication; a head-only variant pins that independent guard."""
        c = self.restore(mode + ("/publish" if kind == "pending" else "/review"))
        if kind == "head":
            c.state.pop("pr")
        c.suspend(stage, "Original interruption reason.", outboxes=False)
        return c

    def assert_start_notice(self, notices, pending=False):
        """Pin the fixed guidance while allowing unrelated notices from a following command."""
        matching = [n for n in notices if n.startswith("### Start command not applied\n")]
        self.assertEqual(len(matching), 1)
        body = matching[0].split("\n\n<sub>", 1)[0]
        guidance = (
            "Use `/concurro retry` to finish the pending publication."
            if pending
            else "Use `/concurro retry` to resume, or `/concurro invalidate` to reassess."
        )
        self.assertEqual(
            body,
            "### Start command not applied\n\n"
            "`/concurro start` cannot restart a task with an open PR or prepared publication. " + guidance,
        )

    def test_manual_start_refusal_preserves_pause_and_posts_once(self):
        """A refused start consumes only its command; it cannot replace the original block reason."""
        for stage in ("blocked", "stopped"):
            for kind in ("pr", "head", "pending"):
                with self.subTest(stage=stage, kind=kind):
                    c = self.paused_publication(stage, kind)
                    before = deepcopy(c.state)
                    command_id = self.gh.counter + 1
                    result = self.apply(c, ["start"])
                    self.assertIsNone(result["blocked"])
                    self.assertIsNone(result["return"])
                    self.assertEqual(result["saves"], [[stage, command_id, before["retry_stage"], None]])
                    self.assertEqual(c.state["command_id"], command_id)
                    self.assert_start_notice(result["notices"], kind == "pending")
                    for key in (
                        "stage",
                        "reason",
                        "retry_stage",
                        "calls",
                        "credentials",
                        "plan_revisions",
                        "approval",
                        "plan",
                        "head",
                        "pr",
                        "pending",
                        "requirements",
                        "design_generation",
                    ):
                        self.assertEqual(c.state.get(key), before.get(key), key)
                    self.assertEqual(self.apply(self.loaded(), [])["notices"], [])

    def test_manual_start_refusal_reaches_queued_retry_in_same_tick(self):
        """Blocked and stopped tasks can resume without deleting the refused start comment."""
        for stage in ("blocked", "stopped"):
            for kind in ("pr", "head", "pending"):
                with self.subTest(stage=stage, kind=kind):
                    c = self.paused_publication(stage, kind)
                    before = deepcopy(c.state)
                    start_id = self.gh.counter + 1
                    result = self.apply(c, ["start", "retry"])
                    target = before["retry_stage"]
                    self.assertIsNone(result["blocked"])
                    self.assertIsNone(result["return"])
                    self.assertEqual(
                        result["saves"], [[stage, start_id, target, None], [target, start_id + 1, None, None]]
                    )
                    self.assertEqual(c.state["stage"], target)
                    self.assertEqual(c.state["command_id"], start_id + 1)
                    self.assert_start_notice(result["notices"], kind == "pending")
                    self.assertEqual(c.state["calls"], before["calls"])
                    self.assertEqual(c.state["design_generation"], before["design_generation"])
                    self.assertEqual(c.state.get("pending"), before.get("pending"))
                    self.assertEqual(self.apply(self.loaded(), [])["saves"], [])

    def test_start_refusal_lost_save_reply_does_not_duplicate_notice_or_retry(self):
        """Replay skips the saved refusal, runs its queued retry once and leaves budgets untouched."""
        for stage in ("blocked", "stopped"):
            for kind in ("pr", "head", "pending"):
                with self.subTest(stage=stage, kind=kind):
                    c = self.paused_publication(stage, kind)
                    before = deepcopy(c.state)
                    self.gh.comment("/concurro start")
                    start_id = self.gh.comments[-1]["id"]
                    self.gh.comment("/concurro retry")
                    retry_id = self.gh.comments[-1]["id"]
                    original = c.save

                    def save_then_crash(*args, **kwargs):
                        """Lose acknowledgement after the consumed command and notice are durable."""
                        original(*args, **kwargs)
                        raise RuntimeError("lost start refusal save reply")

                    with patch.object(c, "save", side_effect=save_then_crash):
                        with self.assertRaisesRegex(RuntimeError, "^lost start refusal save reply$"):
                            c.commands(list(self.gh.comments))
                    restored = self.loaded()
                    self.assertEqual(restored.state["command_id"], start_id)
                    for key in ("stage", "reason", "retry_stage", "calls", "design_generation"):
                        self.assertEqual(restored.state.get(key), before.get(key), key)
                    resumed = self.apply(restored, [])
                    self.assertEqual(resumed["saves"], [[before["retry_stage"], retry_id, None, None]])
                    self.assertEqual(resumed["notices"], [])
                    self.assertIsNone(resumed["blocked"])
                    notices = [
                        comment["body"].split("\n", 1)[1]
                        for comment in self.gh.comments
                        if comment["body"].startswith(conversation.MARKER)
                    ]
                    self.assert_start_notice(notices, kind == "pending")
                    spent = {k: deepcopy(restored.state.get(k)) for k in ("calls", "credentials", "plan_revisions")}
                    self.assertEqual(spent, {k: before.get(k) for k in spent})
                    saved_state = deepcopy(restored.state)
                    comments = deepcopy(self.gh.comments)
                    replayed = self.apply(restored, [])
                    self.assertEqual(replayed["saves"], [])
                    self.assertEqual(restored.state, saved_state)
                    self.assertEqual(self.gh.comments, comments)

    def test_start_refusal_reaches_queued_invalidation(self):
        """The alternative reassessment command also remains reachable in the same invocation."""
        c = self.paused_publication("blocked", "pr")
        before = deepcopy(c.state)
        start_id = self.gh.counter + 1
        result = self.apply(c, ["start", "invalidate"])
        self.assertIsNone(result["blocked"])
        self.assertTrue(result["return"])
        self.assertEqual(result["saves"], [["blocked", start_id, "review", None], ["intake", start_id + 1, None, None]])
        self.assertEqual(c.state["design_generation"], before["design_generation"] + 1)
        self.assertEqual(len(c.state["invalidations"]), 1)
        self.assertEqual(c.state["calls"], before["calls"])
        self.assert_start_notice(result["notices"])
        self.assertEqual(self.apply(self.loaded(), [])["saves"], [])

    def test_autonomous_start_still_ignores_paused_publications(self):
        """The manual refusal path must not consume autonomous start commands or publish notices."""
        for stage in ("blocked", "stopped"):
            for kind in ("pr", "head", "pending"):
                with self.subTest(stage=stage, kind=kind):
                    c = self.paused_publication(stage, kind, mode="autonomous")
                    before = deepcopy(c.state)
                    result = self.apply(c, ["start"])
                    self.assertEqual(c.state, before)
                    self.assertEqual(result["saves"], [])
                    self.assertEqual(result["notices"], [])
                    self.assertIsNone(result["blocked"])
