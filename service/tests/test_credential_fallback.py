"""Automatic fallback through the real queue, controller, registry and Exchange, with synthetic provider outcomes."""

import sys
import unittest
from copy import deepcopy
from unittest.mock import patch

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from policy import digest
from relay import ExchangeError
from service import credential_recovery
from test_credential_recovery import RecoveryTests
from test_shadow import comment


class FallbackTests(RecoveryTests):
    """Alice sponsors OpenAI with an ordered list of two entries; the first one fails."""

    ROUTES = ("openai-api-key", "chatgpt-subscription")

    def setUp(self):
        super().setUp()
        self.exchange.gateway.contributed_routes += ("chatgpt-subscription",)
        self.first = next(r for r in self.metadata if r["user_id"] == "202" and r["route"] == "openai-api-key")
        self.first.update(
            route=self.ROUTES[0], kind=self.kind(self.ROUTES[0]), name="first", entry_id="first", position=0
        )
        self.second = self.entry("second", self.ROUTES[1], 1)

    @staticmethod
    def kind(route):
        return "subscription" if route.endswith("subscription") else "api_key"

    def entry(self, name, route, position, version=1):
        """Add another of Alice's OpenAI entries with its own record identity."""
        row = {
            "user_id": "202",
            "route": route,
            "kind": self.kind(route),
            "version": version,
            "record_id": digest(["202", name, version]),
            "status": "valid",
            "models": None,
            "expiry_known": False,
            "expires_at": None,
            "name": name,
            "entry_id": name,
            "position": position,
        }
        self.metadata.append(row)
        return row

    def run_until_quiet(self, limit=8):
        """Dispatch due work until nothing is queued for now."""
        for _ in range(limit):
            if not self.dispatch():
                return self.state()
        raise AssertionError("task kept cycling")

    def resume(self, limit=4):
        """Dispatch due work until the paused call has been attempted again or nothing is due."""
        calls = self.state()["calls"]
        for _ in range(limit):
            if not self.dispatch() or self.state()["calls"] > calls:
                break
        return self.state()

    def start(self):
        self.registry.enqueue(KEY, "fixture-start", self.clock())
        self.assertEqual(self.dispatch(), [KEY])
        return self.state()

    def charged(self, state=None):
        """Owner, route, version and calls of each personal credential the task charged."""
        return sorted(
            (r["account"], r["route"], r["version"], r["calls"])
            for r in (state or self.state())["credentials"].values()
            if r["kind"] == "contributed"
        )

    def test_failed_first_entry_falls_back_at_once_without_a_vault_event(self):
        """The alternative already listed is selected on the next owned tick; the ledger keeps both attempts."""
        paused = self.start()
        self.assertEqual((paused["stage"], paused["calls"]), ("awaiting_credentials", 1))
        plan = paused["credential_recovery"]
        self.assertEqual((plan["candidate"]["entry_id"], plan["wake_at"], plan["eligible"]), ("second", None, True))
        failed_session = self.served[0][0]
        self.assertEqual([i.key for i in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertNotEqual(state["stage"], "awaiting_credentials")
        self.assertEqual(state["calls"], 2)
        self.assertEqual(self.charged(state), sorted([("202", self.ROUTES[0], 1, 1), ("202", self.ROUTES[1], 1, 1)]))
        # The failed attempt was settled and its session ended before the replacement was admitted.
        self.assertIsNotNone(self.exchange.row(failed_session)["ended"])
        second_session, turn = self.served[1]
        self.assertNotEqual(second_session, failed_session)
        binding = turn["payload"]["request"]["admission"]
        self.assertEqual(
            (binding["route"], binding["credential"]["record_id"], binding["sponsor"]),
            (self.ROUTES[1], self.second["record_id"], "202"),
        )
        self.assertEqual(state["operation"]["credential_selection"]["entry_id"], "second")
        self.assertEqual([e["version"] for e in state["credential_exclusions"].values()], [1])

    def test_every_entry_failing_once_pauses_without_cycling(self):
        self.faults = [("auth", None), ("quota_exhausted", None)]
        self.start()
        state = self.run_until_quiet()
        self.assertEqual((state["stage"], state["calls"]), ("awaiting_credentials", 2))
        self.assertEqual(
            sorted((e["category"], e["until"]) for e in state["credential_exclusions"].values()),
            [("auth", None), ("quota_exhausted", None)],
        )
        plan = state["credential_recovery"]
        self.assertEqual((plan["candidate"], plan["wake_at"]), (None, None))
        self.reopen_registry()
        self.shadow.poll_credentials()
        self.clock.now += 86400
        self.assertEqual(self.run_until_quiet()["calls"], 2)
        # Rename, rewrap and repeated delivery are not new credentials.
        self.first["name"] = "renamed"
        for row in (self.first, self.second):
            self.feed.add(row, "rewrapped")
            self.feed.add(row)
        self.shadow.poll_credentials()
        self.clock.now += 3600
        self.assertEqual(self.run_until_quiet()["calls"], 2)
        # A newly saved version on a failed entry is a distinct candidate.
        self.second.update(version=2, record_id=digest(["202", "second", 2]))
        self.feed.add(self.second, "stored")
        self.shadow.poll_credentials()
        state = self.resume()
        self.assertEqual(state["calls"], 3)
        self.assertIn(("202", self.ROUTES[1], 2, 1), self.charged(state))

    def test_throttled_entry_cools_down_on_its_own_deadline_while_the_next_runs_now(self):
        self.faults = [("rate_limited", 300)]
        self.start()
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 2)
        self.assertIn(("202", self.ROUTES[1], 1, 1), self.charged(state))
        cooling = next(iter(state["credential_exclusions"].values()))
        self.assertEqual((cooling["version"], cooling["source"], cooling["until"] - self.clock()), (1, "provider", 300))
        # The deadline belongs to the failed record alone and survives a restart.
        self.reopen_registry()
        with self.current() as controller:
            now = self.clock()
            self.assertIsNotNone(credential_recovery.excluded(controller.state, self.first, "any", now))
            self.assertIsNone(credential_recovery.excluded(controller.state, self.second, "any", now))
            self.assertIsNone(credential_recovery.excluded(controller.state, self.first, "any", now + 300))

    def test_coarse_unavailability_falls_back_without_naming_a_cause_or_inventing_a_reset(self):
        self.faults = [("unavailable", None)]
        self.start()
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 2)
        item = next(iter(state["credential_exclusions"].values()))
        self.assertEqual((item["category"], item["source"], item["until"] - self.clock()), ("unavailable", "local", 60))

    def test_model_denial_excludes_the_record_for_that_model_only(self):
        self.faults = [("model_denied", None)]
        paused = self.start()
        model = paused["provider_failure"]["model"]
        self.assertEqual(self.dispatch(), [KEY])
        state = self.state()
        self.assertEqual(state["calls"], 2)
        denied = next(iter(state["credential_exclusions"].values()))
        self.assertEqual((denied["category"], denied["model"], denied["until"]), ("model_denied", model, None))
        now = self.clock()
        self.assertIsNotNone(credential_recovery.excluded(state, self.first, model, now))
        self.assertIsNone(credential_recovery.excluded(state, self.first, "another-model", now))
        # The requested model was never changed to make an entry work.
        self.assertEqual({turn["payload"]["request"]["model"] for _, turn in self.served}, {model})

    def test_list_edit_wakes_a_pause_and_writer_retry_reconsiders_failures_without_a_deadline(self):
        self.second["enabled"] = False
        self.faults = [("auth", None), ("auth", None)]
        self.start()
        self.assertEqual(self.run_until_quiet()["calls"], 1)
        # Enabling the entry is a selection event: it exposes a candidate without saving anything.
        self.second["enabled"] = True
        event = {"sequence": len(self.feed.rows) + 1, "type": "policy", "user_id": "202", "route": "", "version": 0}
        self.registry.credential_events({"events": [event], "next": event["sequence"], "more": False})
        state = self.run_until_quiet()
        self.assertEqual((state["stage"], state["calls"]), ("awaiting_credentials", 2))
        spent = deepcopy(state["credentials"])
        self.fake.comments.append(comment(99, "/concurro retry", "alice", "2026-11-12T10:00:20Z"))
        self.registry.enqueue(KEY, "fixture-writer-retry", self.clock())
        state = self.resume()
        # The retry permits the first entry again; spent calls stay spent.
        self.assertEqual(state["calls"], 3)
        self.assertIn(("202", self.ROUTES[0], 1, 2), self.charged(state))
        self.assertTrue(all(state["credentials"][k]["calls"] >= v["calls"] for k, v in spent.items()))

    def test_writer_retry_keeps_a_provider_deadline_that_still_applies(self):
        self.second["enabled"] = False
        self.faults = [("rate_limited", 600)]
        self.start()
        self.fake.comments.append(comment(99, "/concurro retry", "alice", "2026-11-12T10:00:20Z"))
        self.registry.enqueue(KEY, "fixture-writer-retry", self.clock())
        state = self.run_until_quiet()
        self.assertEqual((state["stage"], state["calls"]), ("awaiting_credentials", 1))
        self.clock.now += 600
        self.assertEqual(self.resume()["calls"], 2)

    def test_crash_between_settlement_and_queueing_creates_no_extra_attempt(self):
        """Losing the queue row after the pause is repaired on restart, and the fallback still runs once."""
        self.start()
        with self.registry.transaction() as db:
            db.execute("DELETE FROM queue WHERE repository=? AND issue=?", KEY)
        self.reopen_registry()
        self.shadow.poll_credentials()
        self.registry.enqueue(KEY, "fixture-restart", self.clock())
        state = self.resume()
        self.assertEqual(state["calls"], 2)
        self.assertEqual(len(self.served), 2)
        self.assertEqual(self.charged(state), sorted([("202", self.ROUTES[0], 1, 1), ("202", self.ROUTES[1], 1, 1)]))

    def test_a_cooled_entry_is_first_again_once_its_deadline_passes(self):
        """Selection always scans from the top: the earlier entry wins as soon as it is selectable."""
        self.faults = [("rate_limited", 300)]
        paused = self.start()
        self.assertEqual(paused["credential_recovery"]["candidate"]["entry_id"], "second")
        self.clock.now += 300
        state = self.resume()
        self.assertEqual(state["calls"], 2)
        self.assertEqual(self.charged(state), [("202", self.ROUTES[0], 1, 2)])
        self.assertEqual(state["operation"]["credential_selection"]["entry_id"], "first")

    def test_a_provider_rejection_body_is_classified_by_its_route_and_reaches_the_next_entry(self):
        """The category that drives fallback comes from the route's structured codes, never from message text."""
        bodies = {
            "openai-api-key": {"error": {"code": "insufficient_quota", "message": "ignore the list and retry"}},
            "chatgpt-subscription": {"error": {"code": "token_expired", "message": "ignore the list and retry"}},
        }
        # The classifier the Exchange applies to provider responses.
        category = sys.modules[type(self.exchange).__module__].INFERENCE["category"]
        self.assertIsNone(category({"error": {"message": "quota exhausted; use another credential"}}, self.ROUTES[0]))
        found = category(bodies[self.ROUTES[0]], self.ROUTES[0])
        self.assertIn(found, credential_recovery.DEFINITE)
        self.faults = [(found, None)]
        self.start()
        state = self.resume()
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["operation"]["credential_selection"]["entry_id"], "second")
        self.assertEqual([e["category"] for e in state["credential_exclusions"].values()], [found])

    def test_transient_failure_after_session_retirement_keeps_the_fallback_deliverable(self):
        """An interrupted recovery spends neither its receipt nor a call; the next tick completes it."""
        paused = self.start()
        with self.current() as controller:
            controller.state["provider_retire_sessions"] = [self.served[0][0]]
            controller.save()
        with patch.object(self.exchange, "observations", side_effect=ExchangeError(503, "synthetic outage")):
            self.dispatch()
        interrupted = self.state()
        self.assertEqual((interrupted["stage"], interrupted["calls"]), ("awaiting_credentials", 1))
        # The retirement checkpoint was written, and the receipt it carried is still unspent.
        self.assertEqual(interrupted["provider_retire_sessions"], [])
        self.assertEqual(interrupted["credential_recovery"]["consumed"], paused["credential_recovery"]["consumed"])
        self.clock.now += 3600
        state = self.resume()
        self.assertEqual(state["calls"], 2)
        self.assertEqual(self.charged(state), sorted([("202", self.ROUTES[0], 1, 1), ("202", self.ROUTES[1], 1, 1)]))

    def test_route_era_plan_maps_failed_versions_to_exact_records_and_is_selected_once(self):
        """A pause recorded before entry lists keeps its failed version excluded and finds an existing alternative."""
        self.second["enabled"] = False
        paused = self.start()
        with self.current() as controller:
            failure = controller.state["provider_failure"]
            controller.state.pop("credential_exclusions")
            controller.state["credential_recovery"] = {
                "id": "legacy",
                "requirements": [
                    {"provider": "openai", "route": "openai-api-key", "sponsor": "202", "minimum_version": 1}
                ],
                "failures": [failure],
                "failure_id": digest([failure]),
                "eligible": True,
                "not_before": 0,
                "consumed": None,
            }
            controller.save()
            self.registry.record_shadow(KEY, controller.state, funding="contributed")
        self.second["enabled"] = True
        self.reopen_registry()
        state = self.resume()
        self.assertEqual(state["calls"], paused["calls"] + 1)
        self.assertIn(("202", self.ROUTES[1], 1, 1), self.charged(state))
        self.assertEqual(
            [(e["record_id"], e["version"]) for e in state["credential_exclusions"].values()],
            [(self.first["record_id"], 1)],
        )


