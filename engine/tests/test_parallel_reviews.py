"""Parallel design dispatch with real controller persistence and deterministic turn barriers."""

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import conversation
import review_batch
from agents import Agents
from checkpoints import AUDIT_MARKER, BATCH_FORMAT, STATE_PREFIX, CheckpointError, legacy_state
from controller import Controller
from fakes import BatchAgents, Crash, FakeGitHub, decision, named_config
from pipeline_fixtures import ConsensusFixture
from policy import Blocked, SessionFailure, UncertainOutcome, design_roster


class ParallelTests(ConsensusFixture, unittest.TestCase):
    """Exercise production consensus and checkpoint paths using an asynchronous fake transport."""

    def setUp(self):
        """Start from a persisted plan awaiting independent design reviews."""
        super().setUp()
        self.agents = BatchAgents()
        self.reach("plan_qc")
        self.agents.events.clear()

    def test_roster_starts_before_results_and_fast_second_is_applied_first(self):
        """Verify roster starts before results and fast second is applied first."""
        roster = design_roster(self.config)
        first, second = [m["provider"] for m in roster]
        self.agents.delay[first] = 2
        applied = []
        original = Controller.apply_quality_report

        def apply(c, stage, provider, member, context, report):
            """Record application order while retaining real verdict policy."""
            applied.append(provider)
            return original(c, stage, provider, member, context, report)

        with patch.object(Controller, "apply_quality_report", apply):
            state = self.tick()
        self.assertEqual(self.agents.events[:2], [("submit", first), ("submit", second)])
        self.assertEqual(applied, [second, first])
        self.assertEqual(state["stage"], "implement")
        self.assertEqual(len(state["design_votes"]), 2)
        self.assertTrue(all(v["first_pass"] for v in state["design_votes"].values()))
        self.assertEqual(legacy_state(self.gh.artifacts[7][1]["artifact"]["body"])["state"]["version"], BATCH_FORMAT)
        self.assertNotIn("review_batch", state)

    def test_insufficient_roster_budget_charges_and_submits_nothing(self):
        """Verify insufficient roster budget charges and submits nothing."""
        c = self.load()
        before = c.state["calls"]
        c.config["max_model_calls"] = before + 1
        with self.assertRaisesRegex(Blocked, "roster"):
            c.quality_control("plan_qc")
        self.assertEqual(c.state["calls"], before)
        self.assertEqual(self.agents.events, [])
        c.config["max_model_calls"] = before + 2
        c.quality_control("plan_qc")
        self.assertEqual(c.state["calls"], before + 2)
        self.assertEqual(c.state["stage"], "implement")

    def test_output_correction_does_not_repeat_or_expose_the_sibling(self):
        """Verify output correction does not repeat or expose the sibling."""
        roster = design_roster(self.config)
        first, second = [m["provider"] for m in roster]

        def invalid(report):
            """Return a correctable invalid charter goal ID."""
            report["scope"]["goal_ids"] = ["bad"]
            return report

        self.agents.effects[first] = [invalid]
        state = self.tick()
        self.assertEqual(state["stage"], "implement")
        self.assertEqual([p for event, p in self.agents.events if event == "submit"].count(second), 1)
        calls = [context for p, stage, context in self.agents.calls if stage == "plan_qc" and p == first]
        self.assertEqual(len(calls), 2)
        self.assertIn("review_validation_feedback", calls[-1])
        self.assertNotIn("design_votes", calls[-1])
        self.assertEqual(calls[-1]["own_objections"], [])
        self.assertTrue(all(v["first_pass"] for v in state["design_votes"].values()))
        self.assertEqual([r["outcome"] for r in state["reports"] if r["step"] == "Design review"].count("invalid"), 1)

    def test_provider_restart_is_scoped_and_forces_fresh_phase(self):
        """Verify provider restart is scoped and forces fresh phase."""
        first, second = [m["provider"] for m in design_roster(self.config)]
        self.agents.effects[first] = [SessionFailure("failed", uncertain=True)]
        state = self.tick()
        self.assertEqual(state["design_phase"], "fresh")
        self.assertEqual(state["stage"], "plan_qc")
        self.assertEqual([p for event, p in self.agents.events if event == "cancel"], [first])
        self.assertEqual([p for event, p in self.agents.events if event == "submit"].count(second), 1)
        state = self.tick()
        self.assertEqual(state["stage"], "implement")

    def test_crash_after_reservation_keeps_charges_sessions_and_eligibility(self):
        """A controller crash reuses the journal without claiming that the reserved calls did not run."""
        before = self.load().state["calls"]
        self.agents.fail_submit = 1
        with self.assertRaises(Crash):
            self.tick()
        state = self.load().state
        self.assertEqual(state["calls"], before + 2)
        self.assertTrue(all(m["operation"]["status"] == "started" for m in state["review_batch"]["members"].values()))
        scopes = {role: session["scope"] for role, session in state["sessions"].items()}
        self.agents.fail_submit = None
        self.agents.generation += 1
        state = self.tick()
        self.assertEqual(state["calls"], before + 4)
        self.assertEqual(state["stage"], "implement")
        self.assertEqual({role: session["scope"] for role, session in state["sessions"].items()}, scopes)
        self.assertTrue(all(v["eligible"] for v in state["design_eligibility"].values()))
        self.assertTrue(all(s["restarts"] == 0 for s in state["sessions"].values()))

    def test_saved_sibling_results_replay_without_another_dispatch(self):
        """Verify saved sibling results replay without another dispatch."""
        before = self.load().state["calls"]
        with patch.object(Controller, "apply_quality_report", side_effect=Crash):
            with self.assertRaises(Crash):
                self.tick()
        state = self.load().state
        self.assertEqual(state["calls"], before + 2)
        self.assertTrue(all(m["operation"]["status"] == "completed" for m in state["review_batch"]["members"].values()))
        with patch.object(self.agents, "submit", side_effect=AssertionError("repeated")):
            state = self.tick()
        self.assertEqual(state["calls"], before + 2)
        self.assertEqual(state["stage"], "implement")

    def test_terminal_result_cancels_running_sibling_and_cannot_be_overridden(self):
        """Verify terminal result cancels running sibling and cannot be overridden."""
        first, second = [m["provider"] for m in design_roster(self.config)]

        def escalate(report):
            """Return a valid terminal escalation."""
            report.update(decision="escalate", risk="high")
            return report

        self.agents.effects[first] = [escalate]
        self.agents.delay[second] = 5
        state = self.tick()
        self.assertEqual(state["stage"], "escalated")
        self.assertEqual([p for event, p in self.agents.events if event == "cancel"], [second])
        self.assertNotIn("approval", state)

    def test_changed_owner_cannot_apply_a_completed_result(self):
        """Verify changed owner cannot apply a completed result."""
        original = self.agents.observe

        def observe(handle):
            """Advance one observation without blocking a sibling."""
            result = original(handle)
            self.agents.owner = False
            return result

        with patch.object(self.agents, "observe", side_effect=observe), self.assertRaisesRegex(Blocked, "ownership"):
            self.tick()
        state = self.load().state
        self.assertEqual(state["design_votes"], {})
        self.assertTrue(all(h.cancelled for h in self.agents.handles))

    def test_large_malformed_report_is_corrected_not_blocked_by_checkpoint_size(self):
        """Verify large malformed report is corrected not blocked by checkpoint size."""
        first = design_roster(self.config)[0]["provider"]
        self.agents.effects[first] = [lambda _: ["not a report"] * 15000]
        state = self.tick()
        self.assertEqual(state["stage"], "implement")
        self.assertTrue(any(r["outcome"] == "invalid" for r in state["reports"]))

    def test_two_independent_corrections_compete_without_exceeding_last_call(self):
        """Only one correction is admitted when the initial roster leaves one call."""
        c = self.load()
        before = c.state["calls"]
        c.config["max_model_calls"] = before + 3
        for member in design_roster(self.config):
            self.agents.effects[member["provider"]] = [lambda r: {**r, "scope": {**r["scope"], "goal_ids": ["bad"]}}]
        with self.assertRaisesRegex(Blocked, "budget exhausted"):
            c.quality_control("plan_qc")
        self.assertEqual(c.state["calls"], before + 3)
        members = c.state["review_batch"]["members"]
        self.assertEqual([m["review_repair"]["attempts"] for m in members.values()], [1, 1])
        self.assertTrue(all(h.cancelled or h.status == "done" for h in self.agents.handles))
        self.assertEqual(len([r for r in c.state["reports"] if r.get("diagnostic")]), 2)

    def test_exhausted_correction_retry_preserves_sibling_vote_and_receipt(self):
        """Retry renews this member's allowance without dispatching its approved sibling."""
        first, second = [m["provider"] for m in design_roster(self.config)]
        bad = lambda r: {**r, "scope": {**r["scope"], "goal_ids": ["bad"]}}
        self.agents.effects[first] = [bad, bad]
        state = self.tick()
        self.assertEqual(state["stage"], "escalated")
        before = state["calls"]
        receipts = deepcopy(state["design_eligibility"])
        self.gh.comments.append({"id": 900, "user": {"login": "writer"}, "body": "/concurro retry"})
        state = self.tick()
        self.assertEqual(state["stage"], "implement")
        self.assertEqual(state["calls"], before + 1)
        self.assertEqual(state["design_eligibility"], receipts)
        self.assertEqual([p for e, p in self.agents.events if e == "submit"].count(second), 1)

    def test_fatal_member_error_preserves_ready_sibling_before_stopping(self):
        """An error does not discard a result available in the same observation sweep."""
        first = design_roster(self.config)[0]["provider"]
        self.agents.effects[first] = [Blocked("transport refused")]
        with self.assertRaisesRegex(Blocked, "transport refused"):
            self.tick()
        members = self.load().state["review_batch"]["members"]
        sibling = members[design_roster(self.config)[1]["id"]]
        self.assertEqual(sibling["operation"]["status"], "completed")
        self.assertIn("response", sibling["operation"])

    def test_batch_wire_guard_and_reference_paths_fail_closed(self):
        """Serial readers cannot silently ignore batch authority or reference it as report data."""
        with patch.object(Controller, "apply_quality_report", side_effect=Crash):
            with self.assertRaises(Crash):
                self.tick()
        c = self.load()
        for member in c.state["review_batch"]["members"].values():
            member["operation"]["response"]["rationale"] += "Evidence. " * 2000
        c.checkpoint()
        body = c.checkpoints.audit_body(c.state)
        raw = legacy_state(body)
        self.assertEqual(raw["state"]["version"], 4)
        self.assertEqual(c.checkpoints.decode({"body": body}), c.state)
        for version in (2, 3, 6):
            broken = deepcopy(raw)
            broken["state"]["version"] = version
            with self.assertRaises(CheckpointError):
                c.checkpoints.decode({"body": AUDIT_MARKER + STATE_PREFIX + json.dumps(broken) + "\n```\n"})
        reference = next(x for x in raw["report_data"] if x["path"][0] == "review_batch")
        for path in (["review_batch"], ["review_batch", "binding"], ["review_batch", "members", "other", "identity"]):
            broken = deepcopy(raw)
            broken["report_data"].append({**reference, "path": path})
            with self.assertRaises(CheckpointError):
                c.checkpoints.decode({"body": AUDIT_MARKER + STATE_PREFIX + json.dumps(broken) + "\n```\n"})

    def test_dashboard_reports_every_member_status_without_peer_context(self):
        """Queued, running and correcting members remain individually visible."""
        snapshots = []
        original = Controller.summary_body

        def capture(c, *args, **kwargs):
            """Observe the real published dashboard transitions."""
            body = original(c, *args, **kwargs)
            snapshots.append(body)
            return body

        first, second = [m["provider"] for m in design_roster(self.config)]
        self.agents.delay[second] = 3
        self.agents.effects[first] = [lambda r: {**r, "scope": {**r["scope"], "goal_ids": ["bad"]}}]
        with patch.object(Controller, "summary_body", capture):
            self.tick()
        self.assertTrue(any(first + ": Queued" in b and second + ": Queued" in b for b in snapshots))
        self.assertTrue(any(first + ": Correcting output" in b and second + ": Reviewing" in b for b in snapshots))

    def test_observed_intervals_overlap_without_inventing_serial_savings(self):
        """Retained per-reviewer receipts prove deterministic overlap and correct attribution."""
        first, second = [m["provider"] for m in design_roster(self.config)]
        self.agents.delay = {first: 3, second: 1}
        state = self.tick()
        reports = {x["provider"]: x["timing"] for x in state["reports"] if x["step"] == "Design review"}
        a, b = reports[first], reports[second]
        self.assertEqual(min(a["finished"], b["finished"]) - max(a["started"], b["started"]), 1)
        self.assertNotEqual(a["operation"], b["operation"])
        self.assertNotEqual(a["turn"], b["turn"])
        self.assertTrue(all("timing" not in context for _, _, context in self.agents.calls))

    def test_named_same_cli_roster_preserves_identities_and_pr_reviews_stay_serial(self):
        """Full design consensus uses profile IDs; production PR policy is unchanged."""
        self.config = named_config()
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.agents = BatchAgents()
        self.reach("plan_qc")
        state = self.tick()
        self.assertEqual(state["stage"], "implement")
        self.assertEqual(
            {v["identity"]["provider"] for v in state["design_votes"].values()}, {"sol-high", "astra-high"}
        )
        self.assertTrue(all(v["first_pass"] for v in state["design_votes"].values()))
        self.reach("review")
        before = len([s for _, s, _ in self.agents.calls if s == "review"])
        state = self.tick()
        self.assertEqual(len([s for _, s, _ in self.agents.calls if s == "review"]), before + 1)
        self.assertEqual(state["stage"], "review")

    def test_crash_before_first_submission_keeps_reservations_charged(self):
        """Process loss does not refund calls that might have been submitted."""
        before = self.load().state["calls"]
        with patch.object(self.agents, "submit", side_effect=Crash):
            with self.assertRaises(Crash):
                self.tick()
        self.assertEqual(self.load().state["calls"], before + 2)
        state = self.tick()
        self.assertEqual(state["calls"], before + 4)
        self.assertEqual(state["stage"], "implement")
        self.assertTrue(all(v["first_pass"] for v in state["design_votes"].values()))

    def test_revision_waits_for_every_vote_then_reuses_only_own_discussion(self):
        """Ordinary dissent gathers the roster; revised cooperative contexts stay independent."""
        self.agents.qc_reports = [decision("revise", concerns=["Add an error-path regression."]), decision("approve")]
        state = self.tick()
        self.assertEqual(state["stage"], "plan")
        self.assertEqual(len(state["design_history"][0]["votes"]), 2)
        self.assertNotIn("approval", state)
        self.assertNotIn("review_batch", state)
        state = self.reach("implement")
        self.assertEqual(state["calls"], 9)
        fresh = [c for _, stage, c in self.agents.calls if stage == "plan_qc" and c["review_phase"] == "fresh"]
        self.assertEqual(len(fresh), 2)
        self.assertTrue(all("controller_session" not in c and "own_objections" not in c for c in fresh))

    def test_lost_report_publication_replays_sibling_without_duplicate_call_or_archive(self):
        """Result retention and independent consumption survive an archive's lost response."""
        original = self.gh.repo
        lost = []

        def disconnect(path, method="GET", data=None):
            """Lose exactly the first accepted review archive response."""
            result = original(path, method, data)
            if (
                not lost
                and method == "POST"
                and path.endswith("/artifacts")
                and (data or {}).get("body", "").startswith(conversation.MARKER)
            ):
                lost.append(data["body"])
                raise RuntimeError("lost response")
            return result

        with patch.object(self.gh, "repo", side_effect=disconnect), self.assertRaises(conversation.PublicationError):
            self.tick()
        before = self.load().state["calls"]
        with patch.object(self.agents, "submit", side_effect=AssertionError("duplicate dispatch")):
            state = self.tick()
        self.assertEqual(state["stage"], "implement")
        self.assertEqual(state["calls"], before)
        self.assertEqual(sum(item["artifact"]["body"] == lost[0] for item in self.gh.artifacts[7].values()), 1)

    def test_legacy_inflight_result_finishes_serially_before_missing_member_batches(self):
        """Upgrading execution does not repeat the legacy saved result or upgrade provenance."""
        self.agents.parallel = False
        with patch.object(Controller, "apply_quality_report", side_effect=Crash):
            with self.assertRaises(Crash):
                self.tick()
        before = self.load().state["calls"]
        self.agents.parallel = True
        state = self.tick()
        self.assertEqual(state["calls"], before)
        self.assertEqual(len(state["design_votes"]), 1)
        state = self.tick()
        self.assertEqual(state["calls"], before + 1)
        self.assertEqual(state["stage"], "implement")

    def fresh(self):
        """Start a fresh phase without a cooperative transcript or completed votes."""
        c = self.load()
        c.state.update(design_phase="fresh", design_votes={})
        c.save()
        return c.state["calls"]

    def test_fresh_lost_and_uncertain_turns_retry_without_cancelling_healthy_sibling(self):
        """Real decoding distinguishes sessionless uncertainty from retained-session failure."""
        for outcome in ("lost", "uncertain"):
            with self.subTest(outcome=outcome):
                self.setUp()
                before = self.fresh()
                first, second = [m["provider"] for m in design_roster(self.config)]
                original = self.agents.decode
                failed = []

                def decode(turn, result):
                    """Decode one real sessionless failure, then use the normal fixture reports."""
                    if turn["provider"] == first and not failed:
                        failed.append(True)
                        return Agents.decode_result(None, None, first, "plan_qc", {"outcome": outcome})
                    return original(turn, result)

                # The sibling is still running when the first reviewer needs its replacement.
                self.agents.delay[second] = 2
                with patch.object(self.agents, "decode", side_effect=decode):
                    state = self.tick()
                self.assertEqual(state["stage"], "implement")
                self.assertEqual(state["calls"], before + 3)
                self.assertEqual([p for e, p in self.agents.events if e == "submit"], [first, second, first])
                self.assertEqual([p for e, p in self.agents.events if e == "cancel"], [first])
                self.assertEqual(len(state["design_votes"]), 2)
                self.assertTrue(all("controller_session" not in h.turn["context"] for h in self.agents.handles))

    def test_fresh_uncertain_allowance_exhaustion_preserves_results_and_cancels_only_running_work(self):
        """Two uncertain outcomes stop admission without a third charge or erased sibling evidence."""
        for sibling_done in (True, False):
            with self.subTest(sibling_done=sibling_done):
                self.setUp()
                before = self.fresh()
                first, second = [m["provider"] for m in design_roster(self.config)]
                self.agents.effects[first] = [UncertainOutcome("lost"), UncertainOutcome("lost again")]
                if not sibling_done:
                    self.agents.delay[second] = 5
                with self.assertRaisesRegex(Blocked, "uncertain twice"):
                    self.tick()
                state = self.load().state
                self.assertEqual(state["calls"], before + 3)
                sibling = design_roster(self.config)[1]["id"]
                self.assertEqual(sibling in state["design_votes"], sibling_done)
                cancellations = [p for e, p in self.agents.events if e == "cancel"]
                self.assertEqual(cancellations.count(first), 2)
                self.assertEqual(cancellations.count(second), 0 if sibling_done else 1)
                with patch.object(self.agents, "submit", side_effect=AssertionError("allowance reset")):
                    with self.assertRaisesRegex(Blocked, "uncertain twice"):
                        self.tick()
                self.assertEqual(self.load().state["calls"], before + 3)

    def test_uncertain_retry_cannot_exceed_the_remaining_task_budget(self):
        """A replacement needs a new charge even when its sibling has already approved."""
        before = self.fresh()
        c = self.load()
        c.config["max_model_calls"] = before + 2
        first = design_roster(self.config)[0]["provider"]
        self.agents.effects[first] = [UncertainOutcome("lost")]
        with self.assertRaisesRegex(Blocked, "budget exhausted"):
            c.quality_control("plan_qc")
        self.assertEqual(c.state["calls"], before + 2)
        self.assertEqual(len(c.state["design_votes"]), 1)
        self.assertEqual(len(self.agents.handles), 2)

    def test_real_session_failure_after_crash_rotates_only_that_session(self):
        """Journal mismatch remains a real restart, with sticky eligibility loss."""
        self.agents.fail_submit = 1
        with self.assertRaises(Crash):
            self.tick()
        previous = self.load().state
        scopes = {role: session["scope"] for role, session in previous["sessions"].items()}
        first, second = [m["provider"] for m in design_roster(self.config)]
        self.agents.fail_submit = None
        self.agents.effects[first] = [SessionFailure("journal mismatch")]
        state = self.tick()
        self.assertEqual(state["design_phase"], "fresh")
        changed = [role for role, session in state["sessions"].items() if session["scope"] != scopes[role]]
        self.assertEqual(len(changed), 1)
        self.assertIn(first, changed[0])
        receipts = state["design_eligibility"]
        self.assertFalse(receipts[design_roster(self.config)[0]["id"]]["eligible"])
        self.assertTrue(receipts[design_roster(self.config)[1]["id"]]["eligible"])

    def test_crash_recovery_does_not_upgrade_missing_legacy_provenance(self):
        """Reusing a journal does not manufacture an independence receipt."""
        with patch.object(self.agents, "submit", side_effect=Crash):
            with self.assertRaises(Crash):
                self.tick()
        c = self.load()
        c.state.pop("design_eligibility")
        c.save()
        state = self.tick()
        self.assertEqual(state["design_phase"], "fresh")
        self.assertTrue(all(not v["eligible"] for v in state["design_eligibility"].values()))
        self.assertTrue(all(s["restarts"] == 0 for s in state["sessions"].values()))

    def test_observation_sweep_waits_five_seconds(self):
        """Batch polling uses a fixed modest interval without affecting serial long polls."""
        executor = SimpleNamespace(sleep=Mock())
        agents = Agents(self.config, executor)
        agents.idle()
        executor.sleep.assert_called_once_with(5)


