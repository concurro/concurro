"""Attempt-scoped storage, cold recovery and scan-free first writes."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import attempt_result
import protocol
import sessions
from artifact_fixtures import fixed_parts
from attempt_traffic import measure
from checkpoints import CheckpointError, Checkpoints
from design_batch import Design
from fakes import CONFIG, PLAN, BatchAgents, Crash, FakeGitHub
from github import Transient
from pipeline_fixtures import PipelineFixture
from policy import Blocked, design_roster, digest
from report_store import ReportDataError, Store, projection


class AttemptArtifactsTests(PipelineFixture, unittest.TestCase):
    """Recover paid responses through production checkpoint boundaries."""

    def lost_response_checkpoint(self, stage="plan"):
        """Upload a planning result, then crash before its completion checkpoint commits."""
        c = self.reach(stage)
        provider, context = (
            ("claude", c.context()) if stage == "plan" else c.quality_context(stage, design_roster(c.config)[0])
        )
        response = deepcopy(PLAN)
        response["summary"] = "Detailed plan. " * 1800
        original = c.checkpoints.write

        def write(existing, kind, body):
            """Lose the audit commit only after detached response retention ran."""
            if kind == "generic" and c.state.get("operation", {}).get("status") == "completed":
                raise Crash()
            return original(existing, kind, body)

        with (
            patch.object(c.agents, "ask", return_value=response),
            patch.object(c.checkpoints, "write", side_effect=write),
        ):
            with self.assertRaises(Crash):
                c.ask(provider, stage, context)
        return c

    def test_new_unreferenced_objects_do_not_scan(self):
        """A fresh nonce makes discovery unnecessary for ordinary first storage."""
        with patch.object(self.gh, "pages", side_effect=AssertionError("collection scan")):
            store = Store(self.gh, 7, CONFIG["bot_login"])
            key = store.put({"summary": "new " * 4000})
        self.assertTrue(store.numbers[key])

    def test_distinct_uploads_same_digest_read_by_reference_or_discovery(self):
        """Unreferenced repeated content may have independent complete upload sets."""
        value = {"summary": "new " * 4000}
        first = Store(self.gh, 7, CONFIG["bot_login"])
        key = first.put(value)
        second = Store(self.gh, 7, CONFIG["bot_login"])
        second.put(value)
        self.assertNotEqual(first.numbers[key], second.numbers[key])
        cold = Store(self.gh, 7, CONFIG["bot_login"])
        self.assertEqual(cold.get(key), value)
        self.assertEqual(cold.numbers[key], first.numbers[key])
        self.assertEqual(cold.get(key, second.numbers[key]), value)
        self.assertEqual(cold.numbers[key], first.numbers[key])
        del self.gh.artifacts[7][second.numbers[key][0]]
        with self.assertRaises(ReportDataError):
            cold.get(key, second.numbers[key])

    def test_complete_upload_recovers_response_and_session_without_spending(self):
        """A response uploaded before the lost checkpoint remains usable at the call limit."""
        lost = self.lost_response_checkpoint()
        c = self.loaded()
        before = deepcopy(c.state)
        self.assertEqual(before["operation"]["status"], "started")
        expected = lost.state["operation"]["response"]
        with patch.object(c.agents, "ask", side_effect=AssertionError("new model call")):
            result = c.recover(c.state["operation"])
        self.assertEqual(result, expected)
        self.assertEqual(c.state["sessions"], lost.state["sessions"])
        self.assertEqual(c.state["calls"], before["calls"])
        self.assertEqual(c.state["credentials"], before["credentials"])
        self.assertEqual(self.loaded().state["operation"]["response"], expected)
        again = deepcopy(c.state["sessions"])
        self.assertEqual(c.recover(c.state["operation"]), expected)
        self.assertEqual(c.state["sessions"], again)

    def test_ask_recovers_before_call_credential_and_uncertainty_limits(self):
        """Already paid output bypasses replacement admission, even with no allowance left."""
        self.lost_response_checkpoint()
        c = self.loaded()
        c.config["max_model_calls"] = c.state["calls"]
        c.state["operation"]["uncertain"] = 2
        with patch.object(c.agents, "credential", side_effect=AssertionError("credential requested")):
            result = c.ask("claude", "plan", c.context())
        self.assertEqual(result, c.state["operation"]["response"])
        self.assertEqual(c.state["operation"]["uncertain"], 2)

    def test_only_started_operations_scan(self):
        """Non-started records retain their old paths; a live new call never discovers."""
        c = self.reach("plan")
        with patch.object(c.checkpoints.reports, "collection", side_effect=AssertionError("scan")):
            for status in ("new", "failed", "uncertain", "consumed", "completed"):
                self.assertIsNone(c.recover({"status": status}))
            c.ask("claude", "plan", c.context())

    def test_scan_failure_is_not_permission_to_replace(self):
        """A failed read propagates without changing the operation or spending a call."""
        self.lost_response_checkpoint()
        c = self.loaded()
        before = deepcopy(c.state)
        with patch.object(c.checkpoints.reports, "collection", side_effect=Transient("read failed")):
            with self.assertRaises(Transient):
                c.ask("claude", "plan", c.context())
        self.assertEqual(c.state, before)

    def test_stale_session_cannot_be_restored(self):
        """An explicit restart is not undone by an old completed upload."""
        self.lost_response_checkpoint()
        c = self.loaded()
        role = next(r for r in c.state["sessions"] if r.startswith("plan:"))
        sessions.restart(c.state, {**c.state["sessions"][role], "role": role})
        before = deepcopy(c.state)
        self.assertIsNone(c.recover(c.state["operation"]))
        self.assertEqual(c.state, before)

    def test_recovery_checkpoint_reply_loss_does_not_advance_twice(self):
        """A saved recovery result is replayed, without scanning or another session turn."""
        lost = self.lost_response_checkpoint()
        c = self.loaded()
        original = c.checkpoints.write

        def write(existing, kind, body):
            """Commit recovery but lose its reply."""
            result = original(existing, kind, body)
            if kind == "generic":
                raise Crash()
            return result

        with patch.object(c.checkpoints, "write", side_effect=write), self.assertRaises(Crash):
            c.recover(c.state["operation"])
        c = self.loaded()
        with patch.object(c.checkpoints.reports, "collection", side_effect=AssertionError("scan")):
            self.assertEqual(c.recover(c.state["operation"]), lost.state["operation"]["response"])
        self.assertEqual(c.state["sessions"], lost.state["sessions"])

    def test_inline_and_known_core_keep_the_precommit_retry_window(self):
        """Neither small responses nor reused cores get a separate completion upload."""
        for known in (False, True):
            with self.subTest(known=known):
                self.setUp()
                c = self.reach("plan")
                response = deepcopy(PLAN)
                if known:
                    response["summary"] = "large " * 4000
                    c.checkpoints.reports.put(response)
                before = len(self.gh.artifacts[7])
                original = c.checkpoints.write

                def write(existing, kind, body):
                    """Stop after retention, before the response checkpoint."""
                    if kind == "generic" and c.state.get("operation", {}).get("status") == "completed":
                        raise Crash()
                    return original(existing, kind, body)

                with (
                    patch.object(c.agents, "ask", return_value=response),
                    patch.object(c.checkpoints, "write", side_effect=write),
                    self.assertRaises(Crash),
                ):
                    c.ask("claude", "plan", c.context())
                self.assertEqual(len(self.gh.artifacts[7]), before)
                c = self.loaded()
                self.assertIsNone(c.recover(c.state["operation"]))
                self.assertEqual(c.state["operation"]["status"], "started")
                calls = c.state["calls"]
                dispatched = len(c.agents.calls)
                c.ask("claude", "plan", c.context())
                self.assertEqual(c.state["calls"], calls + 1)
                self.assertEqual(len(c.agents.calls), dispatched + 1)

    def test_raw_session_digest_and_normalized_retained_classification(self):
        """Session continuity uses the original result digest, not normalized ratings."""
        lost = self.lost_response_checkpoint("plan_qc")
        c = self.loaded()
        c.recover(c.state["operation"])
        role = next(r for r in c.state["sessions"] if r.startswith("plan_qc:"))
        completed = c.state["sessions"][role]["completed"]
        self.assertEqual(completed, lost.state["sessions"][role]["completed"])
        self.assertNotEqual(completed["response"], digest(c.state["operation"]["response"]))


class UploadRecoveryTests(PipelineFixture, unittest.TestCase):
    """Exercise multipart attempts and integrity without real model or GitHub traffic."""

    def prepared(self, response):
        """Freeze an existing charged attempt and its expected completion metadata."""
        c = self.reach("plan")
        context = c.prepare_context("claude", "plan", c.context())
        operation = c.operation("claude", "plan", context)
        context.update(operation["contract"])
        session = sessions.prepare(c.state, c.config, "claude", "plan", context)
        c.begin_attempt(operation, context)
        c.charge_call(c.agents.credential("claude", "plan", {}))
        c.save()
        c.record_session_turn(session, c.state["calls"], context, response)
        operation["timing"] = {
            "turn": "00000000-1234-1234-1234-123456789abc",
            "status": "done",
            "created": 99,
            "started": 100,
            "finished": 105,
        }
        completion = attempt_result.capture(c.state, operation, response, session, CONFIG["repository"])
        return c, completion

    def test_each_post_and_readback_boundary_recovers_only_a_complete_upload(self):
        """Partial nonces stay unused; final-entry loss recovers without current-budget packing."""
        response = {"summary": "large " * 2500}
        for action in ("POST", "GET"):
            for boundary in (1, 2, 3):
                with self.subTest(action=action, boundary=boundary):
                    self.setUp()
                    c, completion = self.prepared(response)
                    store = c.checkpoints.reports
                    store.completion = completion
                    original = self.gh.repo
                    calls = []

                    def request(path, method="GET", data=None):
                        """Crash after the selected immutable part has been stored or verified."""
                        result = original(path, method, data)
                        if method == action and "/artifacts" in path:
                            calls.append(path)
                            if len(calls) == boundary:
                                raise Crash()
                        return result

                    # Two continuations, then entry; completion metadata is included in packing.
                    with fixed_parts(7000), patch.object(self.gh, "repo", side_effect=request):
                        with self.assertRaises(Crash):
                            store.put(response)
                    old = deepcopy(self.gh.artifacts[7])
                    c = self.loaded()
                    with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 1000):
                        result = c.recover(c.state["operation"])
                    if boundary == 3:
                        self.assertEqual(result, response)
                        self.assertEqual(c.state["operation"]["timing"], completion[1]["timing"])
                    else:
                        self.assertIsNone(result)
                        # Available source can be freshly uploaded with entirely new boundaries.
                        fresh = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
                        with fixed_parts(9000), patch.object(fresh, "collection", side_effect=AssertionError("scan")):
                            key = fresh.put(response)
                        self.assertFalse(set(fresh.numbers[key]) & set(old))
                        for number, artifact in old.items():
                            self.assertEqual(self.gh.artifacts[7][number], artifact)

    def test_rejected_output_and_large_reconstruction_metadata_round_trip(self):
        """The one canonical record reconstructs exact rejected JSON without a second response."""
        value = {"summary": "payload " * 2000, "verdict": "invalid", "classification": "x" * 18000}
        raw = json.dumps(value, indent=2, ensure_ascii=True)
        response = {"output": raw, "reason": "schema", "attempt": 1, "truncated": False}
        c, completion = self.prepared(response)
        core, annotations, draft = projection(response)
        self.assertEqual(annotations, {"classification": "x" * 18000})
        self.assertIsNotNone(draft)
        store = c.checkpoints.reports
        store.completion = completion
        with fixed_parts(4000):
            key = store.put(core)
        self.assertGreater(len(store.numbers[key]), 8)
        c = self.loaded()
        self.assertEqual(c.recover(c.state["operation"]), response)

    def test_different_attempts_foreign_uploads_and_partial_sets_cannot_restore(self):
        """Only matching controller-authored attempt tags can complete current work."""
        response = {"summary": "large " * 4000}
        for fault in ("attempt", "contract", "foreign", "missing"):
            with self.subTest(fault=fault):
                self.setUp()
                c, completion = self.prepared(response)
                store = c.checkpoints.reports
                if fault == "attempt":
                    completion[1]["binding"]["operation"]["attempt"] = "other"
                elif fault == "contract":
                    completion[1]["binding"]["contract"] = "f" * 64
                store.completion = completion
                key = store.put(response)
                number = store.numbers[key][0]
                if fault == "foreign":
                    self.gh.artifacts[7][number]["artifact"]["creator"]["login"] = "foreign"
                elif fault == "missing":
                    del self.gh.artifacts[7][number]
                c = self.loaded()
                before = deepcopy(c.state)
                self.assertIsNone(c.recover(c.state["operation"]))
                self.assertEqual(c.state, before)

    def test_conflicting_attempt_results_and_malformed_metadata_fail_closed(self):
        """Conflicts and invalid completion records cannot silently trigger another model call."""
        for fault in ("result", "session", "response", "annotations", "edited"):
            with self.subTest(fault=fault):
                self.setUp()
                response = {"summary": "large " * 4000}
                c, completion = self.prepared(response)
                if fault == "session":
                    completion[1]["session"]["after"]["turn"] += 1
                elif fault == "response":
                    completion[1]["response"] = "f" * 64
                elif fault == "annotations":
                    completion[1]["annotations"] = {"classification": "modified"}
                store = c.checkpoints.reports
                store.completion = completion
                key = store.put(response)
                if fault == "edited":
                    self.gh.artifacts[7][store.numbers[key][0]]["versions"][0]["version"] = 2
                if fault == "result":
                    different = {"summary": "different " * 3000}
                    other = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
                    altered = deepcopy(completion[1])
                    altered["response"] = digest(different)
                    other.completion = (digest(different), altered)
                    other.put(different)
                c = self.loaded()
                before = deepcopy(c.state)
                with self.assertRaises(CheckpointError) as caught:
                    c.recover(c.state["operation"])
                self.assertEqual(caught.exception.code, "report_data")
                self.assertEqual(c.state, before)

    def test_identical_attempt_copies_select_lowest_entry(self):
        """Independent complete uploads of the same completion are interchangeable."""
        response = {"summary": "large " * 4000}
        c, completion = self.prepared(response)
        numbers = []
        for _ in range(2):
            store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
            store.completion = completion
            key = store.put(response)
            numbers.append(store.numbers[key])
        c = self.loaded()
        self.assertEqual(c.recover(c.state["operation"]), response)
        self.assertEqual(c.checkpoints.reports.numbers[key], numbers[0])

    def test_sessionless_completion_and_literal_rejected_output(self):
        """Fresh reviewers and noncanonical JSON restore without inventing a session."""
        for raw in ('{"x":1e5,"x":2}', "not json --> \\u003e"):
            with self.subTest(raw=raw):
                self.setUp()
                response = {"output": raw * 3000, "reason": "rejected", "attempt": 1, "truncated": False}
                c, _ = self.prepared(response)
                core, _, _ = projection(response)
                store = c.checkpoints.reports
                store.completion = attempt_result.capture(
                    c.state, c.state["operation"], response, None, CONFIG["repository"]
                )
                store.put(core)
                c = self.loaded()
                before = deepcopy(c.state["sessions"])
                self.assertEqual(c.recover(c.state["operation"]), response)
                self.assertEqual(c.state["sessions"], before)

    def test_legacy_publication_can_use_source_after_partial_new_upload(self):
        """A queued old writer can replace abandoned new parts when its source survived."""
        value = {"summary": "payload " * 3000}
        store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        original = self.gh.repo

        def interrupted(path, method="GET", data=None):
            """Keep one continuation without its creation reply."""
            result = original(path, method, data)
            if method == "POST":
                raise Crash()
            return result

        with fixed_parts(10000), patch.object(self.gh, "repo", side_effect=interrupted), self.assertRaises(Crash):
            store.put(value)
        partial = deepcopy(self.gh.artifacts[7])
        cold = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        key = cold.put(value, legacy=True)
        self.assertFalse(set(cold.numbers[key]) & set(partial))
        self.assertEqual(cold.get(key, cold.numbers[key]), value)
        for number, artifact in partial.items():
            self.assertEqual(self.gh.artifacts[7][number], artifact)

    def test_same_nonce_conflict_and_missing_pinned_reference_are_not_replaced(self):
        """Alternate complete copies cannot rescue a damaged referenced identity."""
        value = {"summary": "payload " * 2000}
        store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        key = store.put(value)
        number = store.numbers[key][0]
        item = self.gh.artifacts[7][number]["artifact"]
        copied = self.gh.repo("issues/7/artifacts", "POST", {k: item[k] for k in ("type", "name", "body")})
        reader = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        self.assertEqual(reader.get(key), value)
        self.assertEqual(reader.numbers[key], [number])
        self.gh.artifacts[7][copied["number"]]["versions"][0]["version"] = 2
        with self.assertRaises(ReportDataError):
            Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"]).get(key)
        del self.gh.artifacts[7][number]
        with self.assertRaises(ReportDataError):
            Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"]).get(key, [number])


class BatchAttemptTests(PipelineFixture, unittest.TestCase):
    """Recover independently charged members before any replacement roster admission."""

    def uploaded_batch(self):
        """Upload two results while losing both result checkpoints, retaining only reservations."""
        self.agents = BatchAgents()
        c = self.reach("plan_qc")
        adapter = Design(c)
        keys = adapter.open()
        prepared = adapter.reserve(keys, initial=True)
        original = c.checkpoints.write
        for key in keys:
            turn = prepared[key]
            response = c.agents.decode(turn, {})
            response["summary"] = key + "Independent evidence " * 800
            response["classification"] = "invalid rating is advisory"
            with (
                patch.object(c.agents, "decode", return_value=response),
                patch.object(c.checkpoints, "write", side_effect=Crash),
                self.assertRaises(Crash),
            ):
                adapter.complete(key, turn, {}, None)
        c.checkpoints.write = original
        return c, keys

    def test_batch_recovers_all_members_with_one_scan_and_no_new_admission(self):
        """Responses, raw session hashes and eligibility survive without another charge."""
        lost, keys = self.uploaded_batch()
        c = self.loaded()
        c.config["max_model_calls"] = c.state["calls"]
        before = deepcopy(c.state)
        with (
            patch.object(self.gh, "pages", wraps=self.gh.pages) as pages,
            patch.object(c.agents, "credential", side_effect=AssertionError("new credential")),
            patch.object(c.agents, "submit", side_effect=AssertionError("new call")),
        ):
            adapter = Design(c)
            self.assertEqual(adapter.open(), keys)
            self.assertTrue(all(adapter.retained(key) for key in keys))
            adapter.reserve([key for key in keys if adapter.needs_attempt(key)], initial=False)
        self.assertEqual(sum(call.args[0].endswith("/artifacts") for call in pages.call_args_list), 1)
        self.assertEqual(c.state["calls"], before["calls"])
        self.assertEqual(c.state["credentials"], before["credentials"])
        self.assertEqual(c.state["design_eligibility"], before["design_eligibility"])
        self.assertEqual(c.state["sessions"], lost.state["sessions"])
        for key in keys:
            member = c.state["review_batch"]["members"][key]
            self.assertEqual(member["operation"], lost.state["review_batch"]["members"][key]["operation"])
            self.assertEqual(member["status"], "complete")
        # The deliberately oversized report is retained, but cannot bypass semantic validation.
        with self.assertRaisesRegex(Blocked, "budget"):
            c.quality_control("plan_qc")
        self.assertFalse(c.state["design_votes"])
        self.assertTrue(any(m.get("review_repair") for m in c.state["review_batch"]["members"].values()))

    def test_changed_batch_binding_drops_stale_members_without_scanning(self):
        """A new generation cannot recover an old generation's uploads."""
        self.uploaded_batch()
        c = self.loaded()
        c.state["design_generation"] += 1
        with patch.object(c.checkpoints.reports, "collection", side_effect=AssertionError("scan")):
            adapter = Design(c)
            keys = adapter.open()
        self.assertTrue(all(not adapter.retained(key) for key in keys))

    def test_live_handles_never_trigger_recovery_discovery(self):
        """Normal batch dispatch retains results without listing old artifacts."""
        self.agents = BatchAgents()
        c = self.reach("plan_qc")
        with patch.object(c.checkpoints.reports, "collection", side_effect=AssertionError("scan")):
            c.quality_control("plan_qc")
        self.assertEqual(len(c.state["design_votes"]), 2)

    def test_rotated_plan_review_session_rejects_upload_before_budget_check(self):
        """The serial plan-review prepare still runs before attempting artifact restoration."""
        fixture = AttemptArtifactsTests()
        fixture.setUp()
        fixture.lost_response_checkpoint("plan_qc")
        c = fixture.loaded()
        member = design_roster(c.config)[0]
        provider, context = c.quality_context("plan_qc", member)
        c.config["max_model_calls"] = c.state["calls"]
        original = sessions.prepare

        def rotated(state, config, provider, stage, context):
            """Simulate a scope rotation at the existing preparation boundary."""
            session = original(state, config, provider, stage, context)
            sessions.restart(state, session)
            return {**state["sessions"][session["role"]], "role": session["role"]}

        with patch.object(sessions, "prepare", side_effect=rotated), self.assertRaisesRegex(Blocked, "budget"):
            c.ask(provider, "plan_qc", context)
        self.assertNotIn("response", c.state["operation"])
        self.assertTrue(any(s["restarts"] for s in c.state["sessions"].values()))


