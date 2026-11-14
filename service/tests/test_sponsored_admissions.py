"""Phase3 payer selection through existing Controller, Registry and Exchange adapters."""

import json
import unittest
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import PropertyMock, patch

import test_shadow as shadow_fixture  # noqa: F401 -- initializes service and gateway test paths

# isort: split
from exchange_fixtures import BINDING, EXPECTED
from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import admissions
from agents import Agents, RunnerExecutor
from controller import config_digest
from design_batch import Design
from exchange import Refused
from github import Transient
from policy import Blocked, ProviderFailure, SessionFailure, design_roster, digest
from relay import ExchangeError
from service import failures
from test_exchange_funding import FundedGateway, org_turn
from test_shadow import ACTORS, AT, ShadowTests, comment


class LocalOperator:
    """Use the real Exchange directly behind the operator client's positional signature."""

    def __init__(self, exchange):
        """Keep only the isolated temporary Exchange, never a live endpoint."""
        self.exchange = exchange

    def submit(self, turn, expected, models, budget, lease, payload, pin, binding):
        """Preserve the real request and admission identity across the operator seam."""
        return self.exchange.submit(turn, expected, models, budget, lease, payload, pin, binding=binding)

    def sponsorship(self, request):
        """Translate the real private refusal envelope exactly as the HTTP client does."""
        try:
            return self.exchange.sponsorship(request)
        except Refused as error:
            raise ExchangeError(error.status, str(error), error.code, error.transition) from None

    def __getattr__(self, name):
        """Reuse existing poll/cancel/end implementations without another lifecycle."""
        return getattr(self.exchange, name)


