"""Subscription versions through real controller charge, resolver pause and writer retry."""

import json
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import test_sponsored_admissions as admission_fixture

# isort: split
from support import setUpModule as setUpModule, tearDownModule as tearDownModule

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/vault"))

# isort: split
from fastapi.testclient import TestClient

from exchange_fixtures import BINDING, EXPECTED
from fixtures import KEY
from vault_fixtures import ALICE, BOB, FakeTransport

import sponsorship
import vault
from agents import Agents, RunnerExecutor
from exchange import Refused
from policy import ProviderFailure
from test_shadow import comment
from test_subscription_validation import TOKENS, Probes


class IdentityTransport(FakeTransport):
    """Use the established provider fake with the service fixture's independent stable owners."""

    def __call__(self, url, headers):
        """GitHub authenticates the same synthetic Alice/Bob tokens as actors202/303 here."""
        status, body = super().__call__(url, headers)
        if url == vault.GITHUB_USER and status == 200:
            user = json.loads(body)
            user["id"] = {"alice": 202, "bob": 303}[user["login"]]
            body = json.dumps(user).encode()
        return status, body


class JourneyTests(admission_fixture.AdmissionTests):
    """Reuse source ordering and real task ownership; only provider/runner I/O is synthetic."""

    def setUp(self):
        """Give both consenting owners real encrypted subscription material on both fixed routes."""
        super().setUp()
        self.config["credential_routes"] = {"codex": "chatgpt-subscription", "claude": "claude-subscription"}
        self.exchange.gateway.contributed_routes = tuple(self.config["credential_routes"].values())
        self.credential_store = vault.Store(
            Path(self.directory.name) / "vault.db", vault.Envelope(os.urandom(32)), clock=self.clock
        )
        self.addCleanup(self.credential_store.close)
        transport = IdentityTransport()
        self.probes = Probes()
        self.owner_api = TestClient(
            vault.create_app(
                self.credential_store, vault.Identity(transport), vault.Validator(transport, probe=self.probes)
            )
        )
        self.addCleanup(self.owner_api.close)
        for route in self.config["credential_routes"].values():
            for token in (ALICE, BOB):
                self.register(route, token)
        self.service()

    def register(self, route, token=ALICE):
        """Validate the selected role model union as one real owner replacement operation."""
        models = sorted({r["model"] for r in sponsorship.selected_routes(self.config) if r["route"] == route})
        body = {"secret": TOKENS[route][int(token == BOB)], "models": models}
        if route == "chatgpt-subscription":
            body["account_id"] = "account-bob" if token == BOB else "account-alice"
        result = self.owner_api.put("/credentials/" + route, headers={"Authorization": "Bearer " + token}, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def read_metadata(self, actor, at=None):
        """Read current or exact source-era availability from the actual versioned vault."""
        return self.credential_store.metadata(int(actor), at)["credentials"]

    def events(self, after):
        """Consume the real lifecycle ledger through Exchange's existing restart-safe cursor."""
        return self.credential_store.events(after)

    def resolve(self, binding):
        """Translate actual exact-version private resolver errors like the HTTP adapter."""
        try:
            return self.credential_store.resolve(
                int(binding["sponsor"]), binding["route"], binding["credential"], binding["credential_version"]
            )
        except vault.VaultError as error:
            raise Refused(error.status, "credential resolution unavailable") from None

    def test_expiry_pause_replace_writer_retry_keeps_charge_and_grant_history(self):
        """Known running-turn expiry pauses once; replacement alone cannot spend or retry work."""
        self.sponsors()
        with self.current() as controller:
            expected = {**EXPECTED, "actor_id": "3003"}
            task = {
                **{k: v for k, v in BINDING.items() if k not in ("role", "base", "phase")},
                "repository": KEY[0],
                "upstream": KEY[0],
                "fork": KEY[0],
                "generation": controller.gh.generation,
            }
            served = []

            def dispatch(session, labels):
                """Use real key/claim/request-start/resolve, injecting trusted fixture expiry mid-turn."""
                oidc = json.dumps(
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
                key = self.exchange.key(session, oidc, engine=task["engine"])
                turn = self.exchange.next(session, key["token"], 1, 0)["turns"][0]
                served.append((session, turn))
                request = self.exchange.inference.start(key["key"], "POST", "/v1/responses")["request"]
                if len(served) == 1:
                    self.credential_store.db.execute(
                        "UPDATE credential_metadata SET expires_at=?,expiry_known=1 WHERE user_id=202 AND route=? AND version=1",
                        (datetime.fromtimestamp(self.clock() - 1, UTC).isoformat(), "chatgpt-subscription"),
                    )
                    with self.assertRaises(Refused) as refused:
                        self.exchange.inference.resolve(request, session)
                    self.assertEqual(refused.exception.status, 410)
                    outcome = {"outcome": "failed", "error": "synthetic runner failure"}
                else:
                    material = self.exchange.inference.resolve(request, session)
                    self.assertEqual(material["token"], TOKENS["chatgpt-subscription"][0])
                    self.exchange.inference.finish(
                        request,
                        "observer",
                        {
                            "category": None,
                            "usage": 2,
                            "completed": True,
                            "status": 200,
                            "retry_after": None,
                        },
                    )
                    outcome = {"outcome": "ok", "response": {"synthetic": "completed"}}
                self.exchange.complete(session, key["token"], turn["id"], outcome)

            controller.agents = Agents(
                controller.config,
                RunnerExecutor(
                    admission_fixture.LocalOperator(self.exchange),
                    "https://fixture.invalid",
                    expected,
                    ["*"],
                    1000,
                    dispatch,
                    task=task,
                ),
                controller.bundle,
            )
            calls = controller.state["calls"]
            slots = json.loads(json.dumps(controller.state["sponsorships"]))
            context = {**controller.context(), "purpose": "subscription expiry journey"}
            with patch("exchange.claims", side_effect=lambda token, *args: json.loads(token)):
                with self.assertRaises(ProviderFailure) as failed:
                    controller.ask("codex", "intake", context)
                self.assertEqual(failed.exception.observations[0]["category"], "auth")
                self.worker._suspend_failure(controller, controller.issue, failed.exception, {"kind": "suspend"}, 0)
                self.assertEqual(controller.state["stage"], "awaiting_credentials")
                self.assertEqual(controller.state["calls"], calls + 1)
                replacement = self.register("chatgpt-subscription")
                self.assertEqual(replacement["version"], 2)
                self.assertEqual(controller.state["calls"], calls + 1)
                self.assertEqual(len(served), 1)
                with self.assertRaises(ProviderFailure):
                    controller.select_admissions([("codex", "intake", context)])
                self.fake.comments.append(comment(90, "/concurro retry"))
                controller.commands(self.fake.comments)
                result = controller.ask("codex", "intake", context)
            self.assertEqual(result, {"synthetic": "completed"})
            self.assertEqual(controller.state["calls"], calls + 2)
            self.assertEqual(controller.state["sponsorships"], slots)
            self.assertEqual(controller.state["operation"]["admission"]["credential"]["version"], 2)
            self.assertNotEqual(served[0][0], served[1][0])
            self.assertIsNotNone(self.exchange.row(served[0][0])["ended"])
            self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 2)


def load_tests(loader, tests, pattern):
    """Run only this journey while reusing existing service and admission fixtures."""
    return unittest.TestSuite(JourneyTests(name) for name in JourneyTests.__dict__ if name.startswith("test_"))
