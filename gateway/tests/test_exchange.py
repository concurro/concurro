"""The key exchange: OIDC verification, session binding, key lifecycle and reconciliation."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exchange"))
import exchange
from exchange import ISSUER, Exchange, Keys, Refused, server

AUDIENCE = "concurro-gateway"
# Sessions take their deadlines from the real clock; tokens are dated relative to it.
NOW = int(time.time())
EXPECTED = {"repository_id": "1001", "repository_owner_id": "2002",
            "job_workflow_ref": "concurro/concurro/.github/workflows/gateway-proof.yml@refs/heads/main",
            "job_workflow_sha": "a" * 40, "event_name": "workflow_dispatch", "ref": "refs/heads/main"}


def segment(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class Signer:
    """A local RSA key standing in for GitHub's OIDC signing key."""

    def __init__(self, directory):
        self.key = Path(directory) / "key.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)
        modulus = subprocess.run(["openssl", "rsa", "-in", str(self.key), "-noout", "-modulus"], check=True,
                                 capture_output=True, text=True).stdout.strip().split("=")[1]
        self.jwk = {"kty": "RSA", "kid": "test-kid", "n": segment(bytes.fromhex(modulus)), "e": segment((65537).to_bytes(3, "big"))}

    def token(self, header=None, **overrides):
        payload = {"iss": ISSUER, "aud": AUDIENCE, "exp": NOW + 300, "nbf": NOW - 5, "iat": NOW - 5,
                   "jti": overrides.pop("jti") if "jti" in overrides else os.urandom(8).hex(),
                   "runner_environment": "github-hosted", "run_id": "77", "run_attempt": "1", **EXPECTED}
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        head = segment(json.dumps(header or {"alg": "RS256", "kid": "test-kid", "typ": "JWT"}).encode())
        body = segment(json.dumps(payload).encode())
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.key)],
                                   input=(head + "." + body).encode(), check=True, capture_output=True).stdout
        return head + "." + body + "." + segment(signature)


class FakeGateway:
    """agentgateway's admin API as the exchange sees it."""

    def __init__(self):
        self.resources, self.next = {}, 0

    def find(self, key):
        return next((i for i, v in self.resources.items() if v["key"] == key), None)

    def create(self, key, name, models, budget):
        self.next += 1
        self.resources[str(self.next)] = {"key": key, "metadata": {"name": name, "concurro.session": name},
                                          "allowedModels": models, "budget": budget}
        return str(self.next)

    def activated(self, key):
        assert self.find(key)

    def list(self):
        return [{"id": i, "value": v} for i, v in self.resources.items()]

    def delete(self, key_id):
        self.resources.pop(key_id, None)
        return True


class ExchangeTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.signer = Signer(directory.name)
        self.gateway = FakeGateway()
        self.exchange = Exchange(str(Path(directory.name) / "exchange.db"), self.gateway,
                                 Keys(fetch=lambda: {"keys": [self.signer.jwk]}), AUDIENCE)
        self.session = self.exchange.issue(dict(EXPECTED), ["gpt-*", "claude-*"], 100000, 3600)

    def mint(self, **overrides):
        return self.exchange.key(self.session, self.signer.token(**overrides), now=NOW)

    def test_a_valid_token_mints_a_scoped_key_once(self):
        first = self.mint()
        self.assertTrue(first["key"].startswith("sk-agw-"))
        [resource] = self.gateway.list()
        self.assertEqual(resource["value"]["allowedModels"], ["gpt-*", "claude-*"])
        self.assertEqual(resource["value"]["budget"], 100000)
        self.assertEqual(self.mint()["key"], first["key"], "a retry from the bound run gets the same key")
        self.assertEqual(len(self.gateway.list()), 1)

    def test_invalid_tokens_are_refused(self):
        cases = {
            "issuer": {"iss": "https://evil.example"}, "audience": {"aud": "other"},
            "expired": {"exp": NOW - 120}, "too old": {"iat": NOW - 3600}, "not yet valid": {"nbf": NOW + 600},
            "no id": {"jti": None}, "empty id": {"jti": ""},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.mint(**overrides)
            self.assertEqual(raised.exception.status, 401)
        token = self.signer.token()
        head, body, signature = token.split(".")
        tampered = head + "." + segment(json.dumps({**json.loads(base64.urlsafe_b64decode(body + "==")),
                                                    "repository_id": "9"}).encode()) + "." + signature
        for name, value in (("tampered", tampered), ("malformed", "a.b"),
                            ("unknown key", self.signer.token(header={"alg": "RS256", "kid": "other"})),
                            ("no algorithm", self.signer.token(header={"alg": "none", "kid": "test-kid"}))):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.exchange.key(self.session, value, now=NOW)
            self.assertEqual(raised.exception.status, 401)
        self.assertEqual(self.gateway.list(), [])

    def test_tokens_must_match_the_session_and_its_run(self):
        for name, overrides in (("repository", {"repository_id": "9"}), ("owner", {"repository_owner_id": "9"}),
                                ("workflow", {"job_workflow_ref": "x/y/.github/workflows/z.yml@main"}),
                                ("commit", {"job_workflow_sha": "b" * 40}), ("event", {"event_name": "push"}),
                                ("ref", {"ref": "refs/heads/other"}), ("self-hosted", {"runner_environment": "self-hosted"}),
                                ("re-run", {"run_attempt": "2"})):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.mint(**overrides)
            self.assertEqual(raised.exception.status, 403)
        self.mint()
        with self.assertRaisesRegex(Refused, "another run"):
            self.mint(run_id="78")

    def test_a_token_is_used_once(self):
        token = self.signer.token(jti="once")
        self.exchange.key(self.session, token, now=NOW)
        with self.assertRaisesRegex(Refused, "already used"):
            self.exchange.key(self.session, token, now=NOW)

    def test_ending_deletes_the_key_and_the_session_stays_ended(self):
        self.mint()
        self.exchange.end(self.session, self.signer.token(), now=NOW)
        self.assertEqual(self.gateway.list(), [])
        with self.assertRaisesRegex(Refused, "ended"):
            self.mint()

    def test_reconciliation_deletes_expired_and_orphaned_keys(self):
        self.mint()
        live = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 7200)
        live_key = self.exchange.key(live, self.signer.token(run_id="90"), now=NOW)["key"]
        self.gateway.create("sk-agw-orphan", "unknown", ["*"], 1)
        removed = self.exchange.reconcile(now=NOW + 5000)
        self.assertEqual(sorted(removed), sorted([self.session, "unknown"]))
        self.assertEqual([r["value"]["key"] for r in self.gateway.list()], [live_key])
        later = {"iat": NOW + 4990, "nbf": NOW + 4990, "exp": NOW + 5300}
        with self.assertRaisesRegex(Refused, "ended"):
            self.exchange.key(self.session, self.signer.token(**later), now=NOW + 5000)

    def test_a_partial_creation_is_adopted_or_completed(self):
        self.exchange.db.execute("UPDATE sessions SET key = 'sk-agw-pending' WHERE id = ?", (self.session,))
        self.gateway.create("sk-agw-pending", self.session, ["gpt-*"], 1)
        self.assertEqual(self.mint()["key"], "sk-agw-pending")
        self.assertEqual(len(self.gateway.list()), 1, "adopted, not duplicated")
        other = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 3600)
        self.exchange.db.execute("UPDATE sessions SET key = 'sk-agw-lost' WHERE id = ?", (other,))
        self.assertEqual(self.exchange.key(other, self.signer.token(run_id="91"), now=NOW)["key"], "sk-agw-lost")
        self.assertIsNotNone(self.gateway.find("sk-agw-lost"))

    def test_a_failed_deletion_still_ends_the_session_and_is_retried(self):
        self.mint()
        working = self.gateway.delete
        self.gateway.delete = lambda key_id: False
        with self.assertRaisesRegex(Refused, "reconciliation will retry"):
            self.exchange.end(self.session, self.signer.token(), now=NOW)
        with self.assertRaisesRegex(Refused, "ended"):
            self.mint()
        self.assertEqual(len(self.gateway.list()), 1)
        self.gateway.delete = working
        self.assertEqual(self.exchange.reconcile(now=NOW), [self.session])
        self.assertEqual(self.gateway.list(), [])

    def test_operator_commands_wait_for_an_issuance_in_another_instance(self):
        """A second Exchange on the same database, as the operator CLI process opens one.

        Each instance opens the lock file itself, so their flocks are independent, as across processes.
        """
        operator = Exchange(self.exchange.db.execute("PRAGMA database_list").fetchone()[2], self.gateway,
                            Keys(fetch=lambda: {"keys": [self.signer.jwk]}), AUDIENCE)
        entered, release, events = threading.Event(), threading.Event(), []
        create = self.gateway.create

        def paused(*args):
            entered.set()
            release.wait(10)
            return create(*args)

        self.gateway.create = paused
        issuing = threading.Thread(target=lambda: events.append(("key", self.mint()["key"])))
        ending = threading.Thread(target=lambda: (operator.end(self.session), events.append(("end", None))))
        issuing.start()
        self.assertTrue(entered.wait(10))
        ending.start()
        ending.join(0.5)
        self.assertTrue(ending.is_alive(), "the operator's end waits for the issuance")
        release.set()
        issuing.join(10)
        ending.join(10)
        self.assertEqual([kind for kind, _ in events], ["key", "end"])
        self.assertEqual(self.gateway.list(), [], "the key issued first is deleted by the end")

    def test_issue_validates_its_inputs(self):
        for expected, models, budget, lease in (({"unknown": "x"}, ["*"], 1, 1), (dict(EXPECTED), [], 1, 1),
                                                (dict(EXPECTED), ["*"], 0, 1), (dict(EXPECTED), ["*"], 1, 0)):
            with self.assertRaises(ValueError):
                self.exchange.issue(expected, models, budget, lease)


class HttpTests(unittest.TestCase):
    """The runner-facing HTTP surface."""

    def setUp(self):
        ExchangeTests.setUp(self)
        clock = patch.object(exchange.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.server = server(self.exchange, "127.0.0.1:0")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, token=None):
        request = urllib.request.Request(self.base + path, method="POST", data=b"",
                                         headers={"Authorization": "Bearer " + token} if token else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_key_and_end_over_http(self):
        status, body = self.post(f"/exchange/sessions/{self.session}/key", self.signer.token())
        self.assertEqual(status, 200)
        self.assertTrue(body["key"].startswith("sk-agw-"))
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/key")[0], 401)
        self.assertEqual(self.post("/exchange/other", self.signer.token())[0], 404)
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/end", self.signer.token())[0], 200)
        self.assertEqual(self.gateway.list(), [])


class BusyTests(unittest.TestCase):

    def test_a_busy_exchange_answers_503(self):
        with patch.object(exchange, "MAX_REQUESTS", 0):
            busy = server(None, "127.0.0.1:0")
        threading.Thread(target=busy.serve_forever, daemon=True).start()
        self.addCleanup(busy.shutdown)
        request = urllib.request.Request(f"http://127.0.0.1:{busy.server_address[1]}/exchange/x", method="POST", data=b"")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