class PRAdapter:
    """PR-shaped fixture: exact head authority and serial findings/outbox application."""

    def __init__(self):
        """Configure controllable delays, failures and ownership for local tests."""
        self.transport = BatchAgents()
        self.head = self.expected = "a" * 40
        self.results, self.applied, self.contexts = {}, [], {}
        self.reopen = False

    def open(self):
        """Freeze each member's own findings and the same reviewed head."""
        self.contexts = {k: {"head": self.head, "own_findings": [k]} for k in ("a", "b")}
        return list(self.contexts)

    def reserve(self, keys, initial):
        """Use the common transport lifecycle without design-session concepts."""
        return {k: {"provider": k, "context": self.contexts[k]} for k in keys}

    def valid(self):
        """Head movement invalidates every unapplied review."""
        return self.head == self.expected

    def retained(self, key):
        """Saved results wait for serial application."""
        return key in self.results and key not in self.applied

    def complete(self, key, turn, result, timing):
        """Retain result without publishing findings concurrently."""
        self.results[key] = result

    def apply(self, key):
        """A design reopen invalidates the remaining results."""
        self.applied.append(key)
        if self.reopen:
            self.head = "b" * 40

    def needs_attempt(self, key):
        """No correction policy is introduced for PR reviews."""
        return key not in self.results

    def finished(self):
        """Wait for the full accepted roster."""
        return len(self.applied) == 2

    def status(self, key, status):
        """Transport status does not authorize a vote."""

    def failed(self, key, error):
        """PR failures retain the stage's own policy."""
        raise error

    def close(self):
        """No design-specific cleanup is required."""


