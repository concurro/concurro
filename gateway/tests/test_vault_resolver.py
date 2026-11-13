"""Use real public/private TCP listeners with fake identities and synthetic provider material."""

import logging
import socket
import threading
import time
import unittest
from contextlib import contextmanager

import httpx
import uvicorn
from fastapi.testclient import TestClient

from vault_fixtures import ALICE, BOB, OPENAI, VaultFixture

import vault

SERVICE, GATEWAY = "a" * 64, "b" * 64


@contextmanager
def listeners(public, private):
    """Run the actual ASGI socket dispatcher on two loopback ports and always stop it."""
    with socket.create_server(("127.0.0.1", 0)) as one, socket.create_server(("127.0.0.1", 0)) as two:
        app = vault.ListenerApps(public, private, two.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", proxy_headers=False, server_header=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [one, two]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        try:
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise AssertionError("isolated vault did not start")
            yield f"http://127.0.0.1:{one.getsockname()[1]}", f"http://127.0.0.1:{two.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=3)
            if thread.is_alive():
                raise AssertionError("isolated vault did not stop")


class ResolverTests(VaultFixture, unittest.TestCase):
    """Exercise principal, accepted socket, exact-version and response-redaction boundaries."""

    def setUp(self):
        """Reuse the existing fake provider transport; enable separate synthetic private principals."""
        super().setUp()
        self.public = vault.create_app(self.store, self.identity, vault.Validator(self.transport), SERVICE, (GATEWAY,))
        self.private = vault.create_resolver(self.store, GATEWAY)

    def test_socket_boundary_cannot_be_selected_by_host_forwarded_headers_or_principal(self):
        """Only the accepted private socket plus gateway bearer may return material."""
        entry = self.put("openai-api-key", OPENAI).json()
        reference = {"user_id": 101, "route": "openai-api-key", "record_id": entry["record_id"], "version": 1}
        with listeners(self.public, self.private) as (public, private), httpx.Client(timeout=3) as client:
            for token in (ALICE, SERVICE, GATEWAY):
                response = client.post(
                    public + "/resolve",
                    json=reference,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Host": private.removeprefix("http://"),
                        "X-Forwarded-Port": private.rsplit(":", 1)[1],
                    },
                )
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(OPENAI, response.text)
            before = len(self.transport.calls)
            for token in (ALICE, SERVICE, "wrong"):
                self.assertEqual(
                    client.post(
                        private + "/resolve", json=reference, headers={"Authorization": "Bearer " + token}
                    ).status_code,
                    401,
                )
            self.assertEqual(len(self.transport.calls), before)
            result = client.post(private + "/resolve", json=reference, headers={"Authorization": "Bearer " + GATEWAY})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["secret"], OPENAI)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(
                client.get(public + "/events", headers={"Authorization": "Bearer " + GATEWAY}).status_code, 404
            )

    def test_private_bearers_never_forward_to_github_on_public_user_routes(self):
        """A mistaken private-to-public request is rejected before identity lookup or provider validation."""
        client = TestClient(self.public)
        for token in (SERVICE, GATEWAY):
            for method, path, body in (
                ("GET", "/credentials", None),
                ("PUT", "/credentials/openai-api-key", {"secret": OPENAI}),
            ):
                self.assertEqual(
                    client.request(method, path, json=body, headers={"Authorization": "Bearer " + token}).status_code,
                    401,
                )
        self.assertEqual(self.transport.calls, [])

    def test_version_events_and_errors_do_not_cross_owner_or_echo_material(self):
        """A replaced version is unusable, metadata cannot resolve it, and malformed errors are fixed."""
        first = self.put("openai-api-key", OPENAI).json()
        self.transport.accepted.add(OPENAI + "2")
        self.put("openai-api-key", OPENAI + "2", token=BOB)
        private = TestClient(self.private)
        headers = {"Authorization": "Bearer " + GATEWAY}
        wrong = {"user_id": 202, "route": "openai-api-key", "record_id": first["record_id"], "version": 1}
        self.assertEqual(private.post("/resolve", headers=headers, json=wrong).status_code, 404)
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().debug("synthetic scan control")
            response = private.post(
                "/resolve",
                headers=headers,
                json={
                    "user_id": "synthetic-sensitive-input",
                    "route": "openai-api-key",
                    "record_id": first["record_id"],
                    "version": 1,
                },
            )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("synthetic-sensitive-input", response.text)
        event_page = private.get("/events", headers=headers).json()
        self.assertEqual({event["user_id"] for event in event_page["events"]}, {101, 202})
        for secret in (OPENAI, OPENAI + "2", ALICE, BOB, SERVICE, GATEWAY):
            self.assertNotIn(secret, response.text + str(logs.output) + str(event_page))
        self.assertEqual(private.get("/events", headers={"Authorization": "Bearer " + ALICE}).status_code, 401)
