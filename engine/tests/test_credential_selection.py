"""List selection rules shared by admission, initialization and coverage."""

import unittest

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import sponsorship

TARGET, OTHER = "acme/demo", "other/demo"


def entry(name, route, position, **fields):
    """One owner's listed metadata record."""
    provider = "openai" if route in ("openai-api-key", "chatgpt-subscription") else "anthropic"
    return {
        "name": name,
        "entry_id": name,
        "route": route,
        "provider": provider,
        "kind": "subscription" if route.endswith("subscription") else "api_key",
        "position": position,
        "enabled": True,
        "roles": [],
        "profiles": [],
        "status": "valid",
        "version": 1,
        "list_revision": 3,
        **fields,
    }


def names(records, role, profile, target=TARGET, **options):
    return [r["name"] for r in sponsorship.candidates(records, "anthropic", role, target, profile, **options)]


class CandidateTests(unittest.TestCase):
    """Position alone ranks entries; filters only decide which entries take part."""

    def setUp(self):
        # The proposal's example list.
        self.records = [
            entry("work", "claude-subscription", 2),
            entry(
                "credits",
                "anthropic-api-key",
                0,
                roles=["triage"],
                profiles=[{"target": TARGET, "profile": "sonnet55-high"}],
            ),
            entry("personal", "claude-subscription", 1),
            entry("codex", "openai-api-key", 0),
        ]

    def test_example_prefers_filtered_credits_then_falls_through_in_order(self):
        self.assertEqual(names(self.records, "triage", "sonnet55-high"), ["credits", "personal", "work"])
        self.assertEqual(names(self.records, "planner", "sonnet55-high"), ["personal", "work"])
        self.assertEqual(names(self.records, "triage", "opus-max"), ["personal", "work"])

    def test_specificity_never_outranks_position(self):
        self.records[1]["position"], self.records[2]["position"] = 1, 0
        self.assertEqual(names(self.records, "triage", "sonnet55-high"), ["personal", "credits", "work"])

    def test_either_filter_alone_and_both_together(self):
        role_only = [entry("r", "anthropic-api-key", 0, roles=["planner", "implementer"])]
        self.assertEqual(names(role_only, "implementer", "any"), ["r"])
        self.assertEqual(names(role_only, "triage", "any"), [])
        profile_only = [entry("p", "anthropic-api-key", 0, profiles=[{"target": TARGET, "profile": "a"}])]
        self.assertEqual(names(profile_only, "pr_reviewers", "a"), ["p"])
        self.assertEqual(names(profile_only, "pr_reviewers", "b"), [])
        self.assertEqual(names(self.records[1:2], "triage", "other"), [])
        self.assertEqual(names(self.records[1:2], "planner", "sonnet55-high"), [])

    def test_equal_profile_keys_on_other_targets_do_not_match(self):
        self.assertEqual(names(self.records, "triage", "sonnet55-high", OTHER), ["personal", "work"])
        both = [
            entry(
                "x",
                "anthropic-api-key",
                0,
                profiles=[{"target": OTHER, "profile": "a"}, {"target": TARGET, "profile": "b"}],
            )
        ]
        self.assertEqual(names(both, "triage", "a"), [])
        self.assertEqual(names(both, "triage", "b"), ["x"])

    def test_other_model_call_roles_match_only_unrestricted_entries(self):
        self.assertEqual(sponsorship.role_key("review"), "pr_reviewers")
        self.assertEqual(sponsorship.role_key("plan_qc"), "plan_reviewers")
        self.assertEqual(sponsorship.role_key("consolidate"), "consolidate")
        self.assertEqual(names(self.records, "consolidate", "sonnet55-high"), ["personal", "work"])

    def test_disabled_removed_unusable_and_unsupported_entries_are_skipped(self):
        for change in (
            {"enabled": False},
            {"position": None},
            {"status": "invalid"},
            {"status": "revoked"},
            {"kind": "api_key"},
            {"provider": "openai"},
            {"expiry_known": True, "expires_at": "2000-01-01T00:00:00+00:00"},
            {"expiry_known": True, "expires_at": None},
        ):
            with self.subTest(change=change):
                records = [{**self.records[2], **change}, self.records[0]]
                self.assertEqual(names(records, "planner", "p", now=10**9), ["work"])
        self.assertEqual(names(self.records, "planner", "p", routes=["anthropic-api-key"]), [])
        future = {**self.records[2], "expiry_known": True, "expires_at": "2999-01-01T00:00:00Z"}
        self.assertEqual(names([future], "planner", "p", now=10**9), ["personal"])

    def test_route_era_records_never_match(self):
        legacy = {"route": "anthropic-api-key", "status": "valid", "kind": "api_key", "version": 1}
        self.assertEqual(sponsorship.candidates([legacy], "anthropic", "planner", TARGET, "p"), [])


