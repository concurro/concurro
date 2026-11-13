"""Credential events through actual Registry, scheduler, Worker, Controller and Exchange."""

import io
import json
import os
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import test_sponsored_admissions as admission_fixture

# isort: split
from exchange_fixtures import BINDING, EXPECTED
from fixtures import KEY, target_files
from vault_fixtures import FakeTransport

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import vault
from agents import Agents, RunnerExecutor
from github import NotFound, Transient
from policy import digest
from service.errors import RegistryFailure
from service.shadow import Metadata
from service.webhook_recovery import Recovery as WebhookRecovery
from test_registry_setup import ConnectionTrace
from test_shadow import comment
from test_vault_resolver import GATEWAY, SERVICE, listeners
from test_webhook_recovery import DeliveryAPI


class Feed:
    """Nonsecret lifecycle input with explicit replay/order control and no secret-resolution capability."""

    def __init__(self, records):
        """Seed the real preexisting metadata fixture's currently validated versions."""
        self.rows = []
        for record in records:
            self.add(record)

    def add(self, record, kind="validated"):
        """Append one synthetic immutable lifecycle identity, never model response text or tokens."""
        value = {k: record[k] for k in ("user_id", "route", "record_id", "version", "status")}
        value.update(
            sequence=len(self.rows) + 1, type=kind, id=uuid.uuid4().hex, reason="fixture", at="2026-11-12T10:00:00Z"
        )
        self.rows.append(value)
        return value

    def events(self, after):
        """Return normal cursor pages; tests may replay rows directly at Registry's atomic seam."""
        rows = deepcopy([r for r in self.rows if r["sequence"] > after])
        return {"events": rows, "next": rows[-1]["sequence"] if rows else after, "more": False}