class AdmissionTests(ShadowTests):
    """Reuse full real source/checkpoint fixture; run only this class's new scenarios."""

    def setUp(self):
        """Create versioned metadata for independently consenting synthetic accounts."""
        super().setUp()
        self.agents.generation = 1
        self.clock.now = datetime.fromisoformat(AT.replace("Z", "+00:00")).timestamp() - 10
        self.exchange.gateway = FundedGateway()
        self.exchange.gateway.contributed_routes = ("openai-api-key", "anthropic-api-key")
        self.exchange.vault = self
        self.metadata = []
        for actor in ACTORS.values():
            for provider, harness in (("openai", "codex"), ("anthropic", "claude")):
                self.metadata.append(
                    {
                        "user_id": str(actor),
                        "route": provider + "-api-key",
                        "version": 1,
                        "record_id": ("a" if provider == "openai" else "b") * 64,
                        "kind": "api_key",
                        "status": "valid",
                        "models": [self.config["models"][harness], self.config["qc_model"]],
                        "expiry_known": False,
                        "expires_at": None,
                    }
                )

    def events(self, after):
        """No lifecycle changes are fabricated by this source/Controller integration fixture."""
        return {"events": [], "next": after, "more": False}

    def read(self, actor):
        """Expose the same current version evidence to Exchange and Controller."""
        return self.read_metadata(actor)

    @contextmanager
    def current(self, funded=True):
        """Reload one actual task under current ownership without advancing its stage."""
        generation = self.gateway.acquire(KEY, "phase3-test")
        self.assertIsNotNone(generation)
        controller = self.controller(generation)
        controller.issue = self.fake.issue
        controller.consumed_comments = self.fake.comments
        controller.load(self.fake.comments)
        self.shadow.begin(controller)
        self.shadow.sync(controller, self.fake.comments)
        if funded:
            controller.config = {**controller.config, "funding_mode": "contributed"}
            # This fixture explicitly requests personal funding; configuration alone no longer changes existing tasks.
            controller.state["provider_funding"] = {"openai": "contributed", "anthropic": "contributed"}
        controller.agents.generation = generation
        try:
            yield controller
        finally:
            self.gateway.release(KEY, generation, None)

    def sponsors(self):
        """Create explicit first-priority grants for different providers before enrollment."""
        self.fake.comments += [
            comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"),
            comment(3, "/concurro sponsor anthropic", "bob", "2026-11-12T10:00:01Z"),
        ]
        return self.advance()

    def test_mixed_funding_crosses_real_runner_exchange_and_keeps_separate_payers(self):
        """Actual Controller/Agents/Exchange turns preserve personal OpenAI and org Anthropic bindings."""
        self.serial_flow(mixed=True)

    def test_mixed_credential_recovery_ignores_org_personal_coverage(self):
        """Only personally funded routes enter credential replacement requirements."""
        from service import credential_recovery

        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        with self.current(funded=False) as controller:
            paused = {**controller.state, "stage": "awaiting_credentials", "retry_stage": "intake"}
            plan = credential_recovery.intent(paused, controller.config, self.clock())
            self.assertTrue(plan["eligible"])
            self.assertEqual({r["provider"] for r in plan["requirements"]}, {"openai"})
            self.assertEqual({r["sponsor"] for r in plan["requirements"]}, {"202"})
            self.assertEqual(controller.state["provider_funding"]["anthropic"], "org")

    def test_lost_funding_receipt_replays_without_changing_admitted_consent(self):
        """An accepted org selection with a lost response is adopted exactly once after reload."""
        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        with self.current(funded=False) as controller:
            before = deepcopy(controller.state["sponsorships"]["openai"])
            calls = controller.state["calls"]
            self.fake.comments.append(comment(9, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            original = self.exchange.sponsorship

            def lost(request):
                """Apply the genuine durable transition before losing its operator reply."""
                original(request)
                raise ExchangeError(503, "synthetic lost funding receipt")

            with patch.object(self.exchange, "sponsorship", side_effect=lost):
                self.shadow.begin(controller)
                self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["provider_funding"]["openai"], "contributed")
            self.assertIn("openai", controller.state["pending_sponsorships"])
            controller.load(self.fake.comments)
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["provider_funding"]["openai"], "org")
            self.assertEqual(controller.state["sponsorships"]["openai"]["epoch"], before["epoch"])
            self.assertEqual(controller.state["sponsorships"]["openai"]["grant_id"], before["grant_id"])
            self.assertEqual(controller.state["calls"], calls)
            self.assertEqual(len([h for h in controller.state["sponsorship_history"] if h["reason"] == "funding"]), 1)

    def test_pending_personal_funding_cannot_admit_stale_org_after_lost_reply(self):
        """Unacknowledged payer changes reconcile before admission across reloads and duplicate delivery."""
        self.sponsors()
        with self.current(funded=False) as controller:
            calls = controller.state["calls"]
            self.fake.comments.append(
                comment(22, "/concurro funding openai personal", "writer", "2026-11-12T10:02:00Z")
            )
            original = self.exchange.sponsorship

            def lost(request):
                """Keep returning an unavailable receipt after the genuine Exchange commit."""
                original(request)
                raise ExchangeError(503, "synthetic lost personal funding receipt")

            with patch.object(self.exchange, "sponsorship", side_effect=lost):
                self.shadow.begin(controller)
                self.shadow.sync(controller, self.fake.comments)
                self.assertEqual(controller.state["provider_funding"]["openai"], "org")
                self.assertIn("openai", controller.state["pending_sponsorships"])
                view = self.shadow.view(KEY)
                self.assertEqual(view["slots"]["openai"]["sponsor_id"], "202")
                self.assertEqual(view["source_slots"]["openai"]["sponsor_id"], "101")
                self.assertNotEqual(view["source_slots"]["openai"]["sponsor_id"], view["slots"]["openai"]["sponsor_id"])
                self.assertEqual(view["provider_funding"]["openai"], "org")
                self.assertEqual(view["source_provider_funding"]["openai"], "contributed")
                with self.assertRaises(Transient):
                    controller.select_admissions([("codex", "intake", {})])
                controller.load(self.fake.comments)
                self.shadow.begin(controller)
                self.shadow.sync(controller, self.fake.comments)
                with self.assertRaises(Transient):
                    controller.select_admissions([("codex", "intake", {})])
                self.assertEqual(controller.state["calls"], calls)
            selected = controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(selected[0]["funding"], "contributed")
            self.assertEqual(selected[0]["sponsor"], "101")
            epoch = selected[0]["epoch"]
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["epoch"], epoch)
            self.assertEqual(controller.state["calls"], calls)

    def test_late_withdrawal_cannot_cancel_newer_personal_funding_consent(self):
        """A renewed explicit personal selection remains active when an older withdrawal arrives late."""
        self.sponsors()
        with self.current(funded=False) as controller:
            self.fake.comments.append(comment(22, "/concurro funding openai personal", "alice", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            before = deepcopy(controller.state["sponsorships"]["openai"])
            self.fake.comments.append(comment(21, "/concurro withdraw openai", "alice", "2026-11-12T10:01:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["sponsorships"]["openai"], before)
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["sponsor"], "202")

    def test_legacy_and_repeated_nonowner_withdrawals_do_not_wedge_exchange(self):
        """Migrated slots without payer metadata accept identity-preserving withdrawal receipts."""
        self.sponsors()
        with self.current(funded=False) as controller:
            slot = controller.state["sponsorships"]["openai"]
            slot.pop("funding", None)
            slot.pop("funding_order", None)
            self.exchange.db.execute(
                "UPDATE sponsorship SET slot=? WHERE repository=? AND issue=? AND provider='openai'",
                (json.dumps(slot, sort_keys=True), *KEY),
            )
            controller.save()
            for identifier in (22, 23):
                self.fake.comments.append(
                    comment(identifier, "/concurro withdraw openai", "bob", f"2026-11-12T10:02:{identifier}Z")
                )
                self.shadow.begin(controller)
                self.shadow.sync(controller, self.fake.comments)
                self.assertFalse(controller.state.get("pending_sponsorships"))
                self.assertEqual(controller.state["sponsorships"]["openai"], slot)
            self.assertEqual(controller.state["provider_funding"]["openai"], "org")

    def test_withdraw_after_permission_loss_preserves_newer_funding_order(self):
        """Post-revocation self-withdrawal and late older payer sources cannot replace a newer org choice."""
        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        with self.current(funded=False) as controller:
            self.fake.comments.append(comment(22, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            order = controller.state["sponsorships"]["openai"]["funding_order"]
            self.allowed.remove("alice")
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["sponsorships"]["openai"]["funding_order"], order)
            self.fake.comments += [
                comment(23, "/concurro withdraw openai", "alice", "2026-11-12T10:03:00Z"),
                comment(21, "/concurro funding openai personal", "writer", "2026-11-12T10:01:00Z"),
            ]
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertFalse(controller.state.get("pending_sponsorships"))
            self.assertEqual(controller.state["provider_funding"]["openai"], "org")
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")

    def test_cutover_keeps_funding_order_and_accepts_later_withdrawal(self):
        """Retiring org-era implicit attribution retains independent payer ordering metadata."""
        self.advance()
        with self.current(funded=False) as controller:
            self.fake.comments.append(comment(22, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            order = controller.state["sponsorships"]["openai"]["funding_order"]
            self.shadow.cutover(controller, {"openai"})
            self.assertEqual(controller.state["sponsorships"]["openai"]["funding_order"], order)
            self.fake.comments.append(comment(23, "/concurro withdraw openai", "writer", "2026-11-12T10:03:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertFalse(controller.state.get("pending_sponsorships"))
            self.assertEqual(controller.state["provider_funding"]["openai"], "org")

    def test_delayed_personal_choice_keeps_newer_prospective_sponsor(self):
        """Payer and consent ordering converge independently when a newer sponsor was observed first."""
        self.sponsors()
        with self.current(funded=False) as controller:
            self.fake.comments.append(comment(22, "/concurro sponsor openai", "bob", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            epoch = controller.state["sponsorships"]["openai"]["epoch"]
            self.fake.comments.append(comment(21, "/concurro funding openai personal", "alice", "2026-11-12T10:01:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["provider_funding"]["openai"], "contributed")
            selected = controller.select_admissions([("codex", "intake", {})])[0]
            self.assertEqual(selected["sponsor"], "303")
            self.assertEqual(selected["epoch"], epoch)

    def test_org_selection_clears_stale_recovery_and_preserves_manual_pause(self):
        """Payer selection keeps lifecycle/budgets stable; a writer retry drains old personal sessions."""
        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        with self.current(funded=False) as controller:
            calls = controller.state["calls"]
            controller.state.update(
                stage="awaiting_credentials",
                retry_stage="intake",
                credential_recovery={"eligible": True, "id": "old", "not_before": self.clock()},
                provider_retire_sessions=["old-personal-session"],
            )
            self.fake.comments.append(comment(22, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.shadow.prepare_recovery(controller)
            self.assertNotIn("credential_recovery", controller.state)
            self.assertEqual(controller.state["stage"], "awaiting_credentials")
            self.assertEqual(controller.state["calls"], calls)
            with patch.object(self.shadow, "retire_pending") as retire:
                self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")
            retire.assert_called_once_with(controller)
            self.assertEqual(controller.state["calls"], calls)

    def test_org_only_intent_does_not_require_personal_initialization(self):
        """Org admission stays available when an org-only source has no usable starter boundary."""
        self.sponsors()
        with self.current(funded=False) as controller:
            controller.state.pop("funding_initialization")
            controller.state["funding_initialization_pending"] = {"actor_id": None, "at": AT}
            self.fake.comments.append(comment(22, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            controller.shadow_projection = self.shadow.confirm(
                KEY, self.fake.comments, [], controller.identities.author("state"), config=controller.config
            )
            calls = controller.state["calls"]
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")
            self.assertEqual(controller.state["calls"], calls)

    def test_rejected_historical_funding_is_history_without_pending_adoption(self):
        """A source suppressed by a durable authority cutoff cannot appear as actionable future funding."""
        import sponsorship

        self.sponsors()
        with self.current(funded=False) as controller:
            self.fake.comments.append(comment(22, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.clock.now += 300
            self.allowed.remove("alice")
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.allowed.add("alice")
            self.fake.comments.append(comment(23, "/concurro funding openai personal", "alice", "2026-11-12T10:03:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            view = self.shadow.view(KEY)
            rejected = next(fact for fact in view["facts"] if fact["source_id"] == 23)
            self.assertIn(rejected["grant_id"], controller.state["sponsorship_applied"])
            self.assertNotIn(rejected["grant_id"], controller.state["sponsorship_sources"].values())
            self.assertEqual(view["provider_funding"]["openai"], "org")
            self.assertEqual(view["source_provider_funding"], view["provider_funding"])
            self.assertEqual(view["source_slots"], view["slots"])
            source, slot = view["source_slots"]["openai"], view["slots"]["openai"]
            self.assertFalse(source.get("sponsor_id") and source.get("sponsor_id") != slot.get("sponsor_id"))
            own = sponsorship.own_consent(view, "202")
            self.assertTrue(any(fact["grant_id"] == rejected["grant_id"] for fact in own["facts"]))
            with patch.object(self.shadow, "preflight", side_effect=AssertionError("ignored funding forced preflight")):
                self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")

    def test_queued_funding_is_general_pending_without_promised_owner_or_payer(self):
        """A posted command invalidates source confirmation before any validated transition exists."""
        import delegation

        self.sponsors()
        with self.current(funded=False) as controller:
            body = delegation.render(
                self.shadow.key, KEY[0], KEY[1], "writer", 101, "/concurro funding openai personal", "f" * 16
            )
            self.registry.request_observation(
                KEY, comment={"id": 24, "body": body, "updated_at": "2026-11-12T10:03:00Z"}
            )
            view = self.shadow.view(KEY)
            self.assertNotEqual(view["status"], "confirmed")
            self.assertFalse(controller.state.get("pending_sponsorships"))
            self.assertEqual(view["source_slots"], view["slots"])
            self.assertEqual(view["source_provider_funding"], view["provider_funding"])
            self.assertEqual(view["provider_funding"]["openai"], "org")

    def test_provider_defaults_and_profile_override(self):
        """Provider defaults cover every role; an explicit profile route keeps precedence."""
        import sponsorship

        config = {
            **self.config,
            "provider_credential_routes": {"openai": "chatgpt-subscription", "anthropic": "claude-subscription"},
        }
        config.pop("credential_routes")
        routes = sponsorship.selected_routes(config)
        self.assertTrue(
            all(
                r["route"] == ("chatgpt-subscription" if r["provider"] == "openai" else "claude-subscription")
                for r in routes
            )
        )
        config["credential_routes"] = {"codex": "openai-api-key"}
        self.assertEqual(sponsorship.credential_route(config, "codex", "intake"), "openai-api-key")
        self.assertEqual(sponsorship.credential_route(config, "claude", "plan"), "claude-subscription")
        from fakes import named_config

        named = named_config()
        named["provider_credential_routes"] = config["provider_credential_routes"]
        named["credential_routes"] = {"sol-high": "openai-api-key"}
        requirements = sponsorship.selected_routes(named)
        self.assertGreater(len({r["profile"] for r in requirements if r["provider"] == "openai"}), 1)
        self.assertTrue(
            all(
                r["route"]
                == (
                    "openai-api-key"
                    if r["profile"] == "sol-high"
                    else "chatgpt-subscription"
                    if r["provider"] == "openai"
                    else "claude-subscription"
                )
                for r in requirements
            )
        )
        config["provider_credential_routes"]["openai"] = "claude-subscription"
        with self.assertRaises(ValueError):
            sponsorship.selected_routes(config)

    def test_mixed_issue_funding_requires_only_personal_provider_coverage(self):
        """Org Anthropic needs no personal route, token or optional starter availability evidence."""
        self.config.pop("credential_routes")
        self.config["provider_credential_routes"] = {"openai": "openai-api-key"}
        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.metadata = [row for row in self.metadata if row["route"] == "openai-api-key"]
        self.advance()
        with self.current(funded=False) as controller:
            selected = controller.select_admissions([("codex", "intake", {}), ("claude", "plan", {})])
            self.assertEqual([row["funding"] for row in selected], ["contributed", "org"])
            self.assertEqual(selected[0]["sponsor"], "202")
            self.assertIsNone(selected[1]["sponsor"])
            self.assertEqual(controller.state["provider_funding"], {"openai": "contributed", "anthropic": "org"})
            self.registry.record_shadow(
                KEY, controller.state, funding=admissions.task_mode(controller.config, controller.state)
            )
            view = self.shadow.view(KEY)
            self.assertEqual(view["funding"], "mixed")
            self.assertEqual(view["provider_funding"], controller.state["provider_funding"])
            self.metadata = []
            calls = controller.state["calls"]
            with self.assertRaisesRegex(Blocked, "credential"):
                controller.select_admissions([("claude", "plan", {})])
            self.assertEqual(controller.state["calls"], calls)

    def test_explicit_org_preserves_consent_and_frozen_personal_admission(self):
        """Changing future funding preserves earlier payer/version, grants and spent budgets."""
        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        with self.current(funded=False) as controller:
            controller.ask("codex", "intake", {"purpose": "freeze personal"})
            prior = deepcopy(controller.state["operation"]["admission"])
            calls = controller.state["calls"]
            grant = controller.state["sponsorships"]["openai"]["grant_id"]
            self.fake.comments.append(comment(9, "/concurro funding openai org", "bob", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["provider_funding"]["openai"], "org")
            self.assertEqual(controller.state["sponsorships"]["openai"]["grant_id"], grant)
            self.assertEqual(controller.state["sponsorships"]["openai"]["sponsor_id"], "202")
            self.assertEqual(controller.state["operation"]["admission"], prior)
            self.assertEqual(controller.state["calls"], calls)
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")
            # Already admitted work remains authorized until its own consent/version is retired.
            binding = {
                "repository": KEY[0],
                "issue": KEY[1],
                "generation": controller.gh.generation,
                "funding": "contributed",
                "sponsorship_provider": "openai",
                "sponsor": "202",
                "epoch": prior["epoch"],
                "grant_id": prior["grant_id"],
                "credential": prior["credential"]["record_id"],
                "credential_version": 1,
                "route": "openai-api-key",
            }
            self.assertTrue(self.exchange.allowed(binding))
            with self.exchange.locked(), self.exchange._transaction():
                self.exchange.current_grant(binding, prior)
            self.fake.comments.append(comment(8, "/concurro withdraw openai", "alice", "2026-11-12T10:01:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["provider_funding"]["openai"], "org")
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")
            self.assertFalse(self.exchange.allowed(binding))

    def test_withdrawal_never_falls_back_and_lost_writer_can_withdraw(self):
        """Personal payer choice survives self-withdrawal, with no org admission or new charge."""
        self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        self.allowed.remove("alice")
        self.fake.comments.append(comment(9, "/concurro withdraw openai", "alice", "2026-11-12T10:02:00Z"))
        with self.current(funded=False) as controller:
            self.assertEqual(controller.state["provider_funding"]["openai"], "contributed")
            calls = controller.state["calls"]
            with self.assertRaises(Blocked):
                controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(controller.state["calls"], calls)
            own = __import__("sponsorship").own_consent(self.shadow.view(KEY), "202")
            self.assertEqual(own["scope"], "own")
            self.assertTrue(own["history"])
            self.assertNotIn("provider_funding", own)
            self.assertNotIn("charged_calls", own)

    def test_old_org_tasks_ignore_settings_only_payer_changes(self):
        """Forward migration freezes prior org policy and legacy sponsor comments remain prospective."""
        self.sponsors()
        with self.current(funded=False) as controller:
            calls = controller.state["calls"]
            operation = deepcopy(controller.state.get("operation"))
            controller.state.pop("provider_funding", None)
            controller.config = {**controller.config, "funding_mode": "contributed"}
            self.shadow.begin(controller)
            with patch.object(self.shadow, "confirm", side_effect=Transient("synthetic first-upgrade source outage")):
                self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(controller.state["provider_funding"], {"openai": "org", "anthropic": "org"})
            self.assertEqual(controller.select_admissions([("codex", "intake", {})])[0]["funding"], "org")
            self.assertEqual(controller.state.get("operation"), operation)
            self.assertEqual(controller.state["calls"], calls)
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            view = self.shadow.view(KEY)
            self.assertEqual(view["provider_funding"], {"openai": "org", "anthropic": "org"})
            self.assertEqual(view["source_provider_funding"], view["provider_funding"])

    def test_pretask_funding_order_is_independent_of_delivery_and_duplicates(self):
        """Issue intents converge in source order and enrollment adopts the exact latest choice."""
        self.fake.comments = [
            comment(8, "/concurro funding openai org", "bob", "2026-11-12T10:00:02Z"),
            comment(7, "/concurro funding openai personal", "alice", "2026-11-12T10:00:01Z"),
        ]
        with self.current(funded=False) as controller:
            first = self.shadow.view(KEY)
            self.assertEqual(first["provider_funding"]["openai"], "org")
            self.assertEqual(first["slots"]["openai"]["sponsor_id"], "202")
            self.assertNotIn("task", first)
            for raw in self.fake.comments:
                raw["author_association"] = "COLLABORATOR"
            self.service()
            self.assertTrue(self.worker.consumed({}, controller)["_sponsorship_discovery"])
            self.fake.comments.reverse()
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            self.assertEqual(self.shadow.view(KEY)["provider_funding"], first["provider_funding"])
        self.fake.comments.append(comment(10, "/concurro start"))
        state = self.advance()
        self.assertEqual(state["provider_funding"]["openai"], "org")
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_funding_commands_require_current_writer_authority(self):
        """Neither org selection nor personal self-consent can be applied by a nonwriter."""
        self.allowed.remove("alice")
        self.fake.comments += [
            comment(2, "/concurro funding openai personal", "alice"),
            comment(3, "/concurro funding anthropic org", "alice"),
        ]
        self.advance()
        with self.current(funded=False) as controller:
            self.assertEqual(controller.state["provider_funding"], {"openai": "org", "anthropic": "org"})
            self.assertFalse(any(f["actor_id"] == "202" for f in controller.shadow_projection["facts"]))

    def test_batch_preflight_has_no_charge_when_any_required_owner_lacks_coverage(self):
        """Aggregate both providers before spending; a retry never borrows the other sponsor."""
        self.sponsors()
        self.metadata = [m for m in self.metadata if m["user_id"] != "303"]
        with self.current() as controller:
            calls = controller.state["calls"]
            with self.assertRaisesRegex(Blocked, "credential"):
                controller.select_admissions([("codex", "intake", {}), ("claude", "plan", {})])
            self.assertEqual(controller.state["calls"], calls)
            self.assertEqual(controller.state["sponsorships"]["anthropic"]["sponsor_id"], "303")

    def test_initial_serial_call_preflights_every_required_role_before_charge(self):
        """Even an OpenAI-only first call pauses when a required Anthropic route lacks coverage."""
        self.sponsors()
        self.metadata = [row for row in self.metadata if row["route"] != "anthropic-api-key"]
        with self.current() as controller:
            calls = controller.state["calls"]
            with self.assertRaisesRegex(Blocked, "credential") as error:
                controller.ask("codex", "intake", {"purpose": "aggregate required roster"})
            self.assertEqual(error.exception.pause_stage, "awaiting_credentials")
            self.assertEqual(error.exception.requirement["provider"], "anthropic")
            self.assertEqual(error.exception.requirement["route"], "anthropic-api-key")
            self.assertEqual(error.exception.requirement["model"], self.config["models"]["claude"])
            self.assertIn("role=", str(error.exception))
            self.assertIn("profile=claude", str(error.exception))
            self.assertEqual(controller.state["calls"], calls)
            self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_pause_acknowledges_old_turn_failures_without_copying_outbox_history(self):
        """Late same-turn rows do not re-pause a writer retry or inflate checkpoints with full history."""
        self.sponsors()
        with self.current() as controller:
            binding = {"sponsorship_provider": "openai"}
            events = [
                {
                    "sequence": index,
                    "turn": "old-turn",
                    "category": "auth" if index in (1, 2, 501) else None,
                    "binding": binding,
                }
                for index in range(1, 501)
            ]
            with patch.object(self.exchange, "observations", return_value={"events": events, "pending": False}):
                with self.assertRaisesRegex(Blocked, "auth"):
                    controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(controller.state["provider_observation"], 500)
            self.assertEqual(controller.state["provider_paused_turns"], ["old-turn"])
            self.assertNotIn("provider_observations", controller.state)
            self.assertTrue(controller.state["provider_pause_pending"])
            # This is the same acknowledgment successful writer retry/resume performs.
            controller.state.pop("provider_pause_pending")
            events.append({"sequence": 501, "turn": "old-turn", "category": "auth", "binding": binding})
            with patch.object(self.exchange, "observations", return_value={"events": events, "pending": False}):
                choices = controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(len(choices), 1)
            self.assertEqual(controller.state["provider_observation"], 501)
            self.assertNotIn("provider_observations", controller.state)
            controller.save()

    def test_all_worker_failures_acknowledge_turns_and_retire_sessions_once(self):
        """One explicit retry covers all drained failures, including evidence below the observed cursor."""
        self.sponsors()
        self.service()
        with self.current() as controller:
            events = [
                {
                    "sequence": i,
                    "turn": "failure-" + str(i),
                    "session": "session-" + str(i),
                    "category": "budget_denied",
                    "binding": {"sponsorship_provider": "openai"},
                }
                for i in (1, 2)
            ]
            error = ProviderFailure(events[0])
            error.observations = events
            self.worker._suspend_failure(controller, controller.issue, error, {"kind": "suspend"}, 0)
            self.assertEqual(controller.state["provider_paused_turns"], [e["turn"] for e in events])
            self.assertEqual(controller.state["provider_retire_sessions"], [e["session"] for e in events])
            self.assertEqual(controller.state["provider_observation"], 2)
            controller.commands([comment(90, "/concurro retry")])
            with (
                patch.object(self.exchange, "end") as ended,
                patch.object(self.exchange, "observations", return_value={"events": events, "pending": False}),
            ):
                self.assertEqual(len(controller.select_admissions([("codex", "intake", {})])), 1)
                self.assertEqual([c.args[0] for c in ended.call_args_list], [e["session"] for e in events])
            self.assertEqual(controller.state["provider_retire_sessions"], [])
            # A worker may have advanced the cursor for another turn before reading an older row.
            older = {**events[0], "sequence": 3, "turn": "late-old-row", "session": "late-session"}
            controller.state["provider_observation"] = 4
            with (
                patch.object(self.exchange, "end") as ended,
                patch.object(
                    self.exchange, "observations", return_value={"events": [*events, older], "pending": False}
                ),
            ):
                with self.assertRaises(ProviderFailure):
                    controller.select_admissions([("codex", "intake", {})])
                ended.assert_called_once_with("late-session")
            self.assertIn("late-old-row", controller.state["provider_paused_turns"])

    def test_writer_reassessment_commands_acknowledge_provider_pause(self):
        """Successful start, invalidate and revision grants reach fresh coverage without stale failure."""
        self.sponsors()
        with self.current() as controller:
            controller.config = {**controller.config, "autonomous": False}
            baseline = deepcopy(controller.state)
            for index, command_text in enumerate(
                ("/concurro start", "/concurro invalidate", "/concurro retry --plan-revisions 1"), 90
            ):
                with self.subTest(command=command_text):
                    controller.state = deepcopy(baseline)
                    controller.state["config_digest"] = config_digest(controller.config)
                    controller.state["provider_failure"] = {
                        "category": "auth",
                        "binding": {"sponsorship_provider": "openai"},
                    }
                    controller.state["provider_pause_pending"] = True
                    controller.suspend("awaiting_credentials", "synthetic provider condition")
                    if "--plan-revisions" in command_text:
                        controller.state["revision_wait"] = {"stage": "intake"}
                        controller.state["plan_revisions"] = controller.plan_revision_limit()
                    controller.commands([*self.fake.comments, comment(index, command_text)])
                    self.assertNotIn("provider_pause_pending", controller.state)
                    self.assertEqual(len(controller.select_admissions([("codex", "intake", {})])), 1)

    def test_bare_sponsor_notices_describe_actual_funding_mode(self):
        """Both pre-task and enrolled replies describe actual choices, independent of config changes."""
        self.sponsors()
        with self.current() as controller:
            from commands import parse

            for index, mode in enumerate(("org", "contributed"), 90):
                controller.config = {**controller.config, "funding_mode": mode}
                controller.state["provider_funding"] = {"openai": mode, "anthropic": mode}
                usage = comment(index, "/concurro sponsor")
                self.shadow.usage(controller, [usage])
                body = self.fake.comments[-1]["body"]
                self.assertEqual("organization credentials" in body, mode == "org")
                with patch("commands.conversation.queue") as queued:
                    controller._command_handler().sponsorship(parse(usage["body"]), usage, [usage])
                self.assertIn(
                    "contributed funding" if mode == "contributed" else "organization credentials",
                    queued.call_args.args[2],
                )

    def test_unavailable_observations_are_explicit_and_do_not_change_consent(self):
        """A read outage is visible as unknown rather than no requests or successful usage."""
        self.sponsors()
        with self.current() as controller:
            self.registry.record_shadow(KEY, controller.state, funding="contributed")
            before = deepcopy(controller.state["sponsorships"])
            with patch.object(self.exchange, "observations", side_effect=ExchangeError(503, "fixture outage")):
                view = self.shadow.view(KEY)
            self.assertEqual(view["observations_status"], "unknown")
            self.assertIsNone(view["observations"])
            self.assertEqual(controller.state["sponsorships"], before)

    def test_unsupported_production_adapter_stops_actual_ask_before_charge(self):
        """Configured contributed funding cannot reach org credential/key creation."""
        self.sponsors()
        self.exchange.gateway.contributed_routes = ()
        with self.current() as controller:
            calls = controller.state["calls"]
            with self.assertRaisesRegex(Blocked, "adapter"):
                controller.ask("codex", "intake", {"purpose": "unsupported route"})
            self.assertEqual(controller.state["calls"], calls)
            self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_permission_denial_isolated_by_owner_and_outage_is_not_denial(self):
        """Fresh writer denial fences that owner's providers; an outage retains consent and charges zero."""
        self.sponsors()
        with self.current() as controller:
            other = deepcopy(controller.state["sponsorships"]["anthropic"])
            self.allowed.remove("alice")
            calls = controller.state["calls"]
            with self.assertRaisesRegex(Blocked, "permission"):
                controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(controller.state["sponsorships"]["anthropic"], other)
            self.assertEqual(controller.state["calls"], calls)
            with patch.object(self.shadow, "writer", side_effect=Transient("fixture outage")):
                with self.assertRaises(Transient):
                    controller.select_admissions([("claude", "plan", {})])
            self.assertEqual(controller.state["sponsorships"]["anthropic"], other)

    def test_source_hint_during_metadata_prevents_charge(self):
        """A newly observed consent revision cannot freeze a stale selection after I/O."""
        self.sponsors()
        with self.current() as controller:
            original = self.shadow.metadata.read

            def racing(actor, at=None):
                """Persist a real local hint between source observation and admission."""
                self.deliver_source(comment(22, "/concurro withdraw openai", "alice", "2026-11-12T10:01:00Z"))
                return original(actor, at)

            calls = controller.state["calls"]
            with patch.object(self.shadow.metadata, "read", side_effect=racing):
                with self.assertRaises(Transient):
                    controller.ask("codex", "intake", {"purpose": "race"})
            self.assertEqual(controller.state["calls"], calls)

    def test_real_serial_controller_runner_exchange_preserves_frozen_identity(self):
        """Run the actual serial adapters, fake only the native job and external providers."""
        self.serial_flow()

    def test_budget_retirement_outage_survives_reload_and_writer_retry(self):
        """A failed-before-send cleanup cannot reuse the old exhausted key after explicit retry."""
        self.serial_flow("before")

    def test_budget_retirement_lost_reply_is_idempotent_after_writer_retry(self):
        """A successful retirement with a lost response is replayed before fresh staffing."""
        self.serial_flow("after")

    def serial_flow(self, retirement=None, mixed=False):
        """Exercise actual serial admissions, optionally losing an exact-session cleanup receipt."""
        if mixed:
            self.fake.comments.append(comment(2, "/concurro funding openai personal", "alice", "2026-11-12T10:00:00Z"))
            self.metadata = [row for row in self.metadata if row["route"] == "openai-api-key"]
            self.advance()
        else:
            self.sponsors()
        with self.current(funded=not mixed) as controller:
            expected = {**EXPECTED, "actor_id": "3003"}
            operator = LocalOperator(self.exchange)
            task = {
                **BINDING,
                "repository": KEY[0],
                "upstream": KEY[0],
                "fork": KEY[0],
                "generation": controller.gh.generation,
            }
            task.pop("role")
            task.pop("base")
            task.pop("phase")
            responses = []

            def dispatch(session, labels):
                """Serve a fake native runner via genuine Exchange key/claim/complete methods."""
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
                runner = self.exchange.key(session, token, engine=task["engine"])["token"]
                turns = self.exchange.next(session, runner, 1, 0)["turns"]
                self.assertEqual(len(turns), 1)
                responses.append(turns[0])
                self.exchange.complete(
                    session, runner, turns[0]["id"], {"outcome": "ok", "response": {"synthetic": "completed"}}
                )

            executor = RunnerExecutor(
                operator,
                "https://fixture.invalid",
                expected,
                ["*"],
                1000,
                dispatch,
                task=task,
                check_owner=lambda: self.gateway.check_owner(KEY, controller.gh.generation),
            )
            controller.agents = Agents(controller.config, executor, controller.bundle)
            with patch("exchange.claims", side_effect=lambda token, *args: json.loads(token)):
                result = controller.ask("codex", "intake", {**controller.context(), "purpose": "admission identity"})
            self.assertEqual(result, {"synthetic": "completed"})
            operation = controller.state["operation"]
            admission = operation["admission"]
            self.assertEqual(admission["sponsor"], "202")
            self.assertEqual(admission["credential"]["version"], 1)
            self.assertEqual(responses[0]["id"], str(uuid.UUID(operation["attempt"])))
            binding = json.loads(self.exchange.db.execute("SELECT binding FROM turns").fetchone()[0])
            self.assertEqual(
                (binding["sponsor"], binding["epoch"], binding["credential_version"]), ("202", admission["epoch"], 1)
            )
            self.assertEqual(next(iter(self.exchange.gateway.resources.values()))["binding"], binding)
            if mixed:
                with patch("exchange.claims", side_effect=lambda token, *args: json.loads(token)):
                    controller.ask("claude", "intake", {**controller.context(), "purpose": "organization provider"})
                self.assertEqual(controller.state["operation"]["admission"]["funding"], "org")
                rows = [
                    json.loads(row[0]) for row in self.exchange.db.execute("SELECT binding FROM turns ORDER BY rowid")
                ]
                self.assertEqual([row["funding"] for row in rows], ["contributed", "org"])
                self.assertEqual(rows[0]["sponsor"], "202")
                self.assertIsNone(rows[1]["sponsor"])
                self.assertEqual(
                    sum(row["calls"] for row in controller.state["credentials"].values()), controller.state["calls"]
                )
                return
            self.exchange.gateway.contributed_routes = ()
            with patch.object(self.shadow, "preflight", side_effect=AssertionError("recovery reselected payer")):
                recovered = controller.ask("codex", "intake", {**controller.context(), "purpose": "admission identity"})
                self.assertEqual(recovered["synthetic"], result["synthetic"])
                self.assertEqual(controller.state["operation"]["admission"], admission)
            if retirement is not None:
                old_turn = responses[-1]["id"]
                old_session = self.exchange.db.execute("SELECT session FROM turns WHERE id=?", (old_turn,)).fetchone()[
                    0
                ]
                old_key = self.exchange.row(old_session)["key_id"]
                event = {
                    "sequence": 1,
                    "turn": old_turn,
                    "session": old_session,
                    "category": "budget_denied",
                    "binding": binding,
                }
                end = self.exchange.end
                calls = controller.state["calls"]

                def unavailable(session):
                    """Choose a failure before application or after the real idempotent mutation."""
                    if retirement == "after":
                        end(session)
                    raise ExchangeError(503, "synthetic retirement reply unavailable")

                with (
                    patch.object(self.exchange, "observations", return_value={"events": [event], "pending": False}),
                    patch.object(self.exchange, "end", side_effect=unavailable),
                ):
                    with self.assertRaises(Transient):
                        controller.select_admissions([("codex", "intake", {})])
                    self.assertEqual(controller.state["calls"], calls)
                    self.assertEqual(controller.state["provider_retire_sessions"], [old_session])
                    controller.load(self.fake.comments)
                    self.assertEqual(controller.state["provider_retire_sessions"], [old_session])
                    controller.suspend("budget_exhausted", "synthetic native denial")
                    retry = comment(90, "/concurro retry")
                    self.fake.comments.append(retry)
                    controller.commands(self.fake.comments)
                    self.assertNotIn("provider_pause_pending", controller.state)
                    self.assertEqual(controller.state["provider_retire_sessions"], [old_session])
                    self.exchange.gateway.contributed_routes = ("openai-api-key", "anthropic-api-key")
                    with self.assertRaises(Transient):
                        controller.select_admissions([("codex", "intake", {})])
                    self.assertEqual(controller.state["calls"], calls)
                with (
                    patch.object(self.exchange, "observations", return_value={"events": [event], "pending": False}),
                    patch("exchange.claims", side_effect=lambda token, *args: json.loads(token)),
                ):
                    result = controller.ask(
                        "codex", "intake", {**controller.context(), "purpose": "writer retry after budget"}
                    )
                self.assertEqual(result, {"synthetic": "completed"})
                new_turn = responses[-1]["id"]
                new_session = self.exchange.db.execute("SELECT session FROM turns WHERE id=?", (new_turn,)).fetchone()[
                    0
                ]
                self.assertNotEqual(new_session, old_session)
                self.assertNotEqual(self.exchange.row(new_session)["key_id"], old_key)
                self.assertIsNotNone(self.exchange.row(old_session)["ended"])
                self.assertEqual(controller.state["provider_retire_sessions"], [])
                self.assertEqual(controller.state["calls"], calls + 1)

    def test_source_policy_interval_controls_fresh_initializer_and_never_backdates(self):
        """Contributed source evidence is produced before the event, not inferred from delayed settings."""
        self.config["funding_mode"] = "contributed"
        self.registry.activate_funding(KEY[0], config_digest(self.config), "contributed")
        self.agents.generation = 1
        state = self.advance()
        self.assertEqual(state["funding_initialization"]["source_funding_mode"], "contributed")
        self.assertEqual(state["sponsorships"]["openai"]["source_policy"]["funding_mode"], "contributed")
        self.assertIsNotNone(state["sponsorships"]["openai"]["source_policy"]["receipt_id"])
        self.assertTrue(
            self.registry.funding_policy(KEY[0], config_digest(self.config), self.clock() - 1)["funding_mode"]
            == "unknown"
        )

    def test_unknown_historical_source_never_fills_personal_sponsor(self):
        """An unknown historical starter cannot become a payer from current credentials."""
        self.config["funding_mode"] = "contributed"
        self.agents.generation = 1
        with self.assertRaises(Blocked):
            self.advance()
        state = self.state()
        self.assertIsNone(state["sponsorships"]["openai"].get("sponsor_id"))
        self.assertEqual(state["calls"], 0)
        self.assertEqual(state["funding_initialization"]["initializer_status"], "unknown")

    def test_org_cutover_preserves_explicit_and_clears_implicit_without_refill(self):
        """Historical org starter attribution stays auditable and never becomes contributed consent."""
        self.fake.comments.append(comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"))
        self.advance()
        with self.current() as controller:
            explicit = deepcopy(controller.state["sponsorships"]["openai"])
            old = deepcopy(controller.state["sponsorships"]["anthropic"])
            self.shadow.cutover(controller)
            self.assertEqual(controller.state["sponsorships"]["openai"], explicit)
            self.assertEqual(controller.state["sponsorships"]["anthropic"]["epoch"], old["epoch"] + 1)
            self.assertIsNone(controller.state["sponsorships"]["anthropic"].get("sponsor_id"))
            self.assertEqual(
                controller.state["funding_initialization"]["slots"]["anthropic"]["grant_id"], old["grant_id"]
            )
            self.shadow.cutover(controller)
            self.assertEqual(controller.state["sponsorships"]["anthropic"]["epoch"], old["epoch"] + 1)

    def test_actual_batch_reservation_preflights_all_owners_and_freezes_distinct_attempts(self):
        """Design.reserve shares the same all-owner no-charge boundary as serial calls."""
        self.sponsors()
        self.advance()
        with self.current() as controller:
            task = {
                **BINDING,
                "repository": KEY[0],
                "upstream": KEY[0],
                "fork": KEY[0],
                "generation": controller.gh.generation,
            }
            for key in ("role", "base", "phase"):
                task.pop(key)
            executor = RunnerExecutor(
                LocalOperator(self.exchange),
                "https://fixture.invalid",
                {**EXPECTED, "actor_id": "3003"},
                ["*"],
                1000,
                lambda *args: None,
                task=task,
            )
            controller.agents = Agents(
                controller.config, executor, controller.bundle, retained=Path(self.directory.name) / "retained"
            )
            adapter = Design(controller)
            keys = adapter.open()
            self.assertGreaterEqual(len(keys), 2)
            calls = controller.state["calls"]
            removed = [m for m in self.metadata if m["user_id"] == "303"]
            self.metadata = [m for m in self.metadata if m["user_id"] != "303"]
            with self.assertRaises(Blocked):
                adapter.reserve(keys, initial=True)
            self.assertEqual(controller.state["calls"], calls)
            self.metadata += removed
            freeze = admissions.freeze
            attempts = []

            def second_fails(*args, **kwargs):
                """Fail the second prepared admission before any roster journal becomes started."""
                attempts.append(None)
                if len(attempts) == 2:
                    raise Blocked("synthetic second admission freeze failure")
                return freeze(*args, **kwargs)

            with patch.object(admissions, "freeze", side_effect=second_fails), self.assertRaises(Blocked):
                adapter.reserve(keys, initial=True)
            self.assertEqual(controller.state["calls"], calls)
            for member in adapter.batch["members"].values():
                self.assertEqual(member["operation"]["status"], "new")
                self.assertEqual(member["operation"]["attempts"], 0)
                self.assertEqual(member["operation"]["uncertain"], 0)
                self.assertIsNone(member["operation"]["attempt"])
            checked = []
            original = self.shadow.writer
            with patch.object(
                self.shadow, "writer", side_effect=lambda *args: (checked.append(str(args[1])), original(*args))[1]
            ):
                prepared = adapter.reserve(keys, initial=True)
            self.assertEqual(checked.count("202"), 1)
            self.assertEqual(checked.count("303"), 1)
            self.assertEqual(controller.state["calls"], calls + len(keys))
            turns = []
            for key, turn in prepared.items():
                handle = executor.submit(turn)
                turns.append(handle.identifier)
                member = adapter.batch["members"][key]
                self.assertEqual(turn["request"]["admission"], member["operation"]["admission"])
                self.assertEqual(turn["request"]["admission"]["call"], member["operation"]["call"])
            self.assertEqual(len(set(turns)), len(keys))
            self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], len(keys))

    def test_pending_legacy_metadata_can_adopt_later_explicit_without_inventing_history(self):
        """Unknown old availability remains unknown while a new exact explicit grant progresses."""
        for record in self.metadata:
            if record["user_id"] == "101":
                record["models"] = None
        state = self.advance()
        boundary = deepcopy(state["funding_initialization_pending"])
        self.fake.comments.append(comment(20, "/concurro sponsor openai", "alice", "2026-11-12T10:01:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        frozen = state["funding_initialization"]
        self.assertEqual(frozen["initializer_status"], "unknown")
        self.assertIsNone(frozen["metadata"])
        self.assertIsNone(frozen["available"])
        self.assertEqual(frozen["boundary"]["source_id"], boundary["source_id"])
        self.assertIsNone(state["sponsorships"]["anthropic"].get("sponsor_id"))

    def test_current_config_observation_persists_policy_but_pinned_recovery_does_not_activate(self):
        """The runtime producer records current configuration without backdating older source events."""
        self.service()
        target = self.worker.load(KEY[0], latest=True)
        now = self.clock()
        receipt = self.registry.funding_policy(KEY[0], config_digest(target.config), now)
        self.assertEqual(receipt["funding_mode"], "org")
        self.assertEqual(receipt["effective_from"], now)
        self.assertEqual(
            self.registry.funding_policy(KEY[0], config_digest(target.config), now - 1)["funding_mode"], "unknown"
        )
        self.registry.bind(KEY, target.config["upstream"], target.config["fork"], target.config_sha)
        self.clock.now += 100
        with patch.object(
            self.registry, "activate_funding", side_effect=AssertionError("pinned load activated policy")
        ):
            self.worker.load(*KEY)

    def test_retry_keeps_charged_admission_in_prior_audit_revision(self):
        """Replaced attempts remain attributable through existing immutable audit artifact history."""
        self.sponsors()
        with self.current() as controller:
            snapshots = []

            def answer(*args):
                """Replace a token between attempts after the first charged checkpoint is durable."""
                snapshots.append((controller.checkpoints.reference(), deepcopy(controller.state["operation"])))
                if len(snapshots) == 1:
                    for record in self.metadata:
                        if record["user_id"] == "303":
                            record["version"] = 2
                    raise SessionFailure("synthetic failed session")
                return {"synthetic": "retry complete"}

            with patch.object(controller.agents, "ask", side_effect=answer):
                controller.ask("claude", "plan", {**controller.context(), "purpose": "audit retry"})
            self.assertEqual(len(snapshots), 2)
            first_ref, first_op = snapshots[0]
            self.assertNotEqual(first_op["attempt"], snapshots[1][1]["attempt"])
            self.assertEqual(first_op["admission"]["credential"]["version"], 1)
            self.assertEqual(snapshots[1][1]["admission"]["credential"]["version"], 2)
            historical = next(
                v
                for v in controller.checkpoints.history(controller.checkpoints.audit)
                if v["version"] == first_ref["revision"]
            )
            retained = controller.checkpoints.decode({"body": historical["body"]})
            self.assertEqual(retained["operation"]["admission"], first_op["admission"])
            self.assertEqual(retained["operation"]["attempt"], first_op["attempt"])
            self.assertEqual(retained["calls"], first_op["admission"]["call"])

    def test_serial_saved_design_result_preserves_funded_transcript_scope(self):
        """Recovery uses the old admitted grant scope before any current payer selection."""
        self.sponsors()
        self.advance()
        with self.current() as controller:
            member = design_roster(controller.config)[0]
            profile = member["provider"]
            _, context = controller.quality_context("plan_qc", member)
            controller.ask(profile, "plan_qc", context)
            scopes = deepcopy(controller.state["sessions"])
            prior = deepcopy(controller.state["operation"]["admission"])
            self.exchange.gateway.contributed_routes = ()
            with patch.object(self.shadow, "preflight", side_effect=AssertionError("recovery reselected")):
                controller.ask(profile, "plan_qc", context)
            self.assertEqual(controller.state["sessions"], scopes)
            self.assertEqual(controller.state["operation"]["admission"], prior)

    def test_transient_initializer_outage_preserves_unfilled_provider_until_recovery(self):
        """One explicit sponsor cannot turn a temporary metadata failure into an empty once-only fill."""
        self.config["funding_mode"] = "contributed"
        self.registry.activate_funding(KEY[0], config_digest(self.config), "contributed")
        self.fake.comments.append(comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"))
        self.agents.generation = 1
        original = self.shadow.metadata.read

        def outage(actor, at=None):
            """Fail only the source-era initiator read, leaving current explicit coverage readable."""
            if str(actor) == "101" and at == AT:
                raise Transient("synthetic temporary metadata outage")
            return original(actor, at)

        with patch.object(self.shadow.metadata, "read", side_effect=outage), self.assertRaises(Blocked):
            self.advance()
        pending = self.state()
        self.assertNotIn("funding_initialization", pending)
        self.assertEqual(pending["funding_initialization_pending"]["at"], AT)
        self.assertEqual(pending["calls"], 0)
        self.agents.generation = 2
        recovered = self.advance()
        self.assertEqual(recovered["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(recovered["sponsorships"]["anthropic"]["sponsor_id"], "101")
        self.assertEqual(recovered["funding_initialization"]["boundary"], pending["funding_initialization_pending"])
        self.assertTrue(recovered["funding_initialization"]["available"])

    def test_actual_serial_transcript_survives_version_replacement_and_provider_transfer(self):
        """Changing the admitted payer changes staffing identity without erasing task transcript state."""
        self.sponsors()
        self.advance()
        with self.current() as controller:
            controller.ask("claude", "plan", {**controller.context(), "purpose": "first funded turn"})
            original = deepcopy(controller.state["operation"]["admission"])
            for record in self.metadata:
                if record["user_id"] == "303":
                    record["version"] = 2
            controller.ask("claude", "plan", {**controller.context(), "purpose": "replacement turn"})
            replaced = deepcopy(controller.state["operation"]["admission"])
            self.assertEqual(replaced["transcript_scope"], original["transcript_scope"])
            self.assertNotEqual(replaced["credential"], original["credential"])
            self.fake.comments.append(comment(22, "/concurro sponsor anthropic", "alice", "2026-11-12T10:02:00Z"))
            self.shadow.begin(controller)
            self.shadow.sync(controller, self.fake.comments)
            controller.ask("claude", "plan", {**controller.context(), "purpose": "transferred turn"})
            transferred = controller.state["operation"]["admission"]
            self.assertEqual(transferred["sponsor"], "202")
            self.assertEqual(transferred["transcript_scope"], original["transcript_scope"])
            self.assertNotEqual(transferred["grant_id"], original["grant_id"])

    def test_stale_pending_permission_batch_resigns_only_proven_unapplied_member(self):
        """A newer org admission cannot strand pending cutoffs or replace a committed peer receipt."""
        self.advance()
        with self.current(funded=False) as controller:
            generation = controller.gh.generation
            for provider, slot in list(controller.state["sponsorships"].items()):
                new = {"sponsor_id": None, "status": "withdrawn", "epoch": slot["epoch"] + 1}
                self.shadow.pending(
                    controller,
                    provider,
                    new,
                    slot["epoch"],
                    "permission_loss",
                    actor="101",
                    cutoff=slot["epoch"],
                    save=False,
                )
                request = controller.state["pending_sponsorships"][provider]["request"]
                request["generation"] = generation - 1
                request["transition"] = digest({k: v for k, v in request.items() if k != "transition"})
            controller.save()
            first = deepcopy(controller.state["pending_sponsorships"]["openai"]["request"])
            second = deepcopy(controller.state["pending_sponsorships"]["anthropic"]["request"])
            self.exchange.sponsorship(first)
            turn, binding, payload = org_turn(generation, {"repository": KEY[0], "upstream": KEY[0], "fork": KEY[0]})
            LocalOperator(self.exchange).submit(
                turn, {**EXPECTED, "actor_id": "3003"}, ["synthetic-model"], 1000, 3600, payload, None, binding
            )
            with patch.object(self.shadow, "operator", return_value=LocalOperator(self.exchange)):
                self.shadow.flush(controller)
            self.assertFalse(controller.state.get("pending_sponsorships"))
            accepted = {value["provider"]: value for value in controller.state["sponsorship_history"][-2:]}
            self.assertEqual(accepted["openai"]["transition"], first["transition"])
            self.assertEqual(accepted["openai"]["generation"], generation - 1)
            self.assertNotEqual(accepted["anthropic"]["transition"], second["transition"])
            self.assertEqual(accepted["anthropic"]["generation"], generation)
            self.assertEqual(accepted["anthropic"]["expected_epoch"], second["expected_epoch"])
            self.assertIsNone(
                self.exchange.db.execute(
                    "SELECT id FROM sponsorship_transitions WHERE id=?", (second["transition"],)
                ).fetchone()
            )
            self.assertEqual(
                self.exchange.db.execute(
                    "SELECT provider,actor,through_epoch FROM sponsorship_revocations ORDER BY provider"
                ).fetchall(),
                [("anthropic", "101", 1), ("openai", "101", 1)],
            )

    def test_generation_lookup_failure_never_starts_or_debits_serial_attempt(self):
        """Fallible executor identity resolution happens before journal status or uncertainty changes."""
        self.sponsors()
        with self.current() as controller:
            calls = controller.state["calls"]
            with patch.object(
                type(controller.agents),
                "generation",
                new_callable=PropertyMock,
                create=True,
                side_effect=Transient("synthetic identity outage"),
            ):
                with self.assertRaises(Transient):
                    controller.ask("codex", "intake", {**controller.context(), "purpose": "identity failure"})
            operation = controller.state["operation"]
            self.assertEqual(operation["status"], "new")
            self.assertEqual(operation["attempts"], 0)
            self.assertEqual(operation["uncertain"], 0)
            self.assertIsNone(operation["attempt"])
            self.assertEqual(controller.state["calls"], calls)

    def test_transient_preflight_exchange_failures_wait_without_charge_or_suspension(self):
        """Retry only uncharged capability/publication I/O through the existing scheduler policy."""
        self.sponsors()
        with self.current() as controller:
            calls = controller.state["calls"]
            for method, status in (("capabilities", 0), ("capabilities", 429), ("capabilities", 503), ("flush", 503)):
                with self.subTest(method=method, status=status):
                    target = self.exchange if method == "capabilities" else self.shadow
                    with patch.object(target, method, side_effect=ExchangeError(status, "synthetic outage")):
                        with self.assertRaises(Transient) as caught:
                            controller.ask(
                                "codex", "intake", {**controller.context(), "purpose": str((method, status))}
                            )
                    self.assertEqual(failures.classify(caught.exception).kind, "wait-until")
                    self.assertIsNone(failures.effect(caught.exception, verified=True))
                    self.assertEqual(controller.state["calls"], calls)
                    self.assertEqual(controller.state["operation"]["status"], "new")
            conflict = ExchangeError(409, "sponsorship fence conflict")
            with patch.object(self.exchange, "capabilities", side_effect=conflict):
                with self.assertRaises(ExchangeError) as caught:
                    controller.ask("codex", "intake", {**controller.context(), "purpose": "permanent conflict"})
            self.assertIs(caught.exception, conflict)
            self.assertEqual(failures.classify(conflict).kind, "blocked")
            self.assertEqual(controller.state["calls"], calls)


def load_tests(loader, tests, pattern):
    """Run new scenarios once while reusing the established shadow fixture and its helpers."""
    return unittest.TestSuite(AdmissionTests(name) for name in AdmissionTests.__dict__ if name.startswith("test_"))