class SharedCoordinatorTests(unittest.TestCase):
    """The reusable scheduler needs no design-specific context or verdict fields."""

    def test_pr_adapter_collects_then_applies_independent_results(self):
        """Verify pr adapter collects then applies independent results."""
        adapter = PRAdapter()
        review_batch.run(adapter)
        self.assertEqual(adapter.applied, ["a", "b"])
        self.assertEqual(adapter.transport.events[:2], [("submit", "a"), ("submit", "b")])
        self.assertEqual(adapter.contexts["a"]["own_findings"], ["a"])

    def test_pr_reopen_preserves_ready_results_but_never_applies_late_approval(self):
        """Verify pr reopen preserves ready results but never applies late approval."""
        adapter = PRAdapter()
        adapter.reopen = True
        review_batch.run(adapter)
        self.assertEqual(adapter.applied, ["a"])
        self.assertEqual(set(adapter.results), {"a", "b"})

    def test_head_movement_cancels_running_members_without_applying(self):
        """Verify head movement cancels running members without applying."""
        adapter = PRAdapter()
        adapter.transport.delay = {"a": 2, "b": 2}
        adapter.transport.idle = lambda: setattr(adapter, "head", "b" * 40)
        review_batch.run(adapter)
        self.assertEqual(adapter.applied, [])
        self.assertTrue(all(h.cancelled for h in adapter.transport.handles))
