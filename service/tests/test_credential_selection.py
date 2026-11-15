"""List selection through the real source, controller, shadow and Exchange fixtures."""

import unittest
from copy import deepcopy

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import admissions
from policy import Blocked, digest
from test_sponsored_admissions import AdmissionTests


class SelectionTests(AdmissionTests):
    """Bob sponsors Anthropic with the proposal's example list; Alice sponsors OpenAI."""

    def setUp(self):
        super().setUp()
        self.exchange.gateway.contributed_routes += ("claude-subscription",)
        self.metadata = [row for row in self.metadata if (row["user_id"], row["route"]) != ("303", "anthropic-api-key")]
        self.target = self.config.get("upstream", KEY[0])
        self.credits = self.entry("credits", "anthropic-api-key", 0, roles=["planner"])
        self.personal = self.entry("personal", "claude-subscription", 1)
        self.work = self.entry("work", "claude-subscription", 2, version=2)
        self.sponsors()

    def entry(self, name, route, position, version=1, **policy):
        """Add one of Bob's Anthropic list entries with its own record identity."""
        row = {
            "user_id": "303",
            "route": route,
            "version": version,
            "record_id": digest(["303", route, version]),
            "status": "valid",
            "models": None,
            "expiry_known": False,
            "expires_at": None,
            "name": name,
            "entry_id": name,
            "position": position,
            **policy,
        }
        self.metadata.append(row)
        return row

    def choose(self, controller, stage="plan", profile="claude"):
        return controller.select_admissions([(profile, stage, {})])[0]

    def test_example_list_selects_by_role_then_order_and_follows_edits(self):
        with self.current() as controller:
            plan = self.choose(controller)
            self.assertEqual((plan["route"], plan["sponsor"]), ("anthropic-api-key", "303"))
            selection = controller.state["credential_selections"][plan["credential"]["id"]]
            self.assertEqual(
                selection, {"entry_id": "credits", "entry": "credits", "list_revision": 1, "target": self.target}
            )
            implement = self.choose(controller, "develop")
            self.assertEqual(implement["credential"]["record_id"], self.personal["record_id"])
            # An admitted choice keeps its exact record; later list edits change only later selections.
            frozen = admissions.freeze(plan, controller.state["calls"] + 1, controller.agents.generation)
            self.credits["enabled"] = False
            self.assertEqual(frozen["credential"]["record_id"], self.credits["record_id"])
            self.assertEqual(self.choose(controller)["credential"]["record_id"], self.personal["record_id"])
            self.personal["position"], self.work["position"] = 2, 1
            later = self.choose(controller)
            self.assertEqual(
                (later["credential"]["record_id"], later["credential"]["version"]), (self.work["record_id"], 2)
            )
            self.credits.update(enabled=True, profiles=[{"target": "other/project", "profile": "claude"}])
            self.assertEqual(self.choose(controller)["credential"]["record_id"], self.work["record_id"])
            self.credits["profiles"] = [{"target": self.target, "profile": "claude"}]
            self.assertEqual(self.choose(controller)["credential"]["record_id"], self.credits["record_id"])

    def test_selection_needs_no_target_route_settings(self):
        self.config.pop("credential_routes", None)
        self.config.pop("provider_credential_routes", None)
        with self.current() as controller:
            for name in ("credential_routes", "provider_credential_routes"):
                controller.config.pop(name, None)
            self.assertEqual(self.choose(controller)["route"], "anthropic-api-key")
            self.assertEqual(self.choose(controller, "intake", "codex")["route"], "openai-api-key")

    def test_no_match_pauses_only_that_call_and_never_switches_sponsor_or_org(self):
        for row in (self.credits, self.personal, self.work):
            row["roles"] = ["triage"]
        with self.current() as controller:
            calls = controller.state["calls"]
            self.assertEqual(self.choose(controller, "intake", "codex")["sponsor"], "202")
            with self.assertRaisesRegex(Blocked, "no matching enabled credential") as error:
                self.choose(controller)
            self.assertEqual(error.exception.pause_stage, "awaiting_credentials")
            self.assertEqual(
                {k: error.exception.requirement[k] for k in ("provider", "role", "profile", "route")},
                {"provider": "anthropic", "role": "planner", "profile": "claude", "route": None},
            )
            self.assertEqual(controller.state["calls"], calls)

    def test_exchange_admits_the_frozen_record_after_its_entry_is_disabled_or_filtered(self):
        with self.current() as controller:
            plan = self.choose(controller)
        binding = {
            "funding": "contributed",
            "route": plan["route"],
            "sponsor": "303",
            "credential": plan["credential"]["record_id"],
            "credential_version": plan["credential"]["version"],
        }
        self.credits.update(enabled=False, roles=["triage"], position=2)
        self.exchange.metadata_for(binding)
        # Removal revokes the record itself, which no admission survives.
        self.credits.update(status="revoked", position=None)
        with self.assertRaises(Exception) as refused:
            self.exchange.metadata_for(binding)
        self.assertEqual(getattr(refused.exception, "status", None), 410)

    def test_attempt_records_its_selected_entry_beside_the_unchanged_admission(self):
        with self.current() as controller:
            plan = self.choose(controller)
            context = controller.prepare_context("claude", "plan", {"purpose": "selection explanation"})
            operation = controller.operation("claude", "plan", context)
            frozen = admissions.freeze(plan, controller.state["calls"] + 1, controller.agents.generation)
            controller.begin_attempt(operation, context, frozen)
            self.assertEqual(operation["credential_selection"]["entry_id"], "credits")
            self.assertEqual(operation["admission"], frozen)
            self.assertNotIn("selection", operation["admission"])