class ProjectionTests(unittest.TestCase):
    def test_listed_routes_follow_the_list_only_where_it_governed_the_boundary(self):
        requirements = [
            {"role": "triage", "profile": "c", "model": "m", "provider": "openai", "route": "openai-api-key"},
            {"role": "planner", "profile": "a", "model": "m", "provider": "anthropic", "route": "anthropic-api-key"},
            {"role": "implementer", "profile": "a", "model": "m", "provider": "anthropic", "route": None},
        ]
        legacy = [{"route": "anthropic-api-key", "status": "valid", "kind": "api_key"}]
        kept = sponsorship.listed_routes(requirements, legacy, TARGET)
        # Route-era Anthropic evidence keeps its configured requirements; unlisted OpenAI has no candidate.
        self.assertEqual(kept, requirements[1:])
        records = [entry("personal", "claude-subscription", 0, roles=["planner"])]
        resolved = sponsorship.listed_routes(requirements, records, TARGET)
        self.assertEqual(
            [(r["role"], r["route"], r["entry_id"]) for r in resolved], [("planner", "claude-subscription", "personal")]
        )
        # A reviewed PR considers only its own role, and route-era providers keep every requirement.
        self.assertEqual(sponsorship.listed_routes(requirements, records, TARGET, ("pr_reviewers",)), [])
        self.assertEqual(sponsorship.listed_routes(requirements, legacy, TARGET, ("pr_reviewers",)), requirements[1:])

    def test_boundary_evidence_is_the_matched_entry_not_every_record_on_its_route(self):
        removed = entry("old", "anthropic-api-key", None, status="unknown", enabled=False)
        current = entry("new", "anthropic-api-key", 0)
        requirement = {"role": "planner", "profile": "a", "model": "m", "provider": "anthropic", "route": None}
        resolved = sponsorship.listed_routes([requirement], [removed, current], TARGET)
        self.assertEqual(sponsorship.boundary_evidence(resolved, [removed, current]), [current])
        legacy = [{"route": "anthropic-api-key", "status": "unknown"}, {"route": "openai-api-key", "status": "valid"}]
        configured = [{**requirement, "route": "anthropic-api-key"}]
        self.assertEqual(sponsorship.boundary_evidence(configured, legacy), legacy[:1])
        self.assertEqual(sponsorship.listed_routes([requirement], [], TARGET), [])

    def test_coverage_names_the_selected_entry_or_why_none_is_selected(self):
        requirement = {"role": "planner", "profile": "a", "model": "m", "provider": "anthropic", "route": None}
        slots = {"anthropic": {"sponsor_id": "101"}, "openai": {}}
        status = lambda records: sponsorship.coverage([requirement], slots, {"101": records}, TARGET)["anthropic"][0]  # noqa: E731
        chosen = status([entry("credits", "anthropic-api-key", 1), entry("personal", "claude-subscription", 0)])
        self.assertEqual(
            (chosen["status"], chosen["entry"], chosen["entry_id"], chosen["route"], chosen["version"]),
            ("available", "personal", "personal", "claude-subscription", 1),
        )
        self.assertEqual(status([entry("old", "anthropic-api-key", 0, status="invalid")])["status"], "reconnect")
        for records in (
            [],
            [entry("off", "anthropic-api-key", 0, enabled=False)],
            [entry("t", "anthropic-api-key", 0, roles=["triage"])],
        ):
            self.assertEqual(status(records)["status"], "missing")
        self.assertEqual(status(None)["status"], "unknown")
        unassigned = sponsorship.coverage([requirement], {"anthropic": {}}, {}, TARGET)["anthropic"][0]
        self.assertEqual((unassigned["status"], unassigned["entry"]), ("unassigned", None))


if __name__ == "__main__":
    unittest.main()
