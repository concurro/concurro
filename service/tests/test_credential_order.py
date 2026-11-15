"""Ordering manifest: personal grants' selected credential types, read from the real registry."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import ServiceFixture

from service import __main__ as cli, credential_order
from service.errors import RegistryError, RegistryFailure
from service.registry import Registry

REPO, OTHER = "up/project", "up/other"
API = {"provider": "anthropic", "route": "anthropic-api-key", "role": "triage", "profile": "a", "model": "m"}
SUBSCRIPTION = {**API, "route": "claude-subscription", "role": "planner", "profile": "b"}
CODEX = {"provider": "openai", "route": "chatgpt-subscription", "role": "implementer", "profile": "c", "model": "m"}


def slot(actor, status="active"):
    return {"sponsor_id": actor, "status": status, "provenance": "explicit_comment"}


class ManifestTests(ServiceFixture, unittest.TestCase):
    def store(self, repository, issue, document, terminal=False):
        """Persist one confirmed sponsorship projection exactly as the registry reads it back."""
        with self.registry.transaction() as db:
            db.execute("INSERT INTO issue_sponsorships VALUES(?,?,1,1,?)", (repository, issue, json.dumps(document)))
            if terminal:
                db.execute("INSERT INTO outcomes VALUES(?,?,'terminal',0,NULL,0)", (repository, issue))

    def document(self, slots, funding, routes, task=False):
        base = {"facts": [{"source_id": "c"}], "slots": slots, "provider_funding": funding, "routes": routes}
        if task:
            return {**base, "slots": {}, "task": {"slots": slots, "provider_funding": funding, "stage": "plan"}}
        return base

    def test_agreeing_conflicting_and_unavailable_selections(self):
        personal = {"anthropic": "contributed", "openai": "contributed"}
        # 101 selects the Anthropic API key on two tasks; 202's tasks disagree; 303 has no route evidence.
        self.store(REPO, 1, self.document({"anthropic": slot("101"), "openai": slot("101")}, personal, [API, CODEX]))
        self.store(OTHER, 2, self.document({"anthropic": slot("101")}, personal, [API], task=True))
        self.store(REPO, 3, self.document({"anthropic": slot("202")}, personal, [API]))
        self.store(REPO, 4, self.document({"anthropic": slot("202")}, personal, [SUBSCRIPTION]))
        self.store(REPO, 5, self.document({"openai": slot("303")}, personal, []))
        # One source without route evidence leaves 404's otherwise single selection unestablished.
        self.store(REPO, 6, self.document({"openai": slot("404")}, personal, [CODEX]))
        self.store(REPO, 7, self.document({"openai": slot("404")}, personal, []))
        # The command reads beside the daemon's open, locked registry and writes nothing.
        settings = SimpleNamespace(state_dir=self.directory.name, registry_id="test")
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["service", "--settings", "s.json", "credential-order-manifest"]),
            patch.object(cli.settings_module, "load", return_value=settings),
            patch.object(cli.control, "request", side_effect=AssertionError("no daemon request")),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result, credential_order.manifest(self.registry))
        self.assertEqual(result["format"], 1)
        summary = {
            (owner, provider): (
                value["first"],
                value["evidence"],
                [(s["repository"], s["issue"]) for s in value["sources"]],
            )
            for owner, lists in result["owners"].items()
            for provider, value in lists.items()
        }
        self.assertEqual(
            summary,
            {
                ("101", "anthropic"): ("api_key", "agreement", [(OTHER, 2), (REPO, 1)]),
                ("101", "openai"): ("subscription", "agreement", [(REPO, 1)]),
                ("202", "anthropic"): (None, "conflict", [(REPO, 3), (REPO, 4)]),
                ("303", "openai"): (None, "unavailable", [(REPO, 5)]),
                ("404", "openai"): (None, "unavailable", [(REPO, 6), (REPO, 7)]),
            },
        )
        self.assertEqual(result["owners"]["101"]["anthropic"]["sources"][1]["kinds"], ["api_key"])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_org_funding_inactive_slots_and_finished_tasks_are_not_preferences(self):
        personal = {"anthropic": "contributed", "openai": "org"}
        self.store(REPO, 1, self.document({"anthropic": slot("101"), "openai": slot("101")}, personal, [API, CODEX]))
        self.store(REPO, 2, self.document({"anthropic": slot("202", "withdrawn")}, personal, [API]))
        self.store(REPO, 3, self.document({"anthropic": slot(None, "unassigned")}, personal, [API]))
        self.store(REPO, 4, self.document({"anthropic": slot("404")}, personal, [SUBSCRIPTION]), terminal=True)
        self.store(REPO, 5, self.document({"anthropic": slot("505")}, {"anthropic": "org"}, [API]))
        owners = credential_order.manifest(self.registry)["owners"]
        self.assertEqual(list(owners), ["101"])
        self.assertEqual(list(owners["101"]), ["anthropic"])

    def test_current_projection_routes_outrank_frozen_initialization(self):
        """A task initialized on a subscription whose effective configuration now selects the API key."""
        document = self.document({"anthropic": slot("101")}, {"anthropic": "contributed"}, [API], task=True)
        document["task"]["initialization"] = {"routes": [SUBSCRIPTION]}
        self.store(REPO, 1, document)
        with self.registry.transaction() as db:
            db.execute("INSERT INTO bindings VALUES(?,?,?,?,?)", (REPO, 1, REPO, "fork/project", "c" * 40))
        value = credential_order.manifest(self.registry)["owners"]["101"]["anthropic"]
        self.assertEqual((value["first"], value["evidence"]), ("api_key", "agreement"))
        self.assertEqual(value["sources"][0]["config_sha"], "c" * 40)

    def test_inspection_refuses_writes_and_missing_stores(self):
        reader = Registry.inspect(self.directory.name)
        self.addCleanup(reader.close)
        self.assertEqual(reader.sponsor_repositories(), [])
        with self.assertRaises(RegistryFailure):
            reader.bind((REPO, 1), REPO, "fork/project", "c" * 40)
        self.assertIsNone(self.registry.binding((REPO, 1)))
        with self.assertRaisesRegex(RegistryError, "missing"):
            Registry.inspect(self.directory.name + "/absent")

    def test_one_task_selecting_both_types_is_a_conflict(self):
        self.store(
            REPO, 1, self.document({"anthropic": slot("101")}, {"anthropic": "contributed"}, [API, SUBSCRIPTION])
        )
        value = credential_order.manifest(self.registry)["owners"]["101"]["anthropic"]
        self.assertEqual((value["first"], value["evidence"]), (None, "conflict"))
        self.assertEqual(value["sources"][0]["kinds"], ["api_key", "subscription"])


if __name__ == "__main__":
    unittest.main()
