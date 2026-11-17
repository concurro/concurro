"""Opt-in local Vault, pinned gateway and hosted Claude relay acceptance fixture.

All identity and provider endpoints are synthetic. Supply frozen macOS native
packages and the accepted Claude provider fixture source explicitly.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "gateway/exchange"), str(ROOT / "gateway/vault")]

from phase5_spike import ALICE, CSRF_HEADER, RuntimeTests as Phase5Tests
from spike_fixtures import request
from vault import relay_approver


class WebSocketPeer:
    """Exercise the hosted Responses bridge with masked client frames."""

    def __init__(self, endpoint, key):
        """Upgrade one local WebSocket connection with the scoped gateway key."""
        parsed = urlsplit(endpoint)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=15)
        self.socket.settimeout(240)
        nonce = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall((
            "GET /v1/responses HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Connection: Upgrade\r\nUpgrade: websocket\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {key}\r\n\r\n"
        ).encode())
        head = bytearray()
        while not head.endswith(b"\r\n\r\n"):
            chunk = self.socket.recv(1)
            if not chunk:
                raise EOFError("WebSocket upgrade closed")
            head.extend(chunk)
        if not head.startswith(b"HTTP/1.1 101"):
            raise AssertionError(head.decode(errors="replace"))

    def exact(self, length):
        """Read one complete WebSocket frame field."""
        result = bytearray()
        while len(result) < length:
            chunk = self.socket.recv(length - len(result))
            if not chunk:
                raise EOFError("WebSocket peer closed")
            result.extend(chunk)
        return bytes(result)

    def send(self, value):
        """Send one masked JSON event to the local bridge."""
        data = json.dumps(value, separators=(",", ":")).encode()
        mask = os.urandom(4)
        length = len(data)
        header = (bytes([length | 0x80]) if length < 126 else
                  b"\xfe" + struct.pack("!H", length) if length < 65536 else
                  b"\xff" + struct.pack("!Q", length))
        self.socket.sendall(b"\x81" + header + mask + bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(data)
        ))

    def recv(self):
        """Read one JSON text event from the local bridge."""
        first, second = self.exact(2)
        length = second & 0x7f
        if length == 126:
            length = struct.unpack("!H", self.exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.exact(8))[0]
        if first & 0x0f != 1:
            raise AssertionError(f"unexpected WebSocket opcode {first & 0x0f}")
        return json.loads(self.exact(length))

    def close(self):
        """Close the local client socket."""
        self.socket.close()


class HostedRelayTests(Phase5Tests):
    """Run the real owner, resolver, Exchange and pinned gateway with local relay peers."""

    options = None

    def setUp(self):
        """Start only task-owned provider and verifier processes before the real Vault API."""
        self.processes = []
        self.synthetic_keychain_account = None
        self.addCleanup(self.stop_processes)
        self.service_token = secrets.token_urlsafe(32)
        token_file = self.options.output / "service-token"
        token_file.write_text(self.service_token)
        token_file.chmod(0o600)
        mixed_case = self._testMethodName == "test_mixed_routes"
        codex_case = self._testMethodName.startswith("test_codex") or mixed_case
        first_route = "claude" if mixed_case else "codex" if codex_case else "claude"
        provider_args = [
            str(self.options.provider_python), str(self.options.relay_root / "native/fixture_server.py"),
            "--route", first_route,
            "--source", str(self.options.codex_provider_source if first_route == "codex" else self.options.provider_source),
            "--certs", str(self.options.certs),
        ]
        if codex_case and not mixed_case:
            provider_args += ["--tool-loop", "--stop-file", str(self.options.output / "provider.stop")]
        if self._testMethodName == "test_two_links_two_turns":
            provider_args += ["--connections", "2"]
        if self._testMethodName == "test_inflight_cancel":
            provider_args += ["--delay-inference", "30"]
        if self._testMethodName == "test_codex_ws_cancel":
            provider_args += ["--delay-inference", "8"]
        self.provider = subprocess.Popen(
            provider_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        self.processes.append(self.provider)
        first = self.provider.stdout.readline()
        self.assertTrue(first, "synthetic provider did not bind")
        port = json.loads(first)["port"]
        codex_port = port
        if mixed_case:
            self.codex_provider = subprocess.Popen(
                [str(self.options.codex_provider_python or self.options.provider_python),
                 str(self.options.relay_root / "native/fixture_server.py"),
                 "--route", "codex", "--source", str(self.options.codex_provider_source),
                 "--certs", str(self.options.certs), "--stop-file", str(self.options.output / "provider.stop")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
            self.processes.append(self.codex_provider)
            first = self.codex_provider.stdout.readline()
            self.assertTrue(first, "synthetic Codex provider did not bind")
            codex_port = json.loads(first)["port"]
        hosted_env = dict(os.environ)
        hosted_env.update(
            CONCURRO_NATIVE_BUNDLE=str(self.options.concurro_package / "native-runtime"),
            CONCURRO_NATIVE_OUTPUT=str(self.options.output / "concurro-sessions"),
        )
        self.hosted = subprocess.Popen(
            [str(self.options.hosted_bin), "--certificate", str(self.options.certs / "verifier.pem"),
             "--private-key", str(self.options.certs / "verifier.key"),
             "--registry", str(self.options.output / "registry.json"),
             "--service-token", str(token_file),
             "--verification-url", "https://fixture.invalid/credentials/relay",
             "--relay-listen", "127.0.0.1:17443", "--private-listen", "127.0.0.1:17450",
             "--provider-host", "127.0.0.1", "--provider-port", str(port),
             "--provider-ca", str(self.options.certs / "chain-ca.pem"),
             "--codex-provider-host", "127.0.0.1", "--codex-provider-port", str(codex_port)],
            cwd=self.options.relay_root, env=hosted_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        self.processes.append(self.hosted)
        self.assertIn("listening", self.hosted.stdout.readline())
        self.relay_approve = relay_approver("http://127.0.0.1:17450", self.service_token)
        super().setUp()
        self.exchange.inference.upstreams["claude-subscription"] = "https://api.anthropic.com/v1"
        self.exchange.inference.verifier_url = "http://127.0.0.1:17450"
        self.exchange.inference.verifier_token = self.service_token
        if codex_case:
            self.bridge = subprocess.Popen(
                [str(self.options.responses_bridge_bin), "--listen", "127.0.0.1:0",
                 "--gateway", self.runtime.inference],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
            self.processes.append(self.bridge)
            ready = self.bridge.stdout.readline()
            self.assertTrue(ready.startswith("RESPONSES_BRIDGE_READY "), ready)
            self.codex_ws_bridge = "http://" + ready.strip().split()[-1]

    def stop_processes(self):
        """Reap only this fixture's process groups after the gateway evidence is saved."""
        for process in reversed(self.processes):
            if process.poll() is not None:
                continue
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(5)
        if hasattr(self, "hosted"):
            (self.options.output / "hosted.log").write_text(self.hosted.stdout.read())
        if hasattr(self, "serving"):
            (self.options.output / "sponsor.log").write_text(self.serving.stdout.read())
        if hasattr(self, "provider"):
            (self.options.output / "provider.log").write_text(self.provider.stdout.read())
        if hasattr(self, "codex_provider"):
            (self.options.output / "codex-provider.log").write_text(self.codex_provider.stdout.read())
        if hasattr(self, "bridge"):
            log = self.options.output / "bridge.log"
            if not log.exists():
                log.write_text(self.bridge.stdout.read())
        if self.synthetic_keychain_account:
            subprocess.run(
                ["/usr/bin/security", "delete-generic-password", "-s", "concurro-relay",
                 "-a", self.synthetic_keychain_account], capture_output=True, timeout=10,
            )

    def approve_owner(self, links=1, routes=None, portal=False, mismatched=None):
        """Register frozen Sponsor, then approve its exact routes through Vault or the owner portal."""
        routes = routes or ["claude-subscription"]
        sponsor_env = dict(os.environ)
        sponsor_env.update(
            CLAUDE_CODE_OAUTH_TOKEN="X" * 44,
            CONCURRO_NATIVE_BUNDLE=str(self.options.sponsor_package / "native-runtime"),
            CONCURRO_NATIVE_OUTPUT=str(self.options.output / "sponsor-sessions"),
            CONCURRO_NATIVE_PROVIDER_CA=str(self.options.certs / "chain-ca.pem"),
        )
        sponsor = str(self.options.sponsor_package / "bin/concurro-relay")
        state = str(self.options.output / "sponsor-state")
        if "chatgpt-subscription" in routes:
            token = "SYNTHETIC_CODEX_" + "X" * 69
            credential = {"token": token, "headers": [["chatgpt-account-id", "synthetic-account-101"]]}
            kept = base64.b64encode(json.dumps(credential, separators=(",", ":")).encode()).decode()
            self.synthetic_keychain_account = "chatgpt_subscription:" + str(Path(state).resolve()).encode().hex()
            command = ("add-generic-password -U -s concurro-relay -a \"" + self.synthetic_keychain_account
                       + "\" -l \"Concurro relay: ChatGPT subscription credential\" -w \"" + kept + "\"\n")
            seeded = subprocess.run(["/usr/bin/security", "-i"], input=command, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
        login = subprocess.Popen(
            [sponsor, "--state-dir", state, "login", "--server", "127.0.0.1:17443",
             "--server-ca", str(self.options.certs / "internal-ca.pem"),
             *(item for route in routes for item in ("--route", route.replace("-", "_"))),
             "--name", "synthetic-hosted-sponsor"],
            env=sponsor_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        self.processes.append(login)
        code = None
        while code is None:
            line = login.stdout.readline()
            self.assertTrue(line, "Sponsor login exited before displaying its code")
            matched = re.search(r"code ([A-Z]{4}-[A-Z]{4})", line)
            if matched:
                code = matched.group(1)
        body = {"code": code, "name": "Synthetic Sponsor", "routes": routes,
                "mutation_id": "hostedfixture0001"}
        if portal:
            self.owner("101")
            if mismatched:
                wrong = self.browser.post(
                    "/api/me/credentials/relay", headers={CSRF_HEADER: "1"},
                    json={**body, "routes": mismatched, "mutation_id": "hostedfixturewrong"},
                )
                self.assertGreaterEqual(wrong.status_code, 400, wrong.text)
                self.assertEqual([group["entries"] for group in self.browser.get("/api/me/credentials").json()["providers"]], [[], []])
            approved = self.browser.post("/api/me/credentials/relay", headers={CSRF_HEADER: "1"}, json=body)
            self.assertEqual(approved.status_code, 200, approved.text)
            entry = approved.json()
        else:
            status, _, reply = request(
                self.public_url + "/credentials/relay", "POST", body,
                [("Authorization", "Bearer " + ALICE)],
            )
            self.assertEqual(status, 200, reply)
            entry = json.loads(reply)
        self.assertEqual(login.wait(15), 0)
        serving = subprocess.Popen(
            [sponsor, "--state-dir", state, "run", "--links", str(links)],
            env=sponsor_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        self.processes.append(serving)
        self.serving = serving
        ready = set()
        while len(ready) < links:
            line = serving.stdout.readline()
            self.assertTrue(line, "Sponsor exited before its links opened")
            matched = re.search(r"link (\d+) up", line)
            if matched:
                ready.add(int(matched.group(1)))
        return entry

    def test_portal_claude_routes(self):
        """The owner portal approves Claude alone against the Rust registry's exact route set."""
        self.expect_inference = False
        approved = self.approve_owner(routes=["claude-subscription"], portal=True,
                                      mismatched=["chatgpt-subscription"])
        self.assertEqual(set(approved["entries"]), {"claude-subscription"})

    def test_portal_chatgpt_routes(self):
        """The owner portal approves ChatGPT alone against the Rust registry's exact route set."""
        self.expect_inference = False
        approved = self.approve_owner(routes=["chatgpt-subscription"], portal=True,
                                      mismatched=["claude-subscription"])
        self.assertEqual(set(approved["entries"]), {"chatgpt-subscription"})

    def test_portal_combined_routes(self):
        """The owner portal approves both routes atomically against the Rust registry."""
        self.expect_inference = False
        approved = self.approve_owner(routes=["claude-subscription", "chatgpt-subscription"], portal=True,
                                      mismatched=["claude-subscription"])
        self.assertEqual(set(approved["entries"]), {"claude-subscription", "chatgpt-subscription"})

    def test_codex_tool_loop(self):
        """Pinned Codex executes a real tool through Gateway and reuses one protected provider session."""
        entry = self.approve_owner(routes=["chatgpt-subscription"])
        grant = self.admit("chatgpt-subscription", "101")
        _, answer = self.native(grant, self.root / "native-codex", "Use the supplied tool and answer in the schema.")
        self.assertEqual(answer, {"answer": "TOOL_LOOP_DONE"})
        self.assertEqual((self.root / "native-codex/workspace/steps.txt").read_text(), "step-01\n")
        self.exchange.end(grant["session"])
        (self.options.output / "provider.stop").touch()
        provider = json.loads(self.provider.stdout.readline())
        self.assertEqual(provider["provider_connections"], 1)
        self.assertEqual(provider["transport_fields"], [[], [], []])
        self.assertEqual(len(provider["bindings"]), 3)
        self.assertEqual(provider["bindings"][2]["parent"], provider["bindings"][1]["response_id"])
        self.assertEqual(provider["bindings"][2]["input_types"], ["function_call_output"])
        self.assertLess(provider["bindings"][2]["body_bytes"], provider["bindings"][1]["body_bytes"])
        self.assertTrue(provider["bindings"][2]["validated_output_sha256"])
        bridge_lines, ingress = [], []
        while len(ingress) < 3:
            line = self.bridge.stdout.readline()
            self.assertTrue(line, "Responses bridge ended before logging all three turns")
            bridge_lines.append(line)
            if line.startswith("{"):
                ingress.append(json.loads(line))
        (self.options.output / "bridge.log").write_text("".join(bridge_lines))
        self.assertEqual(len(ingress), 3)
        self.assertEqual((ingress[0]["generate"], ingress[0]["detached"]), (False, True))
        self.assertEqual(ingress[2]["previous_response_id"], provider["bindings"][1]["response_id"])
        self.assertEqual(ingress[2]["input_types"], ["function_call_output"])
        self.assertLess(ingress[2]["body_bytes"], ingress[1]["body_bytes"])
        self.assertEqual(self.exchange.inference.leased, {})
        (self.root / "hosted-result.json").write_text(json.dumps({
            "record_id": entry["record"]["record_id"],
            "answer": answer,
            "provider_connections": provider["provider_connections"],
            "bindings": provider["bindings"],
            "ingress": ingress,
        }, indent=2) + "\n")

    def test_mixed_routes(self):
        """One approval carries concurrent Claude and Codex sessions on separate native links."""
        entry = self.approve_owner(links=2, routes=["claude-subscription", "chatgpt-subscription"])
        self.assertEqual(set(entry["entries"]), {"claude-subscription", "chatgpt-subscription"})
        grants = [self.admit(route, "101", issue=issue) for route, issue in
                  (("claude-subscription", 81), ("chatgpt-subscription", 82))]
        with ThreadPoolExecutor(max_workers=2) as workers:
            claude = workers.submit(self.gateway_call, grants[0], "mixed")
            codex = workers.submit(self.gateway_call, grants[1], "mixed")
            claude_status, claude_body = claude.result(240)
            codex_status, codex_body = codex.result(240)
        self.assertEqual(claude_status, 200, claude_body)
        self.assertIn(b"message_stop", claude_body)
        self.assertEqual(codex_status, 200, codex_body)
        self.assertIn(b"response.completed", codex_body)
        wrong_route = [self.gateway_call(grants[0], model="gpt-test")[0],
                       self.gateway_call(grants[1], model="claude-test")[0]]
        self.assertTrue(all(status >= 400 for status in wrong_route), wrong_route)
        for grant in grants:
            self.exchange.end(grant["session"])
        self.assertEqual(self.exchange.inference.leased, {})
        ended = [self.gateway_call(grant)[0] for grant in grants]
        self.assertTrue(all(status >= 400 for status in ended), ended)
        (self.options.output / "provider.stop").touch()
        claude_provider = json.loads(self.provider.stdout.readline())
        codex_provider = json.loads(self.codex_provider.stdout.readline())
        self.assertEqual(len(claude_provider["requests"]), 1)
        self.assertEqual(len(codex_provider["calls"]), 1)
        self.assertEqual(codex_provider["transport_fields"], [[]])
        self.assertTrue(codex_provider["calls"][0]["auth_matches"])
        (self.root / "hosted-result.json").write_text(json.dumps({
            "record_ids": {route: value["record"]["record_id"] for route, value in entry["entries"].items()},
            "statuses": {"claude": claude_status, "codex": codex_status},
            "wrong_route_statuses": wrong_route,
            "ended_statuses": ended,
            "provider_requests": {"claude": len(claude_provider["requests"]),
                                  "codex": len(codex_provider["calls"])},
            "released": not self.exchange.inference.leased,
        }, indent=2) + "\n")

    def test_codex_ws_denials(self):
        """The real bridge preserves key, model and budget denial categories."""
        self.approve_owner(routes=["chatgpt-subscription"])
        grant = self.admit("chatgpt-subscription", "101", budget=4)
        wrong_key = WebSocketPeer(self.codex_ws_bridge, "invalid-key")
        wrong_key.send({"type": "response.create", "model": grant["model"], "input": "fixture"})
        auth = wrong_key.recv()
        wrong_key.close()
        wrong_model = WebSocketPeer(self.codex_ws_bridge, grant["key"])
        wrong_model.send({"type": "response.create", "model": "wrong-model", "input": "fixture"})
        model = wrong_model.recv()
        wrong_model.close()
        client = WebSocketPeer(self.codex_ws_bridge, grant["key"])
        client.send({"type": "response.create", "model": grant["model"], "input": "fixture"})
        first = []
        while not any(event.get("type") == "response.completed" for event in first):
            first.append(client.recv())
        time.sleep(.5)
        client.send({"type": "response.create", "model": grant["model"], "input": "fixture"})
        budget = client.recv()
        client.close()
        self.assertEqual((auth["error"]["status"], auth["error"]["code"]), (401, "auth"))
        self.assertEqual((model["error"]["status"], model["error"]["code"]), (403, "model_denied"))
        self.assertEqual((budget["error"]["status"], budget["error"]["code"]), (429, "budget_denied"))
        unsupported = WebSocketPeer(self.codex_ws_bridge, grant["key"])
        unsupported.send({"type": "response.create", "model": grant["model"],
                          "input": "fixture", "background": True})
        background = unsupported.recv()
        unsupported.close()
        self.assertEqual(background["error"]["code"], "unsupported_transport")
        self.exchange.end(grant["session"])
        (self.options.output / "provider.stop").touch()
        provider = json.loads(self.provider.stdout.readline())
        self.assertEqual(len(provider["bindings"]), 1)
        self.assertEqual(provider["transport_fields"], [[]])
        observations = self.exchange.inference.events(grant["binding"]["repository"], grant["binding"]["issue"])
        self.assertEqual(len([event for event in observations if event.get("usage")]), 1)
        (self.root / "hosted-result.json").write_text(json.dumps({
            "denials": [auth["error"], model["error"], budget["error"], background["error"]],
            "provider_requests": len(provider["bindings"]),
            "completed_events": len(first),
            "usage_observations": len([event for event in observations if event.get("usage")]),
        }, indent=2) + "\n")

    def test_codex_ws_cancel(self):
        """A cancel before the first provider event stops its gateway request while another succeeds."""
        self.approve_owner(links=2, routes=["chatgpt-subscription"])
        first = self.admit("chatgpt-subscription", "101", issue=91)
        second = self.admit("chatgpt-subscription", "101", issue=92)
        canceled = WebSocketPeer(self.codex_ws_bridge, first["key"])
        canceled.send({"type": "response.create", "model": first["model"], "input": "cancel"})
        signal = json.loads(self.provider.stdout.readline())
        self.assertEqual(signal["event"], "provider_request_received")
        started = time.monotonic()
        canceled.send({"type": "response.cancel"})
        canceled.socket.settimeout(3)
        closed = canceled.socket.recv(2)
        elapsed = time.monotonic() - started
        canceled.close()
        self.assertLess(elapsed, 3)
        self.assertTrue(not closed or closed[0] & 0x0f == 8, closed)
        independent = WebSocketPeer(self.codex_ws_bridge, second["key"])
        independent.send({"type": "response.create", "model": second["model"], "input": "independent"})
        events = []
        while not any(event.get("type") == "response.completed" for event in events):
            events.append(independent.recv())
        independent.close()
        for _ in range(100):
            second_observations = self.exchange.inference.events(second["binding"]["repository"], second["binding"]["issue"])
            if any(event.get("usage") for event in second_observations):
                break
            time.sleep(.1)
        self.exchange.end(first["session"])
        self.exchange.end(second["session"])
        (self.options.output / "provider.stop").touch()
        line = self.provider.stdout.readline()
        while line and not line.startswith('{"calls"'):
            line = self.provider.stdout.readline()
        self.assertTrue(line, "synthetic provider did not emit its final observation")
        provider = json.loads(line)
        self.assertEqual(len(provider["bindings"]), 2)
        self.assertEqual(len([event for event in events if event["type"] == "response.completed"]), 1)
        first_observations = self.exchange.inference.events(first["binding"]["repository"], first["binding"]["issue"])
        second_observations = self.exchange.inference.events(second["binding"]["repository"], second["binding"]["issue"])
        self.assertFalse(any(event.get("usage") for event in first_observations), first_observations)
        self.assertFalse(any(event.get("completed") for event in first_observations), first_observations)
        self.assertTrue(any(event.get("usage") for event in second_observations), second_observations)
        self.assertEqual(self.exchange.inference.leased, {})
        (self.root / "hosted-result.json").write_text(json.dumps({
            "provider_received_cancelled_request": True,
            "cancel_close_seconds": elapsed,
            "provider_requests": len(provider["bindings"]),
            "independent_completed": True,
            "cancelled_usage_observations": len([event for event in first_observations if event.get("usage")]),
            "independent_usage_observations": len([event for event in second_observations if event.get("usage")]),
        }, indent=2) + "\n")

    def gateway_call(self, grant, prompt="fixture", model=None):
        """Send one provider request through the pinned gateway's public inference port."""
        parsed = urlsplit(self.runtime.inference)
        codex = grant["provider"] == "codex"
        body = json.dumps(
            {"model": model or grant["model"], "input": prompt, "stream": True}
            if codex else
            {"model": model or grant["model"], "max_tokens": 32,
             "messages": [{"role": "user", "content": prompt}], "stream": True}
        )
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=240)
        try:
            connection.request("POST", "/v1/responses" if codex else "/v1/messages", body, {
                "Authorization": "Bearer " + grant["key"],
                "Content-Type": "application/json", "Accept": "text/event-stream",
            })
            answer = connection.getresponse()
            return answer.status, answer.read()
        finally:
            connection.close()

    def test_owner_gateway_relay(self):
        """Owner approval and exact Vault resolution feed two retained native H2 requests."""
        entry = self.approve_owner()
        grant = self.admit("claude-subscription", "101")
        replies = [self.gateway_call(grant, prompt) for prompt in ("first", "second")]
        for status, body in replies:
            self.assertEqual(status, 200)
            self.assertIn(b"message_start", body)
            self.assertIn(b"message_stop", body)
        deadline = self.exchange.db.execute(
            "SELECT deadline FROM sessions WHERE id=?", (grant["session"],)
        ).fetchone()[0]
        self.exchange.reconcile(now=deadline + 1)
        self.assertEqual(self.exchange.inference.leased, {})
        provider = json.loads(self.provider.stdout.readline())
        self.assertEqual([item["stream"] for item in provider["requests"]], [1, 3])
        self.assertNotIn("error", provider)
        self.assertEqual(len(self.exchange.inference.events(grant["binding"]["repository"],
                                                         grant["binding"]["issue"])), 2)
        result = {
            "gateway_image": self.runtime.identity["RepoDigests"],
            "sponsor_manifest_sha256": hashlib.sha256((self.options.sponsor_package / "manifest.json").read_bytes()).hexdigest(),
            "concurro_manifest_sha256": hashlib.sha256((self.options.concurro_package / "manifest.json").read_bytes()).hexdigest(),
            "record_id": entry["record"]["record_id"],
            "responses": [{"status": status, "bytes": len(body)} for status, body in replies],
            "provider_streams": [item["stream"] for item in provider["requests"]],
        }
        (self.root / "hosted-result.json").write_text(json.dumps(result, indent=2) + "\n")

    def test_two_links_two_turns(self):
        """Two admitted tasks retain independent native links and both complete SSE calls."""
        self.approve_owner(links=2)
        grants = [self.admit("claude-subscription", "101", issue=issue) for issue in (71, 72)]
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = list(workers.map(lambda item: self.gateway_call(item, "first"), grants))
            second = list(workers.map(lambda item: self.gateway_call(item, "second"), grants))
        for status, body in first + second:
            self.assertEqual(status, 200)
            self.assertIn(b"message_stop", body)
        self.exchange.end(grants[0]["session"])
        self.exchange.cancel(grants[1]["turn"])
        self.assertEqual(self.exchange.inference.leased, {})
        provider = json.loads(self.provider.stdout.readline())
        streams = [item["stream"] for connection in provider["connections"] for item in connection["requests"]]
        self.assertEqual(sorted(streams), [1, 1, 3, 3])
        self.assertEqual(len(provider["connections"]), 2)
        (self.root / "hosted-result.json").write_text(json.dumps({
            "sessions": [grant["session"] for grant in grants],
            "responses": [{"status": status, "bytes": len(body)} for status, body in first + second],
            "provider_streams_by_connection": [
                [item["stream"] for item in connection["requests"]] for connection in provider["connections"]
            ],
        }, indent=2) + "\n")

    def test_inflight_cancel(self):
        """Release an admitted native link while Claude has the complete request in flight."""
        self.approve_owner()
        grant = self.admit("claude-subscription", "101")
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(self.gateway_call, grant, "cancel")
            self.assertEqual(json.loads(self.provider.stdout.readline())["event"], "provider_request_received")
            self.assertFalse(pending.done())
            self.exchange.cancel(grant["turn"])
            try:
                status, body = pending.result(15)
            except (OSError, http.client.HTTPException):
                status, body = None, b""
        self.assertNotIn(b"message_stop", body)
        self.assertNotEqual(status, 200)
        self.assertEqual(self.exchange.inference.connections, {})
        self.assertEqual(self.exchange.inference.leased, {})
        (self.root / "hosted-result.json").write_text(json.dumps({
            "provider_received_request": True, "released_before_provider_response": True,
            "gateway_status": status, "completed_sse": False,
        }, indent=2) + "\n")


def main():
    """Run one requested local hosted fixture and retain its nonsecret evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--relay-root", type=Path, default=ROOT.parent / "concurro-relay")
    parser.add_argument("--hosted-bin", type=Path, required=True)
    parser.add_argument("--sponsor-package", type=Path, required=True)
    parser.add_argument("--concurro-package", type=Path, required=True)
    parser.add_argument("--provider-python", type=Path, required=True)
    parser.add_argument("--codex-provider-python", type=Path)
    parser.add_argument("--provider-source", type=Path, required=True)
    parser.add_argument("--codex-provider-source", type=Path)
    parser.add_argument("--clients", type=Path)
    parser.add_argument("--responses-bridge-bin", type=Path)
    parser.add_argument("--certs", type=Path, required=True)
    parser.add_argument("--case", choices=("test_owner_gateway_relay", "test_two_links_two_turns",
                                           "test_inflight_cancel", "test_codex_tool_loop",
                                           "test_mixed_routes", "test_codex_ws_denials",
                                           "test_codex_ws_cancel", "test_portal_claude_routes",
                                           "test_portal_chatgpt_routes", "test_portal_combined_routes"), default="test_owner_gateway_relay")
    args = parser.parse_args()
    if args.case in ("test_codex_tool_loop", "test_mixed_routes", "test_codex_ws_denials",
                     "test_codex_ws_cancel") and (not args.codex_provider_source or not args.clients
                                               or not args.responses_bridge_bin):
        parser.error("Codex cases require --codex-provider-source, --clients and --responses-bridge-bin")
    args.output.mkdir(parents=True, exist_ok=False)
    HostedRelayTests.options = args
    result = unittest.TestResult()
    HostedRelayTests(args.case).run(result)
    for _, failure in result.failures + result.errors:
        print(failure, file=sys.stderr)
    print(json.dumps({"passed": result.wasSuccessful(), "output": str(args.output)}), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