class RecoveryTests(admission_fixture.AdmissionTests):
    """Reuse established source/grant fixtures, but dispatch actual admitted Exchange turns."""

    def setUp(self):
        """Configure contributed funding and actual RunnerExecutor without any live transport."""
        super().setUp()
        self.config["funding_mode"] = "contributed"
        self.backend.files = target_files(self.config)
        self.fake.comments += [
            comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"),
            comment(3, "/concurro sponsor anthropic", "bob", "2026-11-12T10:00:01Z"),
        ]
        self.feed = Feed(self.metadata)
        self.shadow.metadata.events = self.feed.events
        self.service()
        self.worker.sponsorship = self.shadow
        self.worker.agents = Agents
        self.worker.make_executor = self.executor
        self.served, self.faults = [], [("auth", None)]
        self.shadow.poll_credentials()

    def executor(self, repository, github):
        """Bind one ordinary Worker generation to the real Exchange and its existing exact grant adapter."""
        task = {k: v for k, v in BINDING.items() if k not in ("role", "base", "phase")}
        task.update(repository=KEY[0], issue=KEY[1], upstream=KEY[0], fork=KEY[0], generation=github.generation)
        binding = self.registry.binding(KEY)
        task["config_sha"] = binding.config_sha if binding else BINDING["config_sha"]
        expected = {**EXPECTED, "actor_id": "3003"}

        def dispatch(session, labels):
            """Produce a trusted fixture observation through real key/next/request-start/completion paths."""
            token = json.dumps(
                {
                    **expected,
                    "ref": "refs/heads/concurro/runs/" + session,
                    "runner_environment": "github-hosted",
                    "run_id": "77",
                    "run_attempt": "1",
                    "jti": uuid.uuid4().hex,
                    "exp": self.clock() + 300,
                }
            )
            grant = self.exchange.key(session, token, engine=task["engine"])
            turn = self.exchange.next(session, grant["token"], 1, 0)["turns"][0]
            request = turn["payload"]["request"]
            self.served.append((session, turn))
            if self.faults:
                category, hint = self.faults.pop(0)
                nonce = self.exchange.inference.start(grant["key"], "POST", "/v1/responses")["request"]
                self.exchange.inference.finish(
                    nonce,
                    "provider",
                    {
                        "category": category,
                        "status": 429 if category == "rate_limited" else 401,
                        "usage": None,
                        "completed": False,
                        "retry_after": hint,
                    },
                )
                result = {"outcome": "failed", "error": "synthetic provider failure"}
            else:
                answer = self.agents.ask(request["provider"], request["stage"], request["context"])
                result = {"outcome": "ok", "response": answer}
            self.exchange.complete(session, grant["token"], turn["id"], result)

        return RunnerExecutor(
            admission_fixture.LocalOperator(self.exchange),
            "https://fixture.invalid",
            expected,
            ["*"],
            1000,
            dispatch,
            task=task,
            check_owner=lambda: self.gateway.check_owner(KEY, github.generation),
        )

    def dispatch(self):
        """Run the actual queue dispatcher while replacing only OIDC signature verification with fixture claims."""
        with patch("exchange.claims", side_effect=lambda token, *args: json.loads(token)):
            return self.scheduler.dispatch()

    def pause(self):
        """Start normally and retain the real trusted-failure checkpoint after one charged attempt."""
        self.registry.enqueue(KEY, "fixture-start", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["stage"], "awaiting_credentials")
        self.assertEqual(state["calls"], 1)
        self.assertEqual(self.registry.due(self.clock()), [])
        return state

    def validate(self, actor="202", route="openai-api-key", version=2):
        """Publish a replacement's metadata/event as the existing real vault would, without inference."""
        row = next(r for r in self.metadata if r["user_id"] == actor and r["route"] == route)
        row.update(version=version, record_id=digest([actor, route, version]), status="valid")
        return self.feed.add(row)

    def test_auth_requires_new_version_and_duplicate_validation_never_retries_same_failed_version(self):
        """One new usable version authorizes one automatic paid attempt, not a loop on old validation."""
        original = self.pause()
        self.validate(version=1)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])
        self.faults = [("auth", None)]
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        failed = self.state()
        self.assertEqual(failed["stage"], "awaiting_credentials")
        self.assertEqual(failed["calls"], 2)
        self.assertEqual(failed["sponsorships"], original["sponsorships"])
        self.assertEqual(failed["operation"]["admission"]["credential"]["version"], 2)
        self.validate(version=2)
        self.shadow.poll_credentials()
        self.registry.credential_events(
            {"events": list(reversed(self.feed.rows)), "next": len(self.feed.rows), "more": False}
        )
        self.assertEqual(self.dispatch(), [])
        self.assertEqual(self.state()["calls"], 2)

    def test_validation_before_pause_is_not_lost_and_success_preserves_admitted_history(self):
        """A newer validated token arriving during an old turn queues after the owner releases."""
        original = self.exchange.inference.finish

        def replace_before_failure(*args, **kwargs):
            """Publish v2 while v1's actual running turn is about to report trusted auth."""
            self.validate()
            self.shadow.poll_credentials()
            return original(*args, **kwargs)

        self.registry.enqueue(KEY, "fixture-start", self.clock())
        with patch.object(self.exchange.inference, "finish", side_effect=replace_before_failure):
            self.assertEqual(self.dispatch(), [KEY])
        failed = self.state()
        self.assertEqual(failed["calls"], 1)
        self.assertEqual([i.key for i in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["stage"], "plan")
        self.assertEqual(state["sponsorships"], failed["sponsorships"])
        rows = self.exchange.db.execute("SELECT binding FROM turns ORDER BY created").fetchall()
        self.assertEqual([json.loads(r[0])["credential_version"] for r in rows], [1, 2])

    def test_rate_deadline_survives_duplicates_restart_and_does_not_resume_before_due(self):
        """Trusted numeric retry hints may reuse a version only after the original durable due time."""
        self.faults = [("rate_limited", 30)]
        self.pause()
        due = self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0]
        self.registry.credential_events({"events": self.feed.rows, "next": len(self.feed.rows), "more": False})
        self.registry.recover()
        self.assertEqual(
            self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0], due
        )
        self.clock.now = due - 1
        self.assertEqual(self.dispatch(), [])
        self.clock.now = due
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)
        self.assertEqual(self.state()["operation"]["admission"]["credential"]["version"], 1)

    def test_stop_consumes_pending_wake_and_new_credentials_never_restart_it(self):
        """A writer stop wins before the automatic hook and remains stopped after replacement events."""
        self.faults = [("rate_limited", 30)]
        self.pause()
        self.fake.comments.append(comment(99, "/concurro stop"))
        self.reconciler.observe(KEY, cached=False)
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["stage"], "stopped")
        self.validate()
        self.shadow.poll_credentials()
        self.clock.now += 60
        self.assertEqual(self.dispatch(), [])
        self.assertEqual(self.state()["calls"], 1)

    def reopen_registry(self):
        """Reopen actual durable queue/cursor state and reconnect the existing ownership adapters."""
        self.registry.close()
        self.registry = self.open()
        for component in (self.gateway, self.shadow, self.worker, self.reconciler, self.scheduler):
            component.registry = self.registry

    def test_unrelated_event_and_missing_peer_coverage_do_not_create_a_paid_retry(self):
        """Readiness aggregates all required provider slots and does not borrow an unrelated owner's token."""
        self.pause()
        self.validate(actor="101")
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])
        peer = next(r for r in self.metadata if r["user_id"] == "303" and r["route"] == "anthropic-api-key")
        peer["status"] = "revoked"
        self.feed.add(peer, "revoked")
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])
        self.validate(actor="303", route="anthropic-api-key")
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_current_model_coverage_is_rechecked_before_latch_or_charge(self):
        """A validated but wrong model set consumes only its wake receipt and leaves the credential latch intact."""
        self.pause()
        self.validate()
        current = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
        wanted = current["models"]
        current["models"] = ["unselected-model"]
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 1)
        self.assertTrue(state["provider_pause_pending"])
        self.assertEqual(state["stage"], "awaiting_credentials")
        self.assertEqual(self.dispatch(), [])
        with self.current() as controller, patch.object(controller, "save", wraps=controller.save) as saved:
            self.shadow.recover(controller, self.fake.comments)
            self.assertEqual(controller.state["credential_recovery"]["status"], "coverage still unavailable")
            saved.assert_not_called()
        current["models"] = wanted
        self.validate(version=3)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_permission_loss_and_withdrawal_remain_scoped_and_cannot_be_restored_by_events(self):
        """Fresh source authorization retires only the lost owner's grant before a queued retry can charge."""
        original = self.pause()
        self.clock.now += 120
        self.allowed.remove("alice")
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 1)
        self.assertIsNone(state["sponsorships"]["openai"].get("sponsor_id"))
        self.assertEqual(state["sponsorships"]["anthropic"], original["sponsorships"]["anthropic"])
        self.allowed.add("alice")
        self.validate(version=3)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])

    def test_no_hint_unknown_budget_and_global_budget_are_not_automatic_recovery(self):
        """Only the accepted failure classes with real retry evidence can generate new paid work."""
        for category, hint, limit in (
            ("rate_limited", None, 20),
            ("unknown", None, 20),
            ("budget_denied", None, 20),
            ("auth", None, 1),
        ):
            # Use separate real fixture state for each independent terminal/paused category.
            with self.subTest(category=category, limit=limit):
                case = RecoveryTests(
                    "test_auth_requires_new_version_and_duplicate_validation_never_retries_same_failed_version"
                )
                case.setUp()
                try:
                    case.config["max_model_calls"] = limit
                    case.backend.files = target_files(case.config)
                    case.faults = [(category, hint)]
                    case.registry.enqueue(KEY, "fixture-start", case.clock())
                    case.dispatch()
                    before = case.state()["calls"]
                    case.validate()
                    case.shadow.poll_credentials()
                    self.assertEqual(case.dispatch(), [])
                    self.assertEqual(case.state()["calls"], before)
                finally:
                    case.doCleanups()

    def test_full_restart_keeps_the_original_rate_deadline(self):
        """Closing and reopening SQLite cannot reset a queued provider backoff or recharge before it."""
        self.faults = [("rate_limited", 90)]
        self.pause()
        due = self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0]
        self.reopen_registry()
        self.shadow.poll_credentials()
        self.assertEqual(
            self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0], due
        )
        self.clock.now = due - 1
        self.assertEqual(self.dispatch(), [])
        self.clock.now = due
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_cursor_and_queued_wake_are_atomic_on_storage_failure(self):
        """A cursor write failure cannot acknowledge the event while losing its corresponding task wake."""
        self.pause()
        after = self.registry.credential_cursor()
        self.validate()

        class FailCursor(ConnectionTrace):
            """Reuse the SQL trace fixture, failing only the final event acknowledgment statement."""

            def execute(self, sql, parameters=()):
                """Fail after version/projection/queue writes but before their shared commit."""
                if sql.startswith("INSERT INTO credential_cursor"):
                    self.failure_index = len(self.trace)
                return super().execute(sql, parameters)

        self.registry.db = FailCursor(self.registry.db, [])
        with self.assertRaises(RegistryFailure):
            self.shadow.poll_credentials()
        self.reopen_registry()
        self.assertEqual(self.registry.credential_cursor(), after)
        self.assertEqual(self.registry.due(self.clock()), [])
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_forward_migration_inspects_old_pause_once_without_inventing_fresh_validation(self):
        """Schema7 paused tasks receive one owned reconciliation; old same-version auth still cannot retry."""
        self.pause()
        # Model only pre-phase6 additive state: checkpoint/projection have no recovery receipt.
        with self.current() as controller:
            controller.state.pop("credential_recovery", None)
            controller.save()
            self.registry.record_shadow(KEY, controller.state, funding="contributed")
        with self.registry.transaction() as db:
            db.execute("DROP TABLE credential_cursor")
            db.execute("DROP TABLE credential_versions")
            db.execute("UPDATE meta SET value='7' WHERE name='schema'")
        self.reopen_registry()
        self.assertEqual([i.key for i in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 1)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])
        self.reopen_registry()
        self.assertEqual(self.dispatch(), [])
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_wrapping_rotation_keeps_valid_candidate_evidence_without_granting_another_retry(self):
        """A rewrap neither erases v2 validation nor makes a failed v2 into a newly validated token."""
        self.pause()
        self.faults = [("auth", None)]
        self.validate()
        row = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
        self.feed.add(row, "rewrapped")
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)
        self.feed.add(row, "rewrapped")
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])

    def test_two_provider_failures_wait_for_both_new_versions_and_preserve_review_work(self):
        """A paused review roster cannot clear its latch after only one of two failed routes is replaced."""
        self.faults = []
        self.registry.enqueue(KEY, "fixture-start", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["stage"], "plan")
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["stage"], "plan_qc")
        self.faults = [("auth", None), ("auth", None)]
        self.assertEqual(self.dispatch(), [KEY])
        paused = self.state()
        self.assertEqual(paused["stage"], "awaiting_credentials")
        self.assertEqual(len(paused["credential_recovery"]["failures"]), 2)
        spent = paused["calls"]
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])
        self.assertTrue(self.state()["provider_pause_pending"])
        self.validate(actor="303", route="anthropic-api-key")
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        recovered = self.state()
        self.assertEqual(recovered["stage"], "planned")
        self.assertEqual(recovered["calls"], spent + 2)
        self.assertEqual(recovered["sponsorships"], paused["sponsorships"])
        self.assertEqual(recovered["plan"], paused["plan"])
        self.assertEqual(recovered["sessions"].keys(), paused["sessions"].keys())
        self.validate(version=3)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])  # A human approval wait is never a recovery target.

    def test_stale_metadata_after_validation_event_waits_without_clearing_auth_latch(self):
        """A newer feed event cannot make an older metadata reply retry the already-failed version."""
        self.pause()
        current = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
        old = deepcopy(current)
        self.validate()
        new = deepcopy(current)
        current.update(old)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 1)
        self.assertTrue(self.state()["provider_pause_pending"])
        current.update(new)
        self.clock.now += 60
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_other_provider_transfer_does_not_reset_recorded_rate_deadline(self):
        """Reconciliation of a peer grant changes its receipt but never restarts the failing turn's clock."""
        self.faults = [("rate_limited", 30)]
        self.pause()
        due = self.state()["credential_recovery"]["not_before"]
        self.clock.now += 10
        self.fake.comments.append(comment(99, "/concurro sponsor anthropic", "writer", "2026-11-12T10:00:20Z"))
        self.reconciler.observe(KEY, cached=False)
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 1)
        self.assertEqual(self.state()["credential_recovery"]["not_before"], due)
        self.assertEqual(
            self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0], due
        )

    def test_new_validation_restart_and_early_inspection_preserve_transient_backoff(self):
        """Credential events cannot shorten the existing service retry deadline or turn it into a paid retry."""
        self.pause()
        current = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
        original = deepcopy(current)
        self.validate()
        current.update(original)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        due = self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0]
        self.clock.now += 1
        self.validate(version=3)
        self.shadow.poll_credentials()
        self.assertEqual(
            self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0], due
        )
        self.reopen_registry()
        self.registry.enqueue(KEY, "fixture-early-inspection", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 1)
        self.assertEqual(
            self.registry.db.execute("SELECT due_at FROM queue WHERE repository=? AND issue=?", KEY).fetchone()[0], due
        )
        self.clock.now = due
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)

    def test_service_event_client_rejects_incomplete_pages_and_allowlists_fields(self):
        """Malformed feed/cursor evidence is transient, while secret-shaped extra fields never persist."""
        client = Metadata("http://vault.invalid", "unused-fixture-path")
        row = {**self.feed.rows[0], "secret": "synthetic-event-secret"}
        valid = {"events": [row], "next": 1, "more": False}
        for payload in (
            {"events": "secret", "next": 1, "more": False},
            {**valid, "next": 2},
            {**valid, "more": "yes"},
            {**valid, "events": [{**row, "version": 0}]},
            {"events": [], "next": 0, "more": True},
        ):
            with patch.object(client, "get", return_value=payload), self.assertRaises(Transient):
                client.events(0)
        body = io.BytesIO(json.dumps(valid).encode())
        with (
            patch("service.shadow.private.read", return_value=b"a" * 64),
            patch("service.shadow.urllib.request.urlopen", return_value=body) as opened,
        ):
            result = client.events(0)
        self.assertNotIn("synthetic-event-secret", str(result))
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://vault.invalid/metadata/events?after=0")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "a" * 64)

    def test_incomplete_historical_failure_identity_never_authorizes_a_paid_retry(self):
        """A legacy/unknown binding cannot turn old validation into permission to retry a failed token."""
        self.pause()
        with self.current() as controller:
            controller.state.pop("credential_recovery", None)
            controller.state["provider_failure"]["binding"] = {}
            controller.save()
            self.registry.record_shadow(KEY, controller.state, funding="contributed")
        self.registry.enqueue(KEY, "fixture-inspect", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        self.assertFalse(self.state()["credential_recovery"]["eligible"])
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])
        self.assertEqual(self.state()["calls"], 1)

    def test_precharge_missing_credential_recovers_without_a_phantom_attempt(self):
        """Prior explicit sponsorship stays first even when its first token is missing before any debit."""
        record = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
        record["status"] = "revoked"
        self.feed.add(record, "revoked")
        self.shadow.poll_credentials()
        self.faults = []
        self.registry.enqueue(KEY, "fixture-start", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        paused = self.state()
        self.assertEqual(paused["stage"], "awaiting_credentials")
        self.assertEqual(paused["calls"], 0)
        self.assertEqual(self.served, [])
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 1)
        self.assertEqual(self.state()["sponsorships"], paused["sponsorships"])
        self.assertEqual(len(self.served), 1)

    def test_explicit_withdrawal_tombstone_wins_over_queued_validation(self):
        """Token events cannot silently reassign a withdrawn provider or alter its peer grant."""
        paused = self.pause()
        self.validate()
        self.shadow.poll_credentials()
        self.fake.comments.append(comment(99, "/concurro withdraw openai", "alice", "2026-11-12T10:00:20Z"))
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 1)
        self.assertIsNone(state["sponsorships"]["openai"].get("sponsor_id"))
        self.assertEqual(state["sponsorships"]["anthropic"], paused["sponsorships"]["anthropic"])
        self.validate(version=3)
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [])

    def test_explicit_transfer_keeps_historical_charges_with_their_actual_owners(self):
        """A consenting new owner may recover future work while prior account/version debits remain unchanged."""
        self.pause()
        self.validate()
        self.faults = [("auth", None)]
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.fake.comments.append(comment(99, "/concurro sponsor openai", "bob", "2026-11-12T10:00:20Z"))
        self.reconciler.observe(KEY, cached=False)
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 3)
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "303")
        projection = self.shadow.view(KEY)
        charged = [r for r in projection["charged_calls"] if r["provider"] == "openai"]
        self.assertEqual(
            sorted((r["sponsor"], r["version"], r["calls"]) for r in charged),
            [("202", 1, 1), ("202", 2, 1), ("303", 1, 1)],
        )
        self.assertEqual(projection["calls"], 3)
        synthetic = deepcopy(state)
        next(iter(synthetic["credentials"].values()))["unexpected_secret"] = "synthetic-no-export"
        self.registry.record_shadow(KEY, synthetic, funding="contributed")
        self.assertNotIn("synthetic-no-export", str(self.shadow.view(KEY)))

    def test_new_precharge_pause_keeps_empty_failure_snapshot(self):
        """A later precharge intent is distinct from the trusted failure already acknowledged by a writer."""
        self.pause()
        peer = next(r for r in self.metadata if r["user_id"] == "303" and r["route"] == "anthropic-api-key")
        peer["status"] = "revoked"
        self.fake.comments.append(comment(99, "/concurro retry", "alice", "2026-11-12T10:00:20Z"))
        self.registry.enqueue(KEY, "fixture-writer-retry", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["stage"], "awaiting_credentials")
        self.assertEqual(state["calls"], 1)
        self.assertEqual(state["credential_recovery"]["failures"], [])
        self.registry.enqueue(KEY, "fixture-reinspection", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["credential_recovery"]["failures"], [])

        self.validate(actor="303", route="anthropic-api-key")
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)
        self.assertEqual(self.state()["operation"]["admission"]["credential"]["version"], 1)

    def test_healthy_webhook_timer_reads_real_replacement_feed_and_schedules_once(self):
        """Normal delivery scans consume actual retired+validated pages before a real task can charge."""
        store = vault.Store(Path(self.directory.name) / "recovery-vault.db", vault.Envelope(os.urandom(32)), self.clock)
        self.addCleanup(store.close)
        for row in self.metadata:
            store.put(vault.User(int(row["user_id"]), "synthetic"), row["route"], "synthetic-token", row["models"])
        with self.registry.transaction() as db:
            db.execute("DELETE FROM credential_versions")
            db.execute("DELETE FROM credential_cursor")
        transport = FakeTransport()
        public = vault.create_app(store, vault.Identity(transport), vault.Validator(transport), SERVICE, (GATEWAY,))
        token = Path(self.directory.name) / "metadata-token"
        token.write_text(SERVICE)
        token.chmod(0o600)
        with listeners(public, vault.create_resolver(store, GATEWAY)) as (url, _):
            self.shadow.metadata = Metadata(url, token)
            self.reconciler.policy_observer = self.worker.observe_policies
            recovery = WebhookRecovery(DeliveryAPI(), self.registry, self.reconciler, {KEY[0]: 42}, self.clock)
            self.registry.webhook_check(recovery.scope, self.clock())
            with patch.object(
                self, "read_metadata", side_effect=lambda actor, at=None: store.metadata(int(actor), at)["credentials"]
            ):
                recovery.poll()
                self.pause()
                prior = self.registry.credential_cursor()
                row = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
                entry = store.put(vault.User(202, "alice"), row["route"], "synthetic-replacement", row["models"])
                page = store.events(prior)
                self.assertEqual(
                    [(e["type"], e["status"]) for e in page["events"]], [("retired", "retired"), ("validated", "valid")]
                )
                self.assertEqual(self.registry.due(self.clock()), [])
                self.clock.now += 300
                recovery.poll()
                self.assertEqual(self.registry.credential_cursor(), page["next"])
                cached = json.loads(
                    self.registry.db.execute(
                        "SELECT document FROM credential_versions WHERE user_id='202' AND route='openai-api-key'"
                    ).fetchone()[0]
                )
                self.assertEqual((cached["version"], cached["validation_sequence"]), (entry["version"], page["next"]))
                self.assertEqual([i.key for i in self.registry.due(self.clock())], [KEY])
                self.assertEqual(self.dispatch(), [KEY])
                self.assertEqual(self.state()["calls"], 2)
                self.assertEqual(self.state()["operation"]["admission"]["credential"]["version"], 2)
                recovery.poll()
                self.assertFalse(
                    any(reason == "credential-recovery" for i in self.registry.due(self.clock()) for reason in i.causes)
                )

    def test_three_terminal_provider_failures_do_not_create_uncertainty(self):
        """Known failures keep attempt identity and charges without inventing uncertain outcomes."""
        self.faults = [("auth", None)] * 3
        first = self.pause()
        for version in (2, 3, 4):
            self.validate(version=version)
            self.shadow.poll_credentials()
            self.assertEqual(self.dispatch(), [KEY])
            state = self.state()
            self.assertEqual(state["calls"], version)
            self.assertEqual(state["operation"]["id"], first["operation"]["id"])
            self.assertEqual(state["operation"]["attempts"], version)
            self.assertEqual(state["operation"]["uncertain"], 0)
        self.assertEqual(state["stage"], "plan")

    def test_preexisting_uncertainty_exhaustion_still_requires_writer_without_extra_charge(self):
        """Automatic recovery cannot erase an unrelated prior uncertainty allowance."""
        self.pause()
        with self.current() as controller:
            controller.state["operation"]["uncertain"] = 2
            controller.save()
        self.validate()
        self.shadow.poll_credentials()
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["stage"], "blocked")
        self.assertEqual(state["calls"], 1)
        self.assertEqual(state["operation"]["uncertain"], 2)
        self.fake.comments.append(comment(99, "/concurro retry", "alice", "2026-11-12T10:00:20Z"))
        self.registry.enqueue(KEY, "fixture-writer-retry", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)
        self.assertEqual(self.state()["stage"], "plan")

    def test_missing_target_cannot_disable_healthy_delivery_or_credential_poll(self):
        """Expected target lookup failure is isolated, while arbitrary policy failures remain visible."""
        self.pause()
        self.validate()
        self.reconciler.policy_observer = self.worker.observe_policies
        api = DeliveryAPI()
        recovery = WebhookRecovery(api, self.registry, self.reconciler, {KEY[0]: 42}, self.clock)
        self.registry.webhook_check(recovery.scope, self.clock())
        self.clock.now += 300
        with patch.object(self.worker, "load", side_effect=NotFound("synthetic removed fork")):
            recovery.poll()
        self.assertEqual(api.requests, [("GET", "app/hook/deliveries?per_page=100")])
        self.assertEqual(self.registry.credential_cursor(), len(self.feed.rows))
        self.assertEqual([i.key for i in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.dispatch(), [KEY])
        self.assertEqual(self.state()["calls"], 2)
        with patch.object(self.worker, "load", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(RuntimeError):
                self.worker.observe_policies()


def load_tests(loader, tests, pattern):
    """Run these production-path recovery scenarios without duplicating inherited fixture suites."""
    return unittest.TestSuite(RecoveryTests(name) for name in RecoveryTests.__dict__ if name.startswith("test_"))
