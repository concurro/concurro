"""Shadow sponsorship through real controller checkpoints, Registry and Exchange transitions."""

import io
import json
import sys
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import ENGINE, KEY, ServiceFixture, target_files

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
from exchange_fixtures import AUDIENCE, FakeGateway

import delegation
import test_ingress as ingress_tests
from exchange import Exchange, Keys
from github import Transient
from policy import Blocked
from relay import ExchangeError
from service.ingress import Ingress
from service.shadow import Metadata, Shadow

AT = "2026-11-12T10:00:10Z"
KEY_BYTES = b"synthetic-delegation-key"
ACTORS = {"writer": 101, "alice": 202, "bob": 303}


def comment(identifier, body, login="writer", at=AT):
    """A source-timestamped GitHub comment with a stable, independently verified author."""
    return {
        "id": identifier,
        "body": body,
        "user": {"login": login, "id": ACTORS[login]},
        "created_at": at,
        "updated_at": at,
    }


class ShadowTests(ServiceFixture, unittest.TestCase):
    """Exercise source ordering and transition replay without changing org-paid agents."""

    def setUp(self):
        """Wire real state machines and a fake metadata-only vault into the existing service fixture."""
        super().setUp()
        self.config["credential_routes"] = {"codex": "openai-api-key", "claude": "anthropic-api-key"}
        self.fake.comments = [comment(1, "/concurro start")]
        self.allowed = set(ACTORS)
        self.fake.permitted = lambda login: login in self.allowed
        self.exchange = Exchange(str(Path(self.directory.name) / "exchange.db"), FakeGateway(), Keys(), AUDIENCE)
        self.addCleanup(self.exchange.db.close)
        self.metadata_calls = []
        self.deliveries = 0
        self.ingress = Ingress(
            b"webhook-secret",
            self.registry,
            {KEY[0]: 42},
            {self.config["bot_login"]},
            command_comment=delegation.marked,
        )
        self.metadata = [
            {
                "user_id": "101",
                "route": "openai-api-key",
                "version": 1,
                "status": "valid",
                "models": [self.config["models"]["codex"]],
            },
            {
                "user_id": "101",
                "route": "anthropic-api-key",
                "version": 1,
                "status": "valid",
                "models": [self.config["models"]["claude"], self.config["qc_model"]],
            },
        ]
        self.shadow = Shadow(
            self.registry, lambda repo: self, lambda: self.exchange, SimpleNamespace(read=self.read_metadata), KEY_BYTES
        )

    def request(self, method, path, data=None):
        """Stable-ID and collaborator reads are independent from source comment contents."""
        if path.startswith("user/"):
            actor = int(path.split("/")[1])
            return {"id": actor, "login": next(name for name, value in ACTORS.items() if value == actor)}
        if "/collaborators/" in path:
            login = path.split("/collaborators/")[1].split("/")[0]
            return {"permission": "write" if login in self.allowed else "read"}
        return self.backend.request(method, path, data)

    def read_metadata(self, actor, at=None):
        """Record the exact initiation boundary used for route availability."""
        self.metadata_calls.append((actor, at))
        return [deepcopy(m) for m in self.metadata if m["user_id"] == str(actor)]

    def controller(self, generation):
        """Use production persistence and command flow with the injected shadow adapter."""
        result = super().controller(generation)
        result.sponsorship, result.delegation_key = self.shadow, KEY_BYTES
        return result

    def advance(self):
        """Tick through the real generation owner and reload the saved checkpoint afterward."""
        state, _ = self.tick()
        return state

    def test_explicit_before_start_wins_without_credential_and_org_still_pays(self):
        """Read complete source history even when no prior webhook announced the earlier sponsor."""
        self.fake.comments.append(comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")
        self.assertIn(("101", AT), self.metadata_calls)
        self.assertEqual(state["funding_initialization"]["boundary"]["source_id"], 1)
        self.assertTrue(state["calls"])
        self.assertTrue(all(not key.startswith("contributed") for key in state["credentials"]))
        self.assertEqual(state["operation"]["shadow_sponsorship"]["funding"], "org")
        self.assertEqual(state["sponsorships"]["openai"]["provenance"], "explicit_comment")

    def test_unenrolled_intent_does_not_create_task_or_call(self):
        """Persist a confirmed pre-task badge projection without a fabricated task stage."""
        self.fake.comments = [comment(4, "/concurro sponsor openai", "alice")]
        self.fake.issue["labels"] = []
        self.assertIsNone(self.advance())
        projection = self.registry.sponsorship(KEY)
        self.assertEqual(projection["status"], "confirmed")
        self.assertEqual(projection["slots"]["openai"]["sponsor_id"], "202")
        self.assertEqual(self.agents.calls, [])
        self.assertNotIn("task", projection)

    def test_transfer_withdraw_reoptin_are_provider_local_and_replay_safe(self):
        """Independent monotonic epochs survive withdrawal while org counters and approvals persist."""
        state = self.advance()
        other = deepcopy(state["sponsorships"]["anthropic"])
        self.fake.comments.append(comment(20, "/concurro sponsor openai", "alice", "2026-11-12T10:01:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 2)
        self.fake.comments.append(comment(21, "/concurro withdraw openai", "alice", "2026-11-12T10:02:00Z"))
        self.allowed.remove("alice")
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["status"], "withdrawn")
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 3)
        self.assertEqual(state["sponsorships"]["anthropic"], other)
        replay = self.advance()
        self.assertEqual(replay["sponsorships"]["openai"]["epoch"], 3)
        self.allowed.add("alice")
        self.fake.comments.append(comment(22, "/concurro sponsor openai", "alice", "2026-11-12T10:03:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 4)
        rows = self.exchange.db.execute("SELECT provider,actor,through_epoch FROM sponsorship_revocations").fetchall()
        self.assertTrue(any(p == "openai" and actor == "202" and cutoff >= 2 for p, actor, cutoff in rows))
        self.assertTrue(
            any(
                item["reason"] == "withdraw" and item["actor"] == "202" and item["cutoff"] == 2
                for item in state["sponsorship_history"]
            )
        )

    def test_withdraw_before_boundary_blocks_only_that_initiator(self):
        """An actor's pre-enrollment tombstone does not veto a different starter's available routes."""
        self.fake.comments.append(comment(2, "/concurro withdraw anthropic", "alice", "2026-11-12T10:00:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")

    def test_metadata_outage_keeps_boundary_pending_without_inventing_empty_fill(self):
        """Org work can continue while one-time metadata reconstruction retries at its original cutoff."""
        with patch.object(self.shadow.metadata, "read", side_effect=Transient("fixture outage")):
            state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(state["funding_initialization_pending"]["at"], AT)
        self.assertTrue(state["calls"])
        state = self.advance()
        self.assertEqual(state["funding_initialization"]["boundary"]["at"], AT)
        self.assertEqual(self.metadata_calls[-1], ("101", AT))
        self.assertEqual(state["sponsorship_status"], "confirmed")
        self.assertNotIn("sponsorship_retry_attempts", state)

    def test_signed_provider_and_actor_are_verified(self):
        """A provider edit invalidates the signed command; a valid command restores the human's stable ID."""
        body = delegation.render(KEY_BYTES, KEY[0], 7, "alice", 202, "/concurro sponsor openai", "a" * 32)
        signed = comment(4, body, at="2026-11-12T10:00:00Z")
        signed["user"] = {"id": 999, "login": self.config["bot_login"]}
        self.fake.comments.append(signed)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertIsNone(
            delegation.resolve(
                {**signed, "body": body.replace("openai", "anthropic")}, KEY_BYTES, *KEY, self.config["bot_login"]
            )
        )

    def test_pre_boundary_late_claim_transfers_future_only(self):
        """Late source discovery uses an epoch transition and preserves prior charged org attribution."""
        state = self.advance()
        self.fake.comments.append(comment(20, "/concurro stop", "writer", "2026-11-12T10:01:00Z"))
        state = self.advance()
        old = deepcopy(state["operation"])
        calls, ledger = state["calls"], deepcopy(state["credentials"])
        self.fake.comments.append(comment(3, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 2)
        self.assertEqual(state["operation"], old)
        self.assertEqual(state["calls"], calls)
        self.assertEqual(state["credentials"], ledger)
        self.assertEqual(state["operation"]["shadow_sponsorship"]["funding"], "org")
        self.assertEqual(state["operation"]["shadow_sponsorship"]["prospective_sponsor"], "101")

    def deliver_source(self, source, action="created", previous=None, editor=None):
        """Use the real signed webhook/receipt/observation path for revision hints."""
        payload = {
            "repository": {"full_name": KEY[0]},
            "installation": {"id": 42},
            "sender": {"login": source["user"]["login"], "id": source["user"]["id"] if editor is None else editor},
            "action": action,
            "issue": {"number": 7, "labels": [], "user": {"login": "author"}},
            "comment": {
                **source,
                "user": {**source["user"], "type": "User"},
                "author_association": source.get("author_association", "NONE"),
            },
        }
        if previous is not None:
            payload["changes"] = {"body": {"from": previous}}
        self.assertEqual(ingress_tests.IngressTests.deliver(self, "issue_comment", payload), 202)

    def test_out_of_order_hooks_use_source_order_and_reserved_missing_coverage(self):
        """A delayed older claim cannot displace the later source claim or fall back to the starter."""
        first = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
        later = comment(3, "/concurro sponsor openai", "bob", "2026-11-12T10:00:01Z")
        self.fake.comments += [first, later]
        self.deliver_source(later)
        self.deliver_source(first)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "303")
        projection = self.shadow.view(KEY)
        self.assertTrue(all(r["status"] == "missing" for r in projection["coverage"]["openai"]))
        self.assertEqual(len(self.registry.sponsor_sources(KEY)), 2)

    def test_edit_away_before_boundary_allows_different_starter_only(self):
        """Pre-enrollment removal withdraws its source and preserves that actor's tombstone."""
        original = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
        edited = {**original, "body": "Removed", "updated_at": "2026-11-12T10:00:05Z"}
        self.fake.comments.append(edited)
        self.deliver_source(original)
        self.deliver_source(edited, "edited", original["body"])
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")
        self.assertIn("202", state["funding_initialization"]["tombstones"]["openai"])

    def test_post_boundary_removal_never_widens_starter_fill(self):
        """A removal visible at freeze but after initiation is an epoch withdrawal, not a new fill."""
        original = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
        edited = {**original, "body": "Removed", "updated_at": "2026-11-12T10:00:20Z"}
        self.fake.comments.append(edited)
        self.deliver_source(original)
        self.deliver_source(edited, "edited", original["body"])
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["status"], "withdrawn")
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 2)
        self.assertEqual(state["funding_initialization"]["slots"]["openai"]["sponsor_id"], "202")

    def test_unobserved_source_and_local_read_race_keep_initialization_pending(self):
        """Known incomplete history and a newer source hint cannot be acknowledged as empty consent."""
        unseen = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
        self.deliver_source(unseen)
        state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(state["funding_initialization_pending"]["at"], AT)
        self.fake.comments.append(unseen)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_exchange_lost_ack_replays_the_same_checkpointed_transition(self):
        """A committed fence with a lost reply does not rotate epochs or repeat initiator fill."""
        actual = self.exchange.sponsorship
        failed = False

        def publish(request):
            """Lose exactly the first response after its real transaction committed."""
            nonlocal failed
            result = actual(request)
            if not failed:
                failed = True
                raise ExchangeError(0, "synthetic lost reply")
            return result

        with patch.object(self.shadow, "operator", return_value=SimpleNamespace(sponsorship=publish)):
            state = self.advance()
            self.assertTrue(state["pending_sponsorships"])
            state = self.advance()
        self.assertEqual({slot["epoch"] for slot in state["sponsorships"].values()}, {1})
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM sponsorship_transitions").fetchone()[0], 2)
        self.assertEqual(len([call for call in self.metadata_calls if call[1] == AT]), 1)

    def test_self_withdraw_without_prior_grant_blocks_only_own_start(self):
        """A pre-task withdrawal is accepted after writer loss and survives comment deletion."""
        withdrawal = comment(2, "/concurro withdraw openai", "writer", "2026-11-12T10:00:00Z")
        self.deliver_source(withdrawal)
        self.deliver_source(withdrawal, "deleted")
        state = self.advance()
        self.assertIsNone(state["sponsorships"]["openai"]["sponsor_id"])
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 0)
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")

    def test_permission_loss_fences_all_prior_slots_without_touching_new_owner(self):
        """One atomic loss event covers former/current consent while another provider owner is preserved."""
        self.fake.comments += [
            comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"),
            comment(3, "/concurro sponsor anthropic", "alice", "2026-11-12T10:00:01Z"),
        ]
        self.advance()
        self.fake.comments.append(comment(20, "/concurro sponsor anthropic", "bob", "2026-11-12T10:01:00Z"))
        state = self.advance()
        other = deepcopy(state["sponsorships"]["anthropic"])
        self.allowed.remove("alice")
        with patch.object(self.exchange, "sponsorship", wraps=self.exchange.sponsorship) as publish:
            state = self.advance()
        batches = [call.args[0]["transitions"] for call in publish.call_args_list]
        self.assertTrue(
            any(
                len(batch) == 2 and all(t["actor"] == "202" and t["reason"] == "permission_loss" for t in batch)
                for batch in batches
            )
        )
        self.assertEqual(state["sponsorships"]["anthropic"], other)
        self.assertEqual(state["sponsorships"]["openai"]["status"], "withdrawn")
        self.assertEqual(
            {r[0] for r in self.exchange.db.execute("SELECT provider FROM sponsorship_revocations WHERE actor='202'")},
            {"openai", "anthropic"},
        )

    def test_legacy_org_checkpoint_never_infers_an_old_starter_grant(self):
        """Old org attempts stay historical; only observed explicit future consent is adopted."""
        with patch.object(
            self, "controller", side_effect=lambda generation: ServiceFixture.controller(self, generation)
        ):
            state = self.advance()
        old_calls = state["calls"]
        self.fake.comments.append(comment(20, "/concurro sponsor openai", "alice", "2026-11-12T10:01:00Z"))
        state = self.advance()
        self.assertEqual(state["funding_initialization"]["boundary"]["provenance"], "legacy")
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertIsNone(state["sponsorships"]["anthropic"]["sponsor_id"])
        self.assertGreaterEqual(state["calls"], old_calls)
        self.assertFalse(any(at == AT for _, at in self.metadata_calls))

    def test_source_delivery_during_read_is_not_frozen_as_empty(self):
        """The local receipt revision fences an authoritative read even when its webhook wins the race."""
        actual = self.shadow.confirm
        raced = False

        def confirm(*args, **kwargs):
            """Insert a source after the captured read fence and before projection publication."""
            nonlocal raced
            if not raced:
                raced = True
                source = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
                self.fake.comments.append(source)
                self.deliver_source(source)
            return actual(*args, **kwargs)

        with patch.object(self.shadow, "confirm", side_effect=confirm):
            state = self.advance()
        self.assertNotIn("funding_initialization", state)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_worker_acknowledges_only_consumed_source_revision(self):
        """The production worker preserves the exact confirmed projection in its acknowledgment."""
        self.backend.files = target_files(self.config)
        self.service()
        self.worker.sponsorship = self.shadow
        self.registry.enqueue(KEY, "test", self.clock())
        self.scheduler.dispatch()
        state = self.state()
        self.assertEqual(
            self.registry.acknowledged(KEY)["sponsorship"]["revision"], state["sponsorship_projection_revision"]
        )
        self.assertTrue(all(value["kind"] == "org" for value in state["credentials"].values()))

    def test_same_second_explicit_claims_and_revisions_have_source_precedence(self):
        """Comment IDs break equal-time claims; prior-body evidence orders same-comment route edits."""
        original = comment(2, "/concurro sponsor openai", "alice", AT)
        edited = {**original, "body": "/concurro sponsor anthropic"}
        later = comment(3, "/concurro sponsor openai", "bob", AT)
        self.fake.comments += [edited, later]
        self.deliver_source(edited, "edited", original["body"])
        self.deliver_source(original)
        self.deliver_source(later)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "303")
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "202")

    def test_late_withdrawal_cannot_defeat_later_explicit_reoptin(self):
        """An earlier unseen withdrawal is audit-only after that actor's later accepted provider grant."""
        self.advance()
        self.fake.comments.append(comment(20, "/concurro sponsor openai", "writer", "2026-11-12T10:01:00Z"))
        state = self.advance()
        slot = deepcopy(state["sponsorships"]["openai"])
        self.fake.comments.append(comment(4, "/concurro withdraw openai", "writer", "2026-11-12T10:00:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"], slot)
        self.assertTrue(state["sponsorship_ordering_diagnostics"])

    def test_autonomous_label_uses_stable_actor_and_exact_source_boundary(self):
        """Label enrollment retains its independent execution authority and source identity."""
        self.config["autonomous"] = True
        self.fake.events = [
            {
                "id": 2,
                "event": "labeled",
                "label": {"name": "concurro"},
                "actor": {"id": 101, "login": "writer"},
                "created_at": AT,
            }
        ]
        self.fake.comments = []
        state = self.advance()
        self.assertEqual(state["enrollment"]["actor_id"], 101)
        self.assertEqual(
            state["funding_initialization"]["boundary"],
            {
                "source_id": 2,
                "actor_id": "101",
                "login": "writer",
                "at": AT,
                "provenance": "label",
                "source_policy": {"funding_mode": "unknown", "config_digest": None},
            },
        )

    def test_actor_rename_is_resolved_by_stable_id(self):
        """Permission checks use the current login returned for the recorded actor ID."""
        seen = []

        def read(method, path):
            """Supply renamed identity and its independent collaborator result."""
            seen.append(path)
            return (
                {"id": 202, "login": "renamed"}
                if path.startswith("user/")
                else {"permission": "write", "user": {"id": 202}}
            )

        with patch.object(self.shadow, "reader", return_value=SimpleNamespace(request=read)):
            self.assertTrue(self.shadow.writer(KEY[0], "202", "alice"))
        self.assertIn("collaborators/renamed/permission", seen[-1])

    def test_backoffice_receipt_fences_initialization_before_webhook_arrives(self):
        """A known App POST expectation cannot be skipped merely because its hook is delayed."""
        body = delegation.render(KEY_BYTES, KEY[0], 7, "alice", 202, "/concurro sponsor openai", "b" * 32)
        self.registry.request_observation(KEY, comment={"id": 2, "body": body, "updated_at": "2026-11-12T10:00:00Z"})
        self.assertEqual(self.registry.sponsorship(KEY)["status"], "provisional")
        state = self.advance()
        self.assertNotIn("funding_initialization", state)
        source = comment(2, body, at="2026-11-12T10:00:00Z")
        source["user"] = {"login": self.config["bot_login"], "id": 999}
        self.fake.comments.append(source)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_bare_sponsor_replies_without_enrolling_or_allocating(self):
        """Pre-task help has an App marker for replay and creates no grant, checkpoint or model call."""
        self.fake.comments = [comment(2, "/concurro sponsor", "alice")]
        self.fake.issue["labels"] = []
        self.assertIsNone(self.advance())
        self.assertEqual(len(self.fake.comments), 2)
        self.assertIn("/concurro funding <provider> personal", self.fake.comments[-1]["body"])
        self.assertIsNone(self.advance())
        self.assertEqual(len(self.fake.comments), 2)
        self.assertEqual(self.agents.calls, [])

    def test_unknown_edit_history_keeps_source_projection_provisional(self):
        """An edited command without its original revision cannot confirm or initialize consent."""
        original = comment(2, "/concurro sponsor anthropic", "alice", "2026-11-12T10:00:00Z")
        edited = {**original, "body": "/concurro sponsor openai", "updated_at": "2026-11-12T10:00:05Z"}
        self.fake.comments.append(edited)
        state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(self.registry.sponsorship(KEY)["status"], "provisional")
        self.deliver_source(original)
        self.deliver_source(edited, "edited", original["body"])
        state = self.advance()
        self.assertEqual(self.registry.sponsorship(KEY)["status"], "confirmed")
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_metadata_client_rejects_other_owner_and_sanitizes_errors(self):
        """A misrouted private response cannot create coverage or expose response fields."""
        client = Metadata("http://vault.invalid", "unused-fixture-path")
        for record in ({"user_id": "202", "secret": "synthetic-provider-secret"}, "synthetic-provider-secret"):
            body = io.BytesIO(json.dumps({"history_complete": True, "credentials": [record]}).encode())
            with (
                patch("service.shadow.private.read", return_value=b"A" * 64),
                patch("service.shadow.urllib.request.urlopen", return_value=body),
            ):
                with self.assertRaisesRegex(Transient, "^GitHub unavailable; retry later[.]$"):
                    client.read("101")
        body = io.BytesIO(
            json.dumps(
                {
                    "history_complete": True,
                    "credentials": [
                        {
                            "user_id": "101",
                            "route": "openai-api-key",
                            "models": [],
                            "secret": "synthetic-provider-secret",
                        }
                    ],
                }
            ).encode()
        )
        with (
            patch("service.shadow.private.read", return_value=b"A" * 64),
            patch("service.shadow.urllib.request.urlopen", return_value=body) as read,
        ):
            records = client.read("101")
        self.assertEqual(records[0]["user_id"], "101")
        self.assertNotIn("synthetic-provider-secret", str(records))
        self.assertEqual(read.call_args.args[0].get_header("Authorization"), "Bearer " + "a" * 64)
        with patch("service.shadow.private.read", side_effect=Blocked("private path detail")):
            with self.assertRaisesRegex(Transient, "^GitHub unavailable; retry later[.]$"):
                client.read("101")

    def test_selected_legacy_model_evidence_waits_instead_of_freezing_empty(self):
        """A valid old vault row without model evidence is unknown at the initiating boundary."""
        self.metadata[0]["models"] = None
        state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(state["funding_initialization_pending"]["at"], AT)
        self.assertEqual(state["sponsorship_status"], "pending")
        self.metadata[0]["models"] = [self.config["models"]["codex"]]
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")

    def test_metadata_read_source_race_cannot_freeze_a_stale_initializer(self):
        """A prior explicit source received during vault I/O invalidates the once-only fill."""
        actual = self.read_metadata
        raced = False

        def read(actor, at=None):
            """Commit a real source hint between confirmation and the metadata reply."""
            nonlocal raced
            if at == AT and not raced:
                raced = True
                source = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
                self.fake.comments.append(source)
                self.deliver_source(source)
            return actual(actor, at)

        with patch.object(self.shadow.metadata, "read", side_effect=read):
            state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(state["funding_initialization_pending"]["at"], AT)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_edit_from_noncommand_has_a_complete_source_chain(self):
        """A signed edit's non-command previous digest proves there was no earlier provider grant."""
        original = comment(2, "A plain issue reply", "alice", "2026-11-12T10:00:00Z")
        edited = {**original, "body": "/concurro sponsor openai", "updated_at": "2026-11-12T10:00:05Z"}
        self.fake.comments.append(edited)
        self.deliver_source(edited, "edited", original["body"])
        state = self.advance()
        self.assertEqual(self.registry.sponsorship(KEY)["status"], "confirmed")
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_removing_superseded_source_does_not_withdraw_a_later_grant(self):
        """A pre-freeze removal targets its exact comment, not the actor's newer consent."""
        original = comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z")
        newer = comment(3, "/concurro sponsor openai", "alice", "2026-11-12T10:00:05Z")
        self.fake.comments.append(newer)
        self.deliver_source(original)
        self.deliver_source(original, "deleted")
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["source_id"], 3)
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM sponsorship_revocations").fetchone()[0], 0)

    def test_permission_loss_fences_delayed_pre_loss_claim_but_allows_new_optin(self):
        """An observation-time cutoff ignores deletion sentinels and cannot resurrect stale consent."""
        removed = comment(3, "/concurro sponsor anthropic", "bob", "2026-11-12T10:00:01Z")
        self.deliver_source(removed)
        self.deliver_source(removed, "deleted")
        self.fake.comments.append(comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:00Z"))
        state = self.advance()
        self.clock.now = datetime.fromisoformat("2026-11-12T10:02:00+00:00").timestamp()
        self.allowed.remove("alice")
        state = self.advance()
        other = deepcopy(state["sponsorships"]["anthropic"])
        revoked = deepcopy(state["sponsorships"]["openai"])
        self.allowed.add("alice")
        self.fake.comments.append(comment(4, "/concurro sponsor openai", "alice", "2026-11-12T10:01:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"], revoked)
        self.fake.comments.append(comment(5, "/concurro sponsor openai", "alice", "2026-11-12T10:03:00Z"))
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(state["sponsorships"]["anthropic"], other)

    def test_missing_configuration_waits_without_self_scheduling(self):
        """Default org-only configuration keeps a pending boundary without a recurring tick."""
        self.config.pop("credential_routes")
        self.shadow.metadata = None
        with patch.object(self.registry, "enqueue", wraps=self.registry.enqueue) as enqueue:
            state = self.advance()
            state = self.advance()
        self.assertIn("funding_initialization_pending", state)
        self.assertFalse(any(call.args[1] == "shadow-reconcile" for call in enqueue.call_args_list))

    def test_transient_shadow_retries_grow_and_recovery_clears_pending(self):
        """Bookkeeping uses durable failure counts and shared backoff without blocking org execution."""
        with (
            patch.object(self.shadow.metadata, "read", side_effect=Transient("fixture outage")),
            patch.object(self.registry, "enqueue", wraps=self.registry.enqueue) as enqueue,
        ):
            for _ in range(3):
                state = self.advance()
        retries = [call.args[2] - self.clock() for call in enqueue.call_args_list if call.args[1] == "shadow-reconcile"]
        self.assertEqual(retries, [60, 120, 240])
        self.assertEqual(state["sponsorship_retry_attempts"], 3)
        state = self.advance()
        self.assertEqual(state["sponsorship_status"], "confirmed")
        self.assertNotIn("sponsorship_retry_attempts", state)

    def test_nonwriter_bare_usage_does_not_post_an_app_reply(self):
        """A status command does not widen the existing writer-command posting authority."""
        self.fake.comments = [comment(2, "/concurro sponsor", "alice")]
        self.fake.issue["labels"] = []
        self.allowed.remove("alice")
        self.assertIsNone(self.advance())
        self.assertFalse(any("concurro:sponsorship-usage" in c["body"] for c in self.fake.comments))

    def test_targeted_observation_discovers_intent_without_hook_or_empty_index_growth(self):
        """Existing observation/snapshot scheduling discovers missed sources without model calls."""
        self.fake.comments = []
        self.fake.issue["labels"] = []
        self.shadow.confirm(KEY, [], [], self.config["bot_login"], config=self.config)
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [])
        self.backend.files = target_files(self.config)
        self.service()
        self.worker.sponsorship = self.shadow
        self.fake.comments.append(comment(2, "/concurro sponsor openai", "alice"))
        self.registry.request_observation(KEY)
        self.reconciler.observe_pending()
        self.scheduler.dispatch()
        self.assertEqual(self.registry.sponsorship(KEY)["slots"]["openai"]["sponsor_id"], "202")
        self.assertEqual(self.agents.calls, [])

    def test_human_edit_consent_belongs_to_editor_not_original_comment_author(self):
        """Another editor cannot turn a human's plain reply into that human's sponsorship."""
        original = comment(2, "A plain issue reply", "alice", "2026-11-12T10:00:00Z")
        edited = {**original, "body": "/concurro sponsor openai", "updated_at": "2026-11-12T10:00:05Z"}
        self.fake.comments.append(edited)
        self.deliver_source(edited, "edited", original["body"], editor=303)
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")
        self.assertFalse(any(f["actor_id"] == "202" for f in self.registry.sponsorship(KEY)["facts"]))

    def test_unknown_human_editor_keeps_history_pending(self):
        """An unobserved edit cannot borrow the original comment author's provider consent."""
        original = comment(2, "A plain issue reply", "alice", "2026-11-12T10:00:00Z")
        edited = {**original, "body": "/concurro sponsor openai", "updated_at": "2026-11-12T10:00:05Z"}
        self.fake.comments.append(edited)
        self.deliver_source(edited, "edited", original["body"], editor=0)
        state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(self.registry.sponsorship(KEY)["status"], "provisional")

    def test_terminal_task_still_records_self_withdrawal(self):
        """Terminal scheduling does not skip the owner-bound consent withdrawal receipt."""
        self.advance()
        generation = self.gateway.acquire(KEY, "terminal-fixture")
        controller = self.controller(generation)
        controller.issue = controller.gh.repo("issues/7")
        controller.load(controller.gh.pages("issues/7/comments"))
        controller.state["stage"] = "closed"
        controller.save()
        self.gateway.release(KEY, generation, controller.outcome(False))
        state = self.state()
        self.assertEqual(state["stage"], "closed")
        self.assertEqual(self.registry.sponsor_issues(KEY[0], active=True), [])
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [])
        calls = state["calls"]
        other = deepcopy(state["sponsorships"]["anthropic"])
        self.fake.base = ENGINE
        self.backend.files = target_files(self.config)
        self.service()
        self.worker.sponsorship = self.shadow
        self.registry.acknowledge(KEY, {**self.reconciler.snapshot(KEY, cached=False), "_stage": "closed"})
        source = comment(3, "/concurro withdraw openai", "writer", "2026-11-12T10:01:00Z")
        self.fake.comments.append(source)
        self.deliver_source(source)
        self.assertEqual(self.registry.due(self.clock()), [])
        self.reconciler.observe_pending()
        self.assertEqual(self.scheduler.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["sponsorships"]["openai"]["status"], "withdrawn")
        self.assertEqual(state["calls"], calls)
        self.assertEqual(state["sponsorships"]["anthropic"], other)
        self.fake.comments.append(comment(4, "An ordinary reply", "writer", "2026-11-12T10:02:00Z"))
        self.assertFalse(self.reconciler.observe(KEY, cached=False))
        self.assertEqual(self.registry.due(self.clock()), [])

    def test_author_edited_start_uses_effective_time_and_observed_editor(self):
        """A legitimate start edit uses its new source boundary and preserves prior explicit priority."""
        edited = {
            **comment(1, "/concurro start", at="2026-11-12T10:00:00Z"),
            "updated_at": AT,
            "author_association": "COLLABORATOR",
        }
        self.fake.comments = [edited, comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:05Z")]
        self.deliver_source(edited, "edited", "A plain issue reply")
        state = self.advance()
        self.assertEqual(state["funding_initialization"]["boundary"]["at"], AT)
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(state["sponsorships"]["openai"]["epoch"], 1)
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")

    def test_unknown_start_editor_waits_until_source_evidence_arrives(self):
        """Unknown initiator provenance cannot become an empty once-only fill, and can reconcile later."""
        edited = {
            **comment(1, "/concurro start", at="2026-11-12T10:00:00Z"),
            "updated_at": AT,
            "author_association": "COLLABORATOR",
        }
        self.fake.comments = [edited]
        state = self.advance()
        self.assertNotIn("funding_initialization", state)
        self.assertEqual(state["funding_initialization_pending"]["at"], AT)
        self.assertTrue(state["calls"])
        self.deliver_source(edited, "edited", "A plain issue reply")
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")

    def test_unknown_start_editor_does_not_block_full_explicit_coverage(self):
        """Explicit provider consent needs no implicit initiator grant or credential lookup."""
        edited = {**comment(1, "/concurro start", at="2026-11-12T10:00:00Z"), "updated_at": AT}
        self.fake.comments = [
            edited,
            comment(2, "/concurro sponsor openai", "alice", "2026-11-12T10:00:05Z"),
            comment(3, "/concurro sponsor anthropic", "bob", "2026-11-12T10:00:06Z"),
        ]
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "202")
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "303")
        self.assertFalse(any(at == AT for _, at in self.metadata_calls))

    def test_unrelated_edited_reply_does_not_require_sponsorship_editor_evidence(self):
        """Editor proof is scoped to consent-bearing revisions, not ordinary issue discussion."""
        self.fake.comments.append(
            {**comment(2, "An ordinary reply", "alice", "2026-11-12T10:00:00Z"), "updated_at": "2026-11-12T10:00:05Z"}
        )
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")
        self.assertEqual(state["sponsorship_status"], "confirmed")

    def test_signed_delegated_start_preserves_its_verified_actor_after_app_edit(self):
        """A valid delegated envelope supplies initiator identity independently of the App editor."""
        body = delegation.render(KEY_BYTES, KEY[0], 7, "writer", 101, "/concurro start", "c" * 32)
        signed = {
            **comment(1, body, at="2026-11-12T10:00:00Z"),
            "updated_at": AT,
            "user": {"id": 999, "login": self.config["bot_login"]},
        }
        self.fake.comments = [signed]
        state = self.advance()
        self.assertTrue(state["funding_initialization"]["boundary"]["initiator_revision"]["delegated"])
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")

    def test_permanent_exchange_conflict_waits_for_input_without_self_retry(self):
        """A permanent fence conflict preserves exact pending receipts without recurring scheduling."""
        operator = SimpleNamespace(
            sponsorship=lambda body: (_ for _ in ()).throw(ExchangeError(409, "fixture conflict"))
        )
        with (
            patch.object(self.shadow, "operator", return_value=operator),
            patch.object(self.registry, "enqueue", wraps=self.registry.enqueue) as enqueue,
        ):
            state = self.advance()
            state = self.advance()
        self.assertTrue(state["pending_sponsorships"])
        self.assertNotIn("sponsorship_retry_attempts", state)
        self.assertFalse(any(call.args[1] == "shadow-reconcile" for call in enqueue.call_args_list))

    def test_transient_existing_projection_recovers_without_a_new_transition(self):
        """Confirmed status returns after an I/O failure even when both provider epochs stay unchanged."""
        self.advance()
        self.fake.comments.append(comment(20, "/concurro stop", "writer", "2026-11-12T10:01:00Z"))
        state = self.advance()
        slots = deepcopy(state["sponsorships"])
        with patch.object(self.shadow, "confirm", side_effect=Transient("fixture read outage")):
            state = self.advance()
        self.assertEqual(state["sponsorship_status"], "pending")
        state = self.advance()
        self.assertEqual(state["sponsorship_status"], "confirmed")
        self.assertEqual(state["sponsorships"], slots)
        self.assertNotIn("sponsorship_retry_attempts", state)

    def parked_worker(self):
        """Run real initialization and a stop through the worker, leaving no due model stage."""
        self.backend.files = target_files(self.config)
        self.fake.comments.append(comment(2, "/concurro stop", "writer", "2026-11-12T10:00:20Z"))
        self.service()
        self.worker.sponsorship = self.shadow
        self.registry.enqueue(KEY, "fixture-start", self.clock())
        self.drain()
        self.assertEqual(self.state()["stage"], "stopped")
        return self.state()

    def test_polling_acknowledges_observed_incomplete_history_without_adopting_it(self):
        """Permanent source uncertainty settles polling but a new verified revision still wakes it."""
        original = self.parked_worker()
        source = {**comment(3, "/concurro sponsor openai", "alice", AT), "updated_at": "2026-11-12T10:01:00Z"}
        self.fake.comments.append(source)
        self.assertTrue(self.reconciler.observe(KEY, cached=False))
        self.scheduler.dispatch()
        state = self.state()
        self.assertEqual(state["sponsorship_status"], "pending")
        self.assertEqual(state["sponsorships"], original["sponsorships"])
        self.assertEqual(self.registry.acknowledged(KEY)["sponsorship"]["status"], "provisional")
        self.assertFalse(self.reconciler.observe(KEY, cached=False))
        self.assertEqual(self.registry.due(self.clock()), [])
        self.deliver_source(source, "edited", "An earlier plain reply")
        self.reconciler.observe_pending()
        self.assertEqual(self.scheduler.dispatch(), [KEY])
        self.assertEqual(self.state()["sponsorships"]["openai"]["sponsor_id"], "202")

    def test_polling_settles_permanent_post_initialization_transition_conflict(self):
        """A read projection is acknowledged separately from the exact unapplied provider transition."""
        original = self.parked_worker()
        source = comment(3, "/concurro sponsor openai", "alice", "2026-11-12T10:01:00Z")
        self.fake.comments.append(source)
        self.deliver_source(source)
        self.reconciler.observe_pending()
        operator = SimpleNamespace(
            sponsorship=lambda body: (_ for _ in ()).throw(ExchangeError(409, "fixture conflict"))
        )
        with patch.object(self.shadow, "operator", return_value=operator):
            self.scheduler.dispatch()
        state = self.state()
        self.assertEqual(state["sponsorships"], original["sponsorships"])
        self.assertEqual(state["pending_sponsorships"]["openai"]["request"]["slot"]["sponsor_id"], "202")
        self.assertFalse(self.reconciler.observe(KEY, cached=False))
        self.assertEqual(self.registry.due(self.clock()), [])

    def test_hint_only_nonwriter_issue_leaves_discovery_after_confirmation(self):
        """Provisional intent stays API-visible, but an empty confirmed source never becomes a poll target."""
        source = comment(3, "/concurro sponsor openai", "alice")
        self.fake.comments = [source]
        self.fake.issue["labels"] = []
        self.allowed.remove("alice")
        self.deliver_source(source)
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [7])
        self.assertEqual(self.registry.sponsor_issues(KEY[0], active=True), [])
        self.assertIsNone(self.advance())
        self.assertEqual(self.registry.sponsorship(KEY)["status"], "confirmed")
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [])
        self.assertEqual(self.registry.sponsor_issues(KEY[0], active=True), [])

    def test_polling_preserves_shadow_retry_when_confirmation_itself_is_unavailable(self):
        """The pre-tick observation settles polling without claiming new consent was adopted."""
        original = self.parked_worker()
        source = comment(3, "/concurro sponsor openai", "alice", "2026-11-12T10:01:00Z")
        self.fake.comments.append(source)
        self.deliver_source(source)
        self.reconciler.observe_pending()
        with patch.object(self.shadow, "confirm", side_effect=Transient("fixture permission outage")):
            self.scheduler.dispatch()
        self.assertEqual(self.state()["sponsorships"], original["sponsorships"])
        self.assertEqual(self.registry.acknowledged(KEY)["sponsorship"]["status"], "provisional")
        self.assertFalse(self.reconciler.observe(KEY, cached=False))
        self.assertEqual(self.registry.due(self.clock()), [])
        self.assertEqual(self.state()["sponsorship_retry_attempts"], 1)

    def test_empty_discovery_tick_does_not_become_an_active_poll_target(self):
        """A stateless read leaves no recurring task, while later targeted discovery still finds consent."""
        self.fake.comments = []
        self.fake.issue["labels"] = []
        self.backend.files = target_files(self.config)
        self.service()
        self.worker.sponsorship = self.shadow
        self.registry.request_observation(KEY)
        self.reconciler.observe_pending()
        self.scheduler.dispatch()
        self.assertEqual(self.registry.outcome(KEY).kind, "await-human")
        self.assertEqual(self.reconciler.candidates(KEY[0]), [])
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [])
        self.registry.request_observation(KEY)
        self.reconciler.observe_pending()
        self.assertEqual(self.scheduler.dispatch(), [])
        self.fake.comments.append(comment(3, "/concurro sponsor openai", "alice"))
        self.registry.request_observation(KEY)
        self.reconciler.observe_pending()
        self.assertEqual(self.scheduler.dispatch(), [KEY])
        self.assertEqual(self.registry.sponsorship(KEY)["slots"]["openai"]["sponsor_id"], "202")
        self.assertEqual(self.reconciler.candidates(KEY[0]), [KEY])
        self.assertEqual(self.agents.calls, [])

    def test_ordinary_command_watch_survives_stateless_shadow_observation(self):
        """Existing help/command watches still discover a later START through normal polling."""
        self.fake.comments = [comment(1, "/concurro help")]
        self.fake.issue["labels"] = []
        self.backend.files = target_files(self.config)
        self.service()
        self.worker.sponsorship = self.shadow
        self.registry.enqueue(KEY, "webhook:issue_comment", self.clock())
        self.drain()
        self.assertIsNone(self.registry.binding(KEY))
        self.assertEqual(self.reconciler.candidates(KEY[0]), [KEY])
        self.fake.comments.append(comment(2, "/concurro start", at="2026-11-12T10:01:00Z"))
        self.reconciler.poll()
        self.drain()
        self.assertIsNotNone(self.registry.binding(KEY))
