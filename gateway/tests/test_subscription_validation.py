"""Both subscription candidates through the actual owner API, encryption and mutation fences."""

import http.client
import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from vault_fixtures import ALICE, BOB, OPENAI, VaultFixture

import vault
from spike_fixtures import FakeServices, fake_server

TOKENS = {
    "chatgpt-subscription": ("SYNTHETIC_CHATGPT_ALICE", "SYNTHETIC_CHATGPT_BOB"),
    "claude-subscription": ("SYNTHETIC_CLAUDE_ALICE", "SYNTHETIC_CLAUDE_BOB"),
}


class Probes:
    """A fake provider validates exact candidate/model/account tuples, never operator material."""

    def __init__(self):
        """Keep configurable fake failures and a synthetic call journal."""
        self.calls, self.mode, self.hook = [], {}, None
        self.accounts = {
            token: "account-" + name
            for tokens in TOKENS.values()
            for token, name in zip(tokens, ("alice", "bob"), strict=True)
        }

    @contextmanager
    def __call__(self, url, headers, body):
        """Return real JSON/SSE bytes behind the production validator transport seam."""
        route = "chatgpt-subscription" if "/backend-api/codex/" in url else "claude-subscription"
        secret = headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
        if self.hook:
            self.hook(secret)
        mode = self.mode.get((secret, body["model"]), "ok")
        status = 200
        if secret not in self.accounts:
            status = 401
        elif (
            route == "chatgpt-subscription"
            and headers.get("ChatGPT-Account-Id") != self.accounts[secret]
            and mode != "ignore_account"
        ):
            status = 403
        elif mode in (401, 429, 503, 404):
            status = mode
        if mode == "disconnect":
            raise http.client.IncompleteRead(secret.encode(), 100)
        if status != 200:
            raw = json.dumps({"error": {"message": secret}}).encode()
            media = "application/json"
        elif mode == "scalar_error":
            raw, media = json.dumps({"error": secret}).encode(), "application/json"
        elif mode == "invalid":
            raw, media = secret.encode(), "application/json"
        else:
            values = (
                [{"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}]
                if route == "chatgpt-subscription"
                else [
                    {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
                    {"type": "message_delta", "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
            )
            if mode == "incomplete":
                values = values[:-1]
            raw = b"".join(b"data: " + json.dumps(value).encode() + b"\n\n" for value in values)
            if mode == "event_error":
                raw = b"event: error\ndata: " + json.dumps({"detail": secret}).encode() + b"\n\n"
            media = "text/event-stream"
        yield status, media, io.BytesIO(raw)


class SubscriptionTests(VaultFixture, unittest.TestCase):
    """Exercise complete owner writes with only upstream identity/provider transport faked."""

    def setUp(self):
        """Reuse the existing GitHub/envelope/store fixture and inject streaming probes."""
        super().setUp()
        self.store.clock = lambda: self.now
        self.probes = Probes()
        self.validator = vault.Validator(self.transport, probe=self.probes)
        self.client = TestClient(
            vault.create_app(self.store, self.identity, self.validator, "metadata-secret", ("gateway-secret",))
        )

    def candidate(self, route, secret=None, token=ALICE, models=None, account_id=None, client=None):
        """Submit one candidate with two server-configured-default model examples."""
        secret = secret or TOKENS[route][0]
        body = {"secret": secret, "models": models if models is not None else ["model-a", "model-b", "model-a"]}
        if route == "chatgpt-subscription":
            body["account_id"] = (
                account_id if account_id is not None else "account-alice" if token == ALICE else "account-bob"
            )
        return (client or self.client).put(
            "/credentials/" + route, headers={"Authorization": "Bearer " + token}, json=body
        )

    def test_two_accounts_each_provider_are_isolated_and_account_is_versioned(self):
        """Both routes validate all models once, encrypt metadata and expose no secret/identity claim."""
        for route, tokens in TOKENS.items():
            for token, github, owner in zip(tokens, (ALICE, BOB), (101, 202), strict=True):
                result = self.candidate(route, token, github)
                self.assertEqual(result.status_code, 200, result.text)
                entry = result.json()
                self.assertEqual(entry["models"], ["model-a", "model-b"])
                self.assertFalse(entry["expiry_known"])
                self.assertIsNone(entry["verified_account"])
                resolved = self.store.resolve(owner, route, entry["record_id"], 1)
                self.assertEqual(resolved["secret"], token)
                self.assertEqual(
                    resolved["account"],
                    {"chatgpt_account_id": "account-alice" if owner == 101 else "account-bob"}
                    if route == "chatgpt-subscription"
                    else None,
                )
                self.assertNotIn(token, result.text)
        self.assertEqual(len(self.probes.calls), 8)
        self.assertTrue(all("/models" not in call["url"] for call in self.probes.calls))
        for call in self.probes.calls:
            self.assertTrue(call["body"]["stream"])
            self.assertNotIn("x-api-key", call["headers"])
            if "api.anthropic.com/" in call["url"]:
                self.assertIn("oauth-2025-04-20", call["headers"]["anthropic-beta"])
                self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
                self.assertNotIn("ChatGPT-Account-Id", call["headers"])
        public = self.request("GET", "/credentials").text + json.dumps(self.store.events())
        for token in sum(TOKENS.values(), ()):
            self.assertNotIn(token, public)
        self.assertNotIn("account-alice", public)
        self.assertNotIn("account-bob", public)

    def test_invalid_or_transient_second_model_preserves_entire_previous_version(self):
        """No partial model coverage or retirement is published after any rejected model."""
        for route in TOKENS:
            original = self.candidate(route).json()
            secret = TOKENS[route][0]
            for mode in (401, 429, 503, 404, "scalar_error", "invalid", "incomplete", "event_error", "disconnect"):
                with self.subTest(route=route, mode=mode):
                    before = self.store.events()
                    self.probes.mode[(secret, "model-b")] = mode
                    reply = self.candidate(route)
                    self.assertEqual(reply.status_code, 422 if mode in (401, 404) else 502)
                    self.assertNotIn(secret, reply.text)
                    self.assertEqual(self.store.events(), before)
                    self.assertEqual(self.store.entry(101, route, 1)["models"], original["models"])
                    self.assertEqual(self.store.resolve(101, route, original["record_id"], 1)["secret"], secret)
            self.probes.mode.clear()

    def test_account_rejection_and_ignored_account_success_never_verify_identity(self):
        """Wrong/missing headers are refused, while an accepting backend does not prove identity."""
        route = "chatgpt-subscription"
        self.assertEqual(self.candidate(route, account_id="wrong-account").status_code, 422)
        self.assertEqual(self.candidate(route, account_id="").status_code, 422)
        self.assertEqual(self.candidate(route, account_id="bad\r\nheader").status_code, 422)
        self.probes.mode[(TOKENS[route][0], "model-a")] = "ignore_account"
        self.probes.mode[(TOKENS[route][0], "model-b")] = "ignore_account"
        result = self.candidate(route, account_id="ignored-account")
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json()["verified_account"])
        self.assertIn("Supplied", result.json()["account_label"])

    def test_empty_repeated_revoke_and_restart_fence_inflight_candidates(self):
        """Missing-slot revocation persists a tombstone without synthetic credential versions/events."""
        route = "claude-subscription"
        initial = self.store.revision(101, route)
        self.probes.hook = lambda _secret: (self.store.revoke(101, route), self.store.revoke(101, route))
        reply = self.candidate(route)
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(self.store.listing(101), [])
        self.assertEqual(self.store.events()["events"], [])
        other = vault.Store(
            self.store.path if hasattr(self.store, "path") else self.directory / "vault.db", self.envelope
        )
        self.addCleanup(other.close)
        self.assertGreater(other.revision(101, route), initial)
        with self.assertRaises(vault.VaultError) as error:
            other.put(vault.User(101, "alice"), route, "old-candidate", ["model-a"], expected_revision=initial)
        self.assertEqual(error.exception.status, 409)
        self.probes.hook = None
        self.assertEqual(self.candidate(route).json()["version"], 1)
        self.assertTrue(all(e["version"] > 0 for e in self.store.events()["events"]))

    def test_overlapping_candidates_use_same_transaction_fence_across_connections(self):
        """A slow older candidate cannot replace a newer successful owner's token."""
        route = "chatgpt-subscription"
        entered, release = threading.Event(), threading.Event()
        slow = "SYNTHETIC_OLDER_CANDIDATE"
        self.probes.accounts[slow] = "account-alice"

        def hold(secret):
            """Hold only the old candidate's first probe outside every Store transaction."""
            if secret == slow:
                entered.set()
                self.assertTrue(release.wait(3))

        self.probes.hook = hold
        other = vault.Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(other.close)
        second = TestClient(vault.create_app(other, self.identity, self.validator))
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.candidate, route, slow)
            try:
                self.assertTrue(entered.wait(3))
                result = self.candidate(route, client=second)
                self.assertEqual(result.status_code, 200, result.text)
            finally:
                release.set()
            stale = pending.result(3)
        self.assertEqual(stale.status_code, 409)
        entry = self.store.listing(101)[0]
        self.assertEqual(entry["version"], 1)
        self.assertEqual(self.store.resolve(101, route, entry["record_id"], 1)["secret"], TOKENS[route][0])

    def test_revoke_during_replacement_never_resurrects_existing_version(self):
        """Revocation remains authoritative after an older valid replacement finishes."""
        route = "claude-subscription"
        self.candidate(route)
        self.probes.hook = lambda _secret: self.store.revoke(101, route)
        reply = self.candidate(route)
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(self.store.listing(101)[0]["status"], "revoked")
        events = self.store.events()["events"]
        self.assertEqual(sum(e["type"] == "revoked" for e in events), 1)

    def test_untrusted_expiry_fields_and_opaque_tokens_remain_unknown(self):
        """User hints and unsigned-looking tokens never authenticate expiry/account claims."""
        route = "chatgpt-subscription"
        token = "eyJhbGciOiJub25lIn0.eyJleHAiOjEsImFjY291bnQiOiJmb3JnZWQifQ."
        self.probes.accounts[token] = "account-alice"
        result = self.request(
            "PUT",
            "/credentials/" + route,
            json={
                "secret": token,
                "models": ["model-a"],
                "account_id": "account-alice",
                "expires_at": "1970-01-01T00:00:01Z",
                "expiry_known": True,
            },
        )
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["expiry_known"])
        self.assertIsNone(result.json()["expires_at"])
        self.assertIsNone(result.json()["verified_account"])
        self.now += 400 * 86400
        self.assertEqual(self.store.listing(101)[0]["status"], "valid")

    def test_schema_two_migration_retains_material_and_seeds_mutation_fence(self):
        """Forward migration preserves exact records/events and adds only local CAS state."""
        route = "claude-subscription"
        original = self.candidate(route).json()
        before = self.store.events()
        self.store.db.execute("DROP TABLE credential_fences")
        self.store.db.execute("UPDATE vault_meta SET value='2' WHERE key='schema'")
        other = vault.Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(other.close)
        self.assertEqual(other.events(), before)
        self.assertEqual(other.resolve(101, route, original["record_id"], 1)["secret"], TOKENS[route][0])
        self.assertGreater(other.revision(101, route), 0)
        self.assertEqual(other.db.execute("SELECT value FROM vault_meta WHERE key='schema'").fetchone()[0], "3")

    def test_private_principals_and_malformed_models_never_probe_or_echo(self):
        """Secret-only principals cannot act as owners; malformed payloads remain fixed errors."""
        route = "claude-subscription"
        for bearer in ("metadata-secret", "gateway-secret"):
            reply = self.candidate(route, token=bearer)
            self.assertEqual(reply.status_code, 401)
        self.assertEqual(self.probes.calls, [])
        marker = TOKENS[route][0]
        for body in ({"secret": marker, "models": [marker]}, {"secret": {"secret": marker}, "models": ["model-a"]}):
            reply = self.request("PUT", "/credentials/" + route, json=body)
            self.assertEqual(reply.status_code, 422)
            self.assertNotIn(marker, reply.text)

    def test_validation_transport_does_not_follow_redirects(self):
        """A redirect never forwards candidate authentication to another endpoint."""

        class Redirect(FakeServices):
            """Use the existing loopback wire fixture to emit only a redirect."""

            def provider(self, handler, body):
                """Record the one synthetic request, then redirect to a forbidden second call."""
                self.calls.append(body)
                return handler.json_reply(302, {}, {"Location": "/stolen"})

        state = Redirect()
        with fake_server(state) as port:
            with vault.http_probe(
                "http://127.0.0.1:" + str(port) + "/first", {"Authorization": "Bearer synthetic"}, {}
            ) as (status, _media, _response):
                self.assertEqual(status, 302)
        self.assertEqual(len(state.calls), 1)

    def test_absolute_probe_deadline_covers_delayed_headers_and_partial_line_drip(self):
        """Slow header/byte progress cannot outlive one model's total validation deadline or replace old material."""

        class Slow(FakeServices):
            """Send synthetic bytes slowly enough to keep an inactivity-only timeout alive."""

            mode = "headers"

            def provider(self, handler, body):
                """Delay headers or drip an unfinished SSE frame until the validator closes it."""
                try:
                    time.sleep(0.5 if self.mode == "headers" else 0.1)
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/event-stream")
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    for _ in range(50):
                        handler.wfile.write(b"x")
                        handler.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        route = "claude-subscription"
        original = self.candidate(route).json()
        before = self.store.events()
        state = Slow()
        with fake_server(state) as port:

            @contextmanager
            def local(url, headers, body):
                """Remap only the fixed upstream origin while retaining the actual deadline transport."""
                with vault.http_probe("http://127.0.0.1:" + str(port) + "/messages", headers, body) as response:
                    yield response

            self.validator.probe = local
            for mode in ("headers", "drip"):
                state.mode = mode
                start = time.monotonic()
                with patch.object(vault, "VALIDATION_SECONDS", 0.2):
                    result = self.candidate(route)
                self.assertEqual(result.status_code, 502, result.text)
                self.assertLess(time.monotonic() - start, 0.8)
                self.assertEqual(self.store.events(), before)
                self.assertEqual(self.store.resolve(101, route, original["record_id"], 1)["secret"], TOKENS[route][0])

    def test_api_key_candidate_uses_same_cross_connection_revision_fence(self):
        """API discovery stays compatible while a slow stale API replacement cannot overwrite another candidate."""
        entered, release = threading.Event(), threading.Event()
        route, slow = "openai-api-key", OPENAI + "-slow"
        self.transport.accepted.add(slow)
        models = self.validator.models

        def held(route, secret):
            """Block only the old API candidate outside all Store transactions."""
            if secret == slow:
                entered.set()
                self.assertTrue(release.wait(3))
            return models(route, secret)

        other = vault.Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(other.close)
        second = TestClient(vault.create_app(other, self.identity, self.validator))
        with patch.object(self.validator, "models", side_effect=held), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.candidate, route, slow)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.candidate(route, OPENAI, client=second).status_code, 200)
            finally:
                release.set()
            self.assertEqual(pending.result(3).status_code, 409)
        entry = self.store.listing(101)[0]
        self.assertEqual(self.store.resolve(101, route, entry["record_id"], 1)["secret"], OPENAI)