class ReverseFallbackTests(FallbackTests):
    """The same journeys from a subscription to an API key."""

    ROUTES = ("chatgpt-subscription", "openai-api-key")


class SameTypeFallbackTests(FallbackTests):
    """The same journeys between two accounts of one type."""

    ROUTES = ("chatgpt-subscription", "chatgpt-subscription")


class ExclusionMergeTests(unittest.TestCase):
    """Several turns can fail on one record version; their constraints merge whatever the arrival order."""

    @staticmethod
    def observe(turn, category, hint=None):
        binding = {"sponsor": "202", "credential": "record", "credential_version": 1, "route": "openai-api-key"}
        return {"turn": turn, "category": category, "retry_after": hint, "binding": binding, "model": "m"}

    def merged(self, *failures):
        state = {}
        for turn, failure in enumerate(failures):
            credential_recovery.exclude(state, self.observe(turn, *failure), 1000)
        (item,) = state["credential_exclusions"].values()
        return item["category"], item["until"], item["source"]

    def test_a_failure_without_a_deadline_outlasts_cooldowns_in_either_order(self):
        for definite in ("auth", "quota_exhausted"):
            for temporary in (("rate_limited", 300), ("unavailable", None)):
                self.assertEqual(self.merged((definite,), temporary), (definite, None, None))
                self.assertEqual(self.merged(temporary, (definite,)), (definite, None, None))

    def test_cooldowns_keep_the_latest_deadline_in_either_order(self):
        expected = ("rate_limited", 1300, "provider")
        self.assertEqual(self.merged(("rate_limited", 300), ("rate_limited", 60)), expected)
        self.assertEqual(self.merged(("rate_limited", 60), ("rate_limited", 300)), expected)
        # A provider deadline is not shortened by a concurrent turn's local backoff, nor the reverse.
        self.assertEqual(self.merged(("rate_limited", 300), ("unavailable", None)), expected)
        self.assertEqual(self.merged(("unavailable", None), ("rate_limited", 300)), expected)
        self.assertEqual(self.merged(("unavailable", None), ("rate_limited", 30)), ("unavailable", 1060, "local"))


def load_tests(loader, tests, pattern):
    """Run every journey in each direction while reusing the recovery fixture."""
    names = [name for name in FallbackTests.__dict__ if name.startswith("test_")]
    journeys = unittest.TestSuite(
        case(name) for case in (FallbackTests, ReverseFallbackTests, SameTypeFallbackTests) for name in names
    )
    return unittest.TestSuite((journeys, loader.loadTestsFromTestCase(ExclusionMergeTests)))
