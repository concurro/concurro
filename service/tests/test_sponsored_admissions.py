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
from policy import Blocked, SessionFailure, design_roster, digest
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
        self.sponsors()
        with self.current() as controller:
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
            self.exchange.gateway.contributed_routes = ()
            with patch.object(self.shadow, "preflight", side_effect=AssertionError("recovery reselected payer")):
                recovered = controller.ask("codex", "intake", {**controller.context(), "purpose": "admission identity"})
                self.assertEqual(recovered["synthetic"], result["synthetic"])
                self.assertEqual(controller.state["operation"]["admission"], admission)

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