class TrafficAcceptanceTests(unittest.TestCase):
    """Keep both issue sizes and retention saves within the measured phase-two request cost."""

    def test_cold_call_cost_at_both_fixture_sizes(self):
        """Every call has zero artifact collection GETs and stays below its baseline."""
        for unrelated, baseline_gets in ((5, (90, 105)), (150, (175, 190))):
            for large in (False, True):
                with self.subTest(unrelated=unrelated, detached=large):
                    measured = measure(unrelated, large)
                    self.assertEqual(measured["calls"][0]["existing_artifacts"], unrelated + 5)
                    self.assertEqual(measured["total"]["collection_GET"], 0)
                    self.assertEqual(measured["total"]["POST"], 6)
                    self.assertEqual(measured["total"]["PATCH"], 24)
                    self.assertLessEqual(measured["total"]["GET"], baseline_gets[large])
                    baseline = [15] * 6 if unrelated == 5 else [29, 29, 29, 29, 29, 30]
                    for index, row in enumerate(measured["calls"]):
                        self.assertEqual(row["retention_save"]["collection_GET"], 0)
                        self.assertEqual(row["whole_call_storage"]["collection_GET"], 0)
                        self.assertLessEqual(
                            row["whole_call_storage"]["GET"], baseline[index] + (index if large else 0)
                        )
                    self.assertEqual(measured["harness_calls"], 0)

    def test_nested_history_and_overflow_preflight_do_not_scan(self):
        """Derived writes run before publication, without adding intent checkpoints or scans."""
        gh = FakeGitHub(CONFIG)
        cp = Checkpoints(gh, CONFIG, 7)
        state = {
            "version": 2,
            "issue": 7,
            "stage": "stopped",
            "calls": 0,
            "padding": "overflow " * 10000,
            "review_history": [{"note": str(i) + "x" * 1000} for i in range(24)],
        }
        with patch.object(cp.reports, "collection", side_effect=AssertionError("scan")):
            cp.preflight([state])
            cp.save(state)
        self.assertEqual(Checkpoints(gh, CONFIG, 7).load([]), state)
