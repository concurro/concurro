"""List fallback through the real encrypted vault, controller, registry and Exchange."""

import json
import unittest
import uuid
from unittest.mock import patch

import test_sponsored_admissions as admission_fixture

# isort: split
from exchange_fixtures import BINDING, EXPECTED
from fixtures import KEY, target_files
from vault_fixtures import ALICE

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from agents import Agents, RunnerExecutor
from policy import ProviderFailure
from test_subscription_journey import JourneyTests

SUBSCRIPTION, API_KEY = "chatgpt-subscription", "openai-api-key"
HEADERS = {"Authorization": "Bearer " + ALICE}
# Route, token and account header each attempt must resolve.
ALICE_SUBSCRIPTION = (SUBSCRIPTION, "SYNTHETIC_CHATGPT_ALICE", "account-alice")
ALICE_KEY = (API_KEY, "SYNTHETIC_OPENAI_KEY", "")


class VaultFallbackTests(JourneyTests):
    """Alice's OpenAI list holds real sealed entries; the first one's provider rejects the call."""

    def setUp(self):
        super().setUp()
        self.exchange.gateway.contributed_routes += (API_KEY,)
        self.config["funding_mode"] = "contributed"
        self.backend.files = target_files(self.config)
        self.service()
        self.worker.sponsorship = self.shadow

    def add(self, route, name, secret, account=None):
        body = {"route": route, "name": name, "secret": secret, "mutation_id": uuid.uuid4().hex}
        result = self.owner_api.post("/credentials", headers=HEADERS, json={**body, **account_field(account)})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def arrange(self, first, second):
        """Put two entries at the head of Alice's OpenAI list, in that order."""
        listed = next(
            g
            for g in self.owner_api.get("/credentials", headers=HEADERS).json()["providers"]
            if g["provider"] == "openai"
        )
        rest = [
            e["entry_id"] for e in listed["entries"] if e["entry_id"] not in (first["entry_id"], second["entry_id"])
        ]
        body = {"revision": listed["revision"], "mutation_id": uuid.uuid4().hex}
        body["entries"] = [first["entry_id"], second["entry_id"], *rest]
        result = self.owner_api.put("/credentials/order/openai", headers=HEADERS, json=body)
        self.assertEqual(result.status_code, 200, result.text)

    def fall_back(self, first, second, expected):
        """Fail the first entry's call at the provider and require the second to run on its own material."""
        self.arrange(first, second)
        self.sponsors()
        resolved = []
        with self.current() as controller:
            claims = {**EXPECTED, "actor_id": "3003"}
            task = {k: v for k, v in BINDING.items() if k not in ("role", "base", "phase")}
            task.update(repository=KEY[0], upstream=KEY[0], fork=KEY[0], generation=controller.gh.generation)

            def dispatch(session, labels):
                token = {**claims, "ref": "refs/heads/concurro/runs/" + session, "runner_environment": "github-hosted"}
                token.update(run_id="77", run_attempt="1", jti=uuid.uuid4().hex, exp=self.clock() + 300)
                key = self.exchange.key(session, json.dumps(token), engine=task["engine"])
                turn = self.exchange.next(session, key["token"], 1, 0)["turns"][0]
                request = self.exchange.inference.start(key["key"], "POST", "/v1/responses")["request"]
                resolved.append((session, self.exchange.inference.resolve(request, session)))
                failed = len(resolved) == 1
                result = {"category": "quota_exhausted" if failed else None, "status": 429 if failed else 200}
                result.update(usage=None if failed else 2, completed=not failed, retry_after=None)
                self.exchange.inference.finish(request, "provider" if failed else "observer", result)
                outcome = {"outcome": "failed", "error": "synthetic"} if failed else {"outcome": "ok", "response": {}}
                self.exchange.complete(session, key["token"], turn["id"], outcome)

            operator = admission_fixture.LocalOperator(self.exchange)
            executor = RunnerExecutor(operator, "https://fixture.invalid", claims, ["*"], 1000, dispatch, task=task)
            controller.agents = Agents(controller.config, executor, controller.bundle)
            calls, before = controller.state["calls"], ledger(controller.state)
            context = {**controller.context(), "purpose": "list fallback journey"}
            with patch("exchange.claims", side_effect=lambda token, *args: json.loads(token)):
                with self.assertRaises(ProviderFailure) as failure:
                    controller.ask("codex", "intake", context)
                self.worker._suspend_failure(controller, controller.issue, failure.exception, {"kind": "suspend"}, 0)
                self.assertEqual(controller.state["stage"], "awaiting_credentials")
                plan = controller.state["credential_recovery"]
                self.assertEqual(plan["candidate"]["entry_id"], second["entry_id"])
                # The alternative was already listed: recovery needs no vault event and no writer.
                self.shadow.recover(controller, self.fake.comments)
                self.assertNotEqual(controller.state["stage"], "awaiting_credentials")
                self.assertEqual(controller.ask("codex", "intake", context), {})
            state = controller.state
            self.assertEqual(state["calls"], calls + 2)
            self.assertEqual([(m["route"], m["token"], m["account"]) for _, m in resolved], expected)
            # The ledger charges each attempt to the exact record whose secret that attempt resolved.
            after = ledger(state)
            for entry in (first, second):
                key = entry["route"], entry["record"]["record_id"], entry["record"]["version"]
                self.assertEqual(after[key] - before.get(key, 0), 1)
            self.assertEqual(state["operation"]["credential_selection"]["entry_id"], second["entry_id"])
            self.assertEqual(state["operation"]["admission"]["credential"]["record_id"], second["record"]["record_id"])
            self.assertNotEqual(resolved[0][0], resolved[1][0])
            self.assertIsNotNone(self.exchange.row(resolved[0][0])["ended"])

    def existing(self):
        """The subscription entry the fixture saved for Alice."""
        groups = self.owner_api.get("/credentials", headers=HEADERS).json()["providers"]
        return next(e for g in groups for e in g["entries"] if e["route"] == SUBSCRIPTION)

    def test_two_accounts_of_one_type_resolve_their_own_token_and_account(self):
        other = self.add(SUBSCRIPTION, "second account", "SYNTHETIC_CHATGPT_SECOND", "account-second")
        self.fall_back(
            self.existing(), other, [ALICE_SUBSCRIPTION, (SUBSCRIPTION, "SYNTHETIC_CHATGPT_SECOND", "account-second")]
        )

    def test_api_key_falls_back_to_subscription(self):
        key = self.add(API_KEY, "key", "SYNTHETIC_OPENAI_KEY")
        self.fall_back(key, self.existing(), [ALICE_KEY, ALICE_SUBSCRIPTION])

    def test_subscription_falls_back_to_api_key(self):
        key = self.add(API_KEY, "key", "SYNTHETIC_OPENAI_KEY")
        self.fall_back(self.existing(), key, [ALICE_SUBSCRIPTION, ALICE_KEY])


def ledger(state):
    """Charged calls by route, record and version."""
    return {(r.get("route"), r.get("record_id"), r.get("version")): r["calls"] for r in state["credentials"].values()}


def account_field(account):
    return {"account_id": account} if account else {}


def load_tests(loader, tests, pattern):
    """Run only these journeys while reusing the subscription journey's vault fixture."""
    names = [name for name in VaultFallbackTests.__dict__ if name.startswith("test_")]
    return unittest.TestSuite(VaultFallbackTests(name) for name in names)
