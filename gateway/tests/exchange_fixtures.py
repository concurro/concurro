"""Signed identities and isolated exchange state shared by gateway and runner tests."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exchange"))
from exchange import ISSUER, Exchange, Keys


AUDIENCE = "concurro-gateway"


# Sessions use the real clock; tokens are dated relative to it.
NOW = int(time.time())


EXPECTED = {"repository_id": "1001", "repository_owner_id": "2002",
            "job_workflow_ref": "concurro/concurro/.github/workflows/gateway-proof.yml@refs/heads/main",
            "job_workflow_sha": "a" * 40, "event_name": "workflow_dispatch", "ref": "refs/heads/main"}


BINDING = {"repository": "upstream/project", "issue": 7, "role": "plan:claude", "generation": 1,
           "config_sha": "b" * 40, "base": "c" * 40, "upstream": "upstream/project", "fork": "owner/project",
           "phase": "cooperative"}


TURN = {"request": {"provider": "claude", "stage": "plan"}}


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
                   "runner_environment": "github-hosted", "run_id": "77", "run_attempt": "1", **EXPECTED, "ref": getattr(self, "ref", EXPECTED["ref"])}
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


class ExchangeFixture:
    """Allocate a private database, signing key and gateway for each test."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.signer = Signer(directory.name)
        self.gateway = FakeGateway()
        self.exchange = Exchange(str(Path(directory.name) / "exchange.db"), self.gateway,
                                 Keys(fetch=lambda: {"keys": [self.signer.jwk]}), AUDIENCE)
        self.session = self.exchange.issue(dict(EXPECTED), ["gpt-*", "claude-*"], 100000, 3600, binding=BINDING)
        self.signer.ref = "refs/heads/concurro/runs/" + self.session

    def mint(self, **overrides):
        return self.exchange.key(self.session, self.signer.token(**overrides), now=NOW)