class InitializationTests(AdmissionTests):
    """A boundary governed by entry lists fills the initiator from their list, not the target's route."""

    def boundary(self, *rows):
        self.listed_boundary = True
        self.metadata = [row for row in self.metadata if row["user_id"] != "101"] + [
            {"user_id": "101", "version": 1, "status": "valid", "models": None, **row} for row in rows
        ]
        return self.advance()

    def test_initiator_fills_from_a_listed_subscription_the_target_never_named(self):
        state = self.boundary({"route": "openai-api-key"}, {"route": "claude-subscription"})
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")
        available = {(r["provider"], r["route"]) for r in state["funding_initialization"]["available"]}
        self.assertIn(("anthropic", "claude-subscription"), available)
        self.assertNotIn(("anthropic", "anthropic-api-key"), available)

    def test_disabled_or_filtered_out_entries_do_not_make_the_initiator_a_sponsor(self):
        state = self.boundary(
            {"route": "openai-api-key", "enabled": False},
            {"route": "anthropic-api-key", "profiles": [{"target": "other/project", "profile": "claude"}]},
        )
        self.assertFalse(state["sponsorships"]["openai"].get("sponsor_id"))
        self.assertFalse(state["sponsorships"]["anthropic"].get("sponsor_id"))

    def test_removed_record_of_unknown_history_does_not_block_a_valid_entry_on_its_route(self):
        """A migrated tombstone shares the route; only the matched entry is initialization evidence."""
        state = self.boundary(
            {"route": "openai-api-key"},
            {"route": "anthropic-api-key", "status": "unknown", "position": None, "enabled": False, "removed": True},
            {"route": "anthropic-api-key", "version": 2, "position": 0},
        )
        self.assertEqual(state["sponsorships"]["anthropic"]["sponsor_id"], "101")
        evidence = [r for r in state["funding_initialization"]["metadata"] if r["route"] == "anthropic-api-key"]
        self.assertEqual([(r["version"], r["status"]) for r in evidence], [(2, "valid")])

    def test_route_era_boundary_keeps_the_frozen_route_semantics(self):
        self.metadata = [row for row in self.metadata if (row["user_id"], row["route"]) != ("101", "anthropic-api-key")]
        self.metadata.append(
            {"user_id": "101", "route": "claude-subscription", "version": 1, "status": "valid", "models": None}
        )
        state = self.advance()
        self.assertEqual(state["sponsorships"]["openai"]["sponsor_id"], "101")
        self.assertFalse(state["sponsorships"]["anthropic"].get("sponsor_id"))
        evidence = deepcopy(state["funding_initialization"]["metadata"])
        self.assertTrue(all("entry_id" not in record and "list_revision" not in record for record in evidence))


def load_tests(loader, tests, pattern):
    """Run these scenarios once while reusing the admission fixture and its helpers."""
    return unittest.TestSuite(
        case(name)
        for case in (SelectionTests, InitializationTests)
        for name in case.__dict__
        if name.startswith("test_")
    )
