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
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "gateway/exchange"), str(ROOT / "gateway/vault")]

from phase5_spike import ALICE, CSRF_HEADER, RuntimeTests as Phase5Tests
from spike_fixtures import request
from vault import relay_approver
from bundle import Bundle
from exchange import server as exchange_server
from inference import UPSTREAMS
from phase4_spike import CLI_VERSIONS, CapturingHandler
from relay import Runner
import agent_entry
import processes
import runner as agent_runner


class EventTimer:
    """Record the first private-hop SSE event while delegating writes unchanged."""

    def __init__(self, stream):
        """Wrap one handler response stream."""
        self.stream = stream
        self.first_event = None

    def write(self, data):
        """Timestamp the first event bytes passed toward agentgateway."""
        if self.first_event is None and b"event:" in data:
            self.first_event = time.monotonic()
        return self.stream.write(data)

    def __getattr__(self, name):
        """Preserve the underlying stream's remaining interface."""
        return getattr(self.stream, name)


class TimedHandler(CapturingHandler):
    """Record private-hop first-event and completion times by request identity."""

    def forward(self, snapshot, url, body):
        """Time one otherwise unchanged private inference forward."""
        started = time.monotonic()
        request = json.loads(body)
        if self.capture_cli_requests:
            captured = self.evidence_output / f"cli-request-{snapshot['request']}.json"
            captured.write_bytes(body)
            captured.chmod(0o600)
        items = request.get("input", request.get("messages", []))
        input_types = [block.get("type") for item in items if isinstance(item, dict)
                       for block in (item.get("content", []) if isinstance(item.get("content"), list)
                                     else [item]) if isinstance(block, dict)]
        tool_result_ids = [block["tool_use_id"] for item in items if isinstance(item, dict)
                           for block in (item.get("content", []) if isinstance(item.get("content"), list)
                                         else [item]) if isinstance(block, dict)
                           and block.get("type") == "tool_result" and block.get("tool_use_id")]
        original = self.wfile
        timer = EventTimer(original)
        self.wfile = timer
        try:
            return super().forward(snapshot, url, body)
        finally:
            self.wfile = original
            record = {
                "request": snapshot["request"],
                "body_bytes": len(body),
                "input_types": input_types,
                "tool_result_ids": tool_result_ids,
                "first_event_seconds": None if timer.first_event is None else timer.first_event - started,
                "completion_seconds": time.monotonic() - started,
            }
            self.server.hop_timings.append(record)
            with (self.evidence_output / "gateway-hop-timings.jsonl").open("a") as journal:
                journal.write(json.dumps(record) + "\n")


class SyntheticOidcHandler(BaseHTTPRequestHandler):
    """Serve one signed synthetic GitHub job identity to the real runner client."""

    def do_GET(self):
        """Return the bound JWT only to the expected local Actions token request."""
        if (self.path != "/token?fixture=1&audience=concurro-gateway"
                or self.headers.get("Authorization") != "bearer synthetic-actions-request"):
            self.send_error(403)
            return
        self.server.requests += 1
        body = json.dumps({"value": self.server.oidc_token}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        """Keep the synthetic JWT out of server logs."""


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
        sustained = self._testMethodName.endswith("_sustained")
        live = self._testMethodName.endswith("_live")
        if sustained or live:
            self.inference_handler = TimedHandler
            TimedHandler.evidence_output = self.options.output
            TimedHandler.capture_cli_requests = self.options.capture_cli_requests
        self.processes = []
        self.task_keychain_accounts = []
        self.addCleanup(self.stop_processes)
        self.service_token = secrets.token_urlsafe(32)
        token_file = self.options.output / "service-token"
        token_file.write_text(self.service_token)
        token_file.chmod(0o600)
        mixed_case = self._testMethodName == "test_mixed_routes"
        codex_case = self._testMethodName.startswith("test_codex") or mixed_case
        first_route = "claude" if mixed_case else "codex" if codex_case else "claude"
        provider_args = (
            [str(self.options.provider_python), str(self.options.relay_root / "native/sustained_claude_fixture.py"),
             "--source", str(self.options.provider_source), "--certs", str(self.options.certs),
             "--steps", str(self.options.steps), "--stop-file", str(self.options.output / "provider.stop")]
            if sustained and not codex_case else
            [str(self.options.provider_python), str(self.options.relay_root / "native/fixture_server.py"),
             "--route", first_route,
             "--source", str(self.options.codex_provider_source if first_route == "codex" else self.options.provider_source),
             "--certs", str(self.options.certs)]
        )
        if codex_case and not mixed_case:
            provider_args += ["--tool-loop", "--stop-file", str(self.options.output / "provider.stop")]
            if sustained:
                provider_args += ["--sustained", "--steps", str(self.options.steps)]
            if not live and self.options.codex_token_length != 85:
                provider_args += ["--codex-token-length", str(self.options.codex_token_length)]
        if sustained and not codex_case and self.options.expire_once_at >= 0:
            provider_args += ["--expire-once-at", str(self.options.expire_once_at)]
        if self._testMethodName == "test_two_links_two_turns":
            provider_args += ["--connections", "2"]
        if self._testMethodName == "test_inflight_cancel":
            provider_args += ["--delay-inference", "30"]
        if self._testMethodName == "test_codex_ws_cancel":
            provider_args += ["--delay-inference", "8"]
        port = None
        codex_port = None
        if not live:
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
        hosted_env.pop("CONCURRO_CLAUDE_MESSAGE_THREADS", None)
        if not self.options.claude_threads:
            hosted_env["CONCURRO_CLAUDE_MESSAGE_THREADS"] = "0"
        provider_override = (["--provider-ca", "/etc/ssl/certs/ca-certificates.crt"] if live else
                             ["--provider-host", "127.0.0.1", "--provider-port", str(port),
                              "--provider-ca", str(self.options.certs / "chain-ca.pem"),
                              "--codex-provider-host", "127.0.0.1", "--codex-provider-port", str(codex_port)])
        self.hosted = subprocess.Popen(
            [str(self.options.hosted_bin), "--certificate", str(self.options.certs / "verifier.pem"),
             "--private-key", str(self.options.certs / "verifier.key"),
             "--registry", str(self.options.output / "registry.json"),
             "--service-token", str(token_file),
             "--verification-url", "https://fixture.invalid/credentials/relay",
             "--relay-listen", "127.0.0.1:17443", "--private-listen", "127.0.0.1:17450",
             *provider_override],
            cwd=self.options.relay_root, env=hosted_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        self.processes.append(self.hosted)
        self.assertIn("listening", self.hosted.stdout.readline())
        self.relay_approve = relay_approver("http://127.0.0.1:17450", self.service_token)
        super().setUp()
        self.exchange.inference.upstreams["claude-subscription"] = "https://api.anthropic.com/v1"
        if live:
            self.exchange.inference.upstreams["chatgpt-subscription"] = UPSTREAMS["chatgpt-subscription"]
        self.exchange.inference.verifier_url = "http://127.0.0.1:17450"
        self.exchange.inference.verifier_token = self.service_token
        if sustained or live:
            self.start_runner_http()
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
        for account in self.task_keychain_accounts:
            subprocess.run(
                ["/usr/bin/security", "delete-generic-password", "-s", "concurro-relay",
                 "-a", account], capture_output=True, timeout=10,
            )

    def start_runner_http(self):
        """Expose the real Exchange API and a signed synthetic Actions OIDC endpoint."""
        exchange_http = exchange_server(self.exchange, "127.0.0.1:0")
        exchange_thread = threading.Thread(target=exchange_http.serve_forever, daemon=True)
        exchange_thread.start()
        self.context.callback(lambda: (exchange_http.shutdown(), exchange_http.server_close(),
                                       exchange_thread.join(5)))
        self.exchange_http = f"http://127.0.0.1:{exchange_http.server_port}"
        oidc_http = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticOidcHandler)
        oidc_http.requests = 0
        oidc_http.oidc_token = None
        oidc_thread = threading.Thread(target=oidc_http.serve_forever, daemon=True)
        oidc_thread.start()
        self.context.callback(lambda: (oidc_http.shutdown(), oidc_http.server_close(),
                                       oidc_thread.join(5)))
        self.oidc_http = oidc_http

    def claim_http(self, session, engine, run_id):
        """Fetch signed OIDC over HTTP and claim/receive this turn through the runner client."""
        self.oidc_http.oidc_token = self.signer.token(
            ref="refs/heads/concurro/runs/" + session, actor_id="3003", run_id=str(run_id))
        with patch.dict(os.environ, {
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{self.oidc_http.server_port}/token?fixture=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-actions-request",
        }):
            oidc = agent_runner.oidc_token()
        client = Runner(self.exchange_http, session)
        grant = client.key(oidc, engine)
        claimed = client.next(1, 0)
        self.runner_client = client
        return grant, claimed

    def run_native_entry(self, grant):
        """Invoke the production agent entry with pinned CLI and isolated fixture files."""
        live = self._testMethodName.endswith("_live")
        directory = self.root / "sustained-native"
        workspace = directory / "workspace"
        scratch = directory / "scratch"
        retained = directory / "retained"
        for path in (workspace, scratch, retained, directory / "home", directory / "tmp",
                     retained / "codex", retained / "claude", retained / "copilot"):
            path.mkdir(parents=True, exist_ok=True)
        names = [f"file-{index:02d}.txt" for index in range(1, self.options.steps + 1)]
        for index, name in enumerate(names, 1):
            (workspace / name).write_text(f"topic-{index:02d}\n")
        bundle = Bundle(target=ROOT / "examples/stacks-core")
        provider = grant["provider"]
        configured = {
            "provider": provider, "model": grant["model"],
            "effort": "low" if live or provider == "codex" else "medium",
            "stage": "plan", "timeout": 1800 if live else 900, "session": None,
            "workspace": str(workspace), "scratch": str(scratch), "retained_root": str(retained),
            "gateway": {"url": self.runtime.inference, "headers": {},
                        **({"codex_ws_url": self.codex_ws_bridge} if provider == "codex" else {})},
            "admission": {"route": grant["binding"]["route"], "custody": "relay"},
            "charter": bundle.charter_text, "instruction_texts": bundle.instruction_texts,
            "bundle_id": bundle.identity,
            "context": {"repository_commit": "c" * 40,
                        "repository": {"refs": {}, "head": "main"},
                        "task": ("Read file-01.txt through file-" + f"{self.options.steps:02d}"
                                 + ".txt in numeric order. Make one read-only tool call per file; "
                                 "do not batch files. Return the ordered filenames and put exactly "
                                 "one line per file in the structured output's steps array, "
                                 "in the form 'file-NN.txt: topic-NN'."
                                 if live else
                                 "Read every file in this directory and give one line per file.")},
        }
        (directory / "entry-request.json").write_text(json.dumps(configured, indent=2) + "\n")
        (directory / "cli-command.json").write_text(json.dumps(
            agent_entry.command_for({**configured, "schema": bundle.schema("plan")},
                                    str(scratch / "schema.json"), str(scratch / "result.json")), indent=2) + "\n")
        client = self.options.clients / provider
        version = subprocess.run([str(client), "--version"], capture_output=True, text=True,
                                 check=True, timeout=15).stdout.strip()
        self.assertIn(CLI_VERSIONS[provider], version)
        (directory / "cli-version.txt").write_text(version + "\n")
        environment = {
            "PATH": str(self.options.clients) + ":/opt/homebrew/bin:/usr/bin:/bin",
            "HOME": str(directory / "home"), "TMPDIR": str(directory / "tmp"),
            "CODEX_HOME": str(retained / "codex"), "CLAUDE_CONFIG_DIR": str(retained / "claude"),
            "COPILOT_HOME": str(retained / "copilot"),
            agent_entry.KEY_VARIABLE: grant["key"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        }
        command = [sys.executable, str(ROOT / "engine/agent_entry.py")]
        started = time.monotonic()
        process = subprocess.Popen(command, cwd=workspace, env=environment, text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        stdout = stderr = ""
        timed_out = False
        try:
            stdout, stderr = process.communicate(json.dumps(configured), timeout=1920 if live else 1020)
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            timed_out = True
            raise
        finally:
            processes.kill_group(process.pid)
            process.wait()
            (directory / "cli-elapsed.json").write_text(json.dumps({
                "seconds": time.monotonic() - started, "exit_code": process.returncode,
                "timed_out": timed_out,
            }, indent=2) + "\n")
            (directory / "stdout.log").write_text(stdout)
            (directory / "stderr.log").write_text(stderr)
        elapsed = time.monotonic() - started
        (directory / "entry-command.json").write_text(json.dumps(command) + "\n")
        self.assertEqual(process.returncode, 0, stderr[-1500:])
        answer = json.loads(stdout)
        summaries = [f"{name}: topic-{index:02d}" for index, name in enumerate(names, 1)]
        if not live:
            self.assertEqual(answer["files"], names)
            self.assertEqual(answer["summary"].splitlines(), summaries)
        return answer, elapsed

    def sustained(self, route):
        """Run one real CLI through HTTP claim, native relay, tools and exact usage joins."""
        self.approve_owner(routes=[route])
        grant = self.admit(route, "101", budget=1000, claim_session=self.claim_http,
                           engine=agent_runner.engine_commit())
        answer, elapsed = self.run_native_entry(grant)
        self.runner_client.complete(grant["turn"],
                                    {"outcome": "ok", "response": answer,
                                     "workspace": None, "retained": None})
        self.runner_client.end()
        (self.options.output / "provider.stop").touch()
        line = self.provider.stdout.readline()
        self.assertTrue(line, "synthetic provider ended without a request journal")
        provider = json.loads(line)
        self.assertNotIn("error", provider)
        rows = provider["bindings"] if route == "chatgpt-subscription" else provider["requests"]
        expected_requests = self.options.steps + (2 if route == "chatgpt-subscription" else 1)
        self.assertEqual(len(rows), expected_requests)
        if self.options.expire_once_at >= 0:
            self.assertEqual(len(provider.get("errors", [])), 1)
        self.assertEqual(sum(bool(row.get("validated_output_sha256") or
                                  row.get("validated_tool_output_sha256")) for row in rows), self.options.steps)
        self.assertEqual(provider.get("provider_connections", provider.get("connections")), 1)
        self.assertEqual(self.oidc_http.requests, 1)
        self.assertEqual(self.exchange.inference.leased, {})
        observations = self.exchange.inference.events(grant["binding"]["repository"],
                                                      grant["binding"]["issue"])
        self.assertEqual(len(observations), expected_requests)
        logs = self.runtime.docker("logs", self.runtime.name)
        gateway = [json.loads(line) for line in logs.stdout.splitlines() if line.startswith("{")]
        gateway = {row["concurro_request"]: row for row in gateway
                   if row.get("scope") == "request" and row.get("http.method") == "POST"
                   and row.get("concurro_request")}
        timings = {row["request"]: row for row in self.hop_timings}
        records = []
        for index, (row, observed) in enumerate(zip(rows, observations), 1):
            identifier = observed["request"]
            usage = row.get("provider_usage") or {"input_tokens": row["input_tokens"],
                                                  "output_tokens": row["output_tokens"]}
            self.assertEqual(observed["usage"], {key: usage[key] for key in ("input_tokens", "output_tokens")})
            logged = gateway[identifier]
            self.assertEqual((logged["gen_ai.usage.input_tokens"], logged["gen_ai.usage.output_tokens"]),
                             (usage["input_tokens"], usage["output_tokens"]))
            timing = timings[identifier]
            self.assertIsNotNone(timing["first_event_seconds"])
            records.append({
                "index": index, "request": identifier, "stream": row.get("stream"),
                "parent": row.get("parent"), "response_id": row.get("response_id"),
                "warmup": row.get("warmup", False), "body_bytes": row["body_bytes"],
                "input_types": row.get("input_types"), "provider_usage": usage,
                "gateway_usage": {"input_tokens": logged["gen_ai.usage.input_tokens"],
                                  "output_tokens": logged["gen_ai.usage.output_tokens"]},
                "exchange_usage": observed["usage"],
                "first_event_seconds": timing["first_event_seconds"],
                "private_completion_seconds": timing["completion_seconds"],
                "gateway_duration": logged["duration"],
                "validated_tool_output_sha256": row.get("validated_output_sha256") or
                                                 row.get("validated_tool_output_sha256"),
            })
        (self.root / "sustained-result.json").write_text(json.dumps({
            "route": route, "cli_elapsed_seconds": elapsed, "model_requests": len(rows),
            "tool_calls": self.options.steps, "oidc_http_requests": self.oidc_http.requests,
            "provider_connections": 1, "warmups": sum(row["warmup"] for row in records),
            "retries": 0, "records": records, "answer": answer,
            "released": not self.exchange.inference.leased,
        }, indent=2) + "\n")

    def test_claude_sustained(self):
        """Claude reads every synthetic file through real Bash tools on one H2 link."""
        self.sustained("claude-subscription")

    def test_codex_sustained(self):
        """Codex reads every synthetic file through real exec tools on one WS link."""
        self.sustained("chatgpt-subscription")

    def live(self, route, model):
        """Run one real subscription CLI task and retain nonsecret hop measurements."""
        self.approve_owner(routes=[route])
        grant = self.admit(route, "101", budget=1200000, claim_session=self.claim_http,
                           engine=agent_runner.engine_commit(), model=model)
        answer = elapsed = failure = None
        lifecycle_errors = []
        try:
            answer, elapsed = self.run_native_entry(grant)
        except BaseException as error:
            failure = error
        finally:
            try:
                self.runner_client.complete(grant["turn"],
                                            {"outcome": "ok" if failure is None else "failed",
                                             "response": answer, "error": None if failure is None
                                             else type(failure).__name__,
                                             "workspace": None, "retained": None})
            except BaseException as error:
                lifecycle_errors.append("complete: " + repr(error))
            try:
                self.runner_client.end()
            except BaseException as error:
                lifecycle_errors.append("end: " + repr(error))
        observations = self.exchange.inference.events(grant["binding"]["repository"],
                                                      grant["binding"]["issue"])
        logs = self.runtime.docker("logs", self.runtime.name)
        gateway = [json.loads(line) for line in logs.stdout.splitlines() if line.startswith("{")]
        gateway = {row["concurro_request"]: row for row in gateway
                   if row.get("scope") == "request" and row.get("http.method") == "POST"
                   and row.get("concurro_request")}
        timings = {row["request"]: row for row in self.hop_timings}
        records = []
        for index, observed in enumerate(observations, 1):
            identifier = observed["request"]
            logged = gateway.get(identifier, {})
            timing = timings.get(identifier, {})
            records.append({
                "index": index, "request": identifier, "model": model,
                "body_bytes": timing.get("body_bytes"), "input_types": timing.get("input_types", []),
                "tool_results": sum(kind in ("tool_result", "function_call_output")
                                    for kind in timing.get("input_types", [])),
                "tool_result_ids": timing.get("tool_result_ids", []),
                "exchange_usage": observed["usage"],
                "gateway_usage": {key.removeprefix("gen_ai.usage."): value
                                  for key, value in logged.items() if key.startswith("gen_ai.usage.")},
                "gateway_status": logged.get("http.status"),
                "first_event_seconds": timing.get("first_event_seconds"),
                "private_completion_seconds": timing.get("completion_seconds"),
                "gateway_duration": logged.get("duration"),
            })
        result = {
            "route": route, "model": model, "cli_elapsed_seconds": elapsed,
            "claude_threads": self.options.claude_threads,
            "model_requests": len(records),
            "tool_results": len({tool for row in records for tool in row["tool_result_ids"]}),
            "oidc_http_requests": self.oidc_http.requests,
            "records": records, "answer": answer,
            "released": not self.exchange.inference.leased,
            "failure": None if failure is None else type(failure).__name__,
            "lifecycle_errors": lifecycle_errors,
        }
        (self.root / "live-result.json").write_text(json.dumps(result, indent=2) + "\n")
        if failure is not None:
            raise failure
        self.assertEqual(answer["steps"], [f"file-{index:02d}.txt: topic-{index:02d}"
                                           for index in range(1, self.options.steps + 1)])
        self.assertFalse(lifecycle_errors)
        self.assertEqual(self.oidc_http.requests, 1)
        self.assertEqual(self.exchange.inference.leased, {})
        self.assertTrue(all(row["gateway_status"] == 200 for row in records))
        self.assertTrue(all(
            row["gateway_usage"]["output_tokens"] == row["exchange_usage"]["output_tokens"]
            and row["gateway_usage"]["input_tokens"] == row["exchange_usage"]["input_tokens"]
            + (row["gateway_usage"].get("cache_creation.input_tokens", 0)
               + row["gateway_usage"].get("cache_read.input_tokens", 0)
               if route == "claude-subscription" else 0)
            for row in records
        ))

    def test_claude_live(self):
        """Run the directory summary with the existing Claude subscription."""
        self.live("claude-subscription", "claude-sonnet-4-6")

    def test_codex_live(self):
        """Run the directory summary with the existing ChatGPT subscription."""
        self.live("chatgpt-subscription", "gpt-6.1-terra")

    def test_codex_upgrade_live(self):
        """Probe the real Codex upgrade with one non-generating Responses request."""
        self.approve_owner(routes=["chatgpt-subscription"])
        grant = self.admit("chatgpt-subscription", "101", budget=1200000,
                           claim_session=self.claim_http, engine=agent_runner.engine_commit(),
                           model="gpt-6.1-terra")
        status = body = failure = None
        lifecycle_errors = []
        try:
            status, body = self.gateway_call(grant, prompt=[], generate=False)
        except BaseException as error:
            failure = error
        finally:
            try:
                self.runner_client.complete(grant["turn"],
                                            {"outcome": "ok" if failure is None else "failed",
                                             "response": None, "error": None if failure is None
                                             else type(failure).__name__,
                                             "workspace": None, "retained": None})
            except BaseException as error:
                lifecycle_errors.append("complete: " + repr(error))
            try:
                self.runner_client.end()
            except BaseException as error:
                lifecycle_errors.append("end: " + repr(error))
        (self.root / "upgrade-probe.json").write_text(json.dumps({
            "status": status, "body_bytes": None if body is None else len(body),
            "completed_event": body is not None and b"response.completed" in body,
            "failure": None if failure is None else type(failure).__name__,
            "lifecycle_errors": lifecycle_errors,
        }, indent=2) + "\n")
        if failure is not None:
            raise failure
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", body)
        self.assertFalse(lifecycle_errors)

    def copy_existing_credential(self, route, state):
        """Copy a relay-owned Keychain credential to this task's disposable relay state."""
        source = self.options.credential_store.resolve()
        source_account = route.replace("-", "_") + ":" + str(source).encode().hex()
        target_account = route.replace("-", "_") + ":" + str(Path(state).resolve()).encode().hex()
        found = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "concurro-relay",
             "-a", source_account, "-w"], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(found.returncode, 0, "existing relay credential unavailable")
        kept = found.stdout.strip()
        self.assertTrue(kept, "existing relay credential is empty")
        command = ("add-generic-password -U -s concurro-relay -a \"" + target_account
                   + "\" -l \"Concurro relay task credential\" -w \"" + kept + "\"\n")
        seeded = subprocess.run(["/usr/bin/security", "-i"], input=command, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(seeded.returncode, 0, "task relay credential could not be kept")
        self.task_keychain_accounts.append(target_account)

    def approve_owner(self, links=1, routes=None, portal=False, mismatched=None):
        """Register frozen Sponsor, then approve its exact routes through Vault or the owner portal."""
        routes = routes or ["claude-subscription"]
        live = self._testMethodName.endswith("_live")
        sponsor_env = dict(os.environ)
        sponsor_env.update(
            CONCURRO_NATIVE_BUNDLE=str(self.options.sponsor_package / "native-runtime"),
            CONCURRO_NATIVE_OUTPUT=str(self.options.output / "sponsor-sessions"),
        )
        if live:
            sponsor_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            sponsor_env.pop("CONCURRO_NATIVE_PROVIDER_CA", None)
        else:
            sponsor_env["CLAUDE_CODE_OAUTH_TOKEN"] = "X" * 44
            sponsor_env["CONCURRO_NATIVE_PROVIDER_CA"] = str(self.options.certs / "chain-ca.pem")
        sponsor = str(self.options.sponsor_package / "bin/concurro-relay")
        state = str(self.options.output / "sponsor-state")
        if live:
            for route in routes:
                self.copy_existing_credential(route, state)
        elif "chatgpt-subscription" in routes:
            token = "SYNTHETIC_CODEX_" + "X" * (self.options.codex_token_length - len("SYNTHETIC_CODEX_"))
            credential = {"token": token, "headers": [["chatgpt-account-id", "synthetic-account-101"]]}
            kept = base64.b64encode(json.dumps(credential, separators=(",", ":")).encode()).decode()
            account = "chatgpt_subscription:" + str(Path(state).resolve()).encode().hex()
            command = ("add-generic-password -U -s concurro-relay -a \"" + account
                       + "\" -l \"Concurro relay: ChatGPT subscription credential\" -w \"" + kept + "\"\n")
            seeded = subprocess.run(["/usr/bin/security", "-i"], input=command, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            self.task_keychain_accounts.append(account)
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
        self.assertIs(ingress[0]["generate"], False)
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

    def gateway_call(self, grant, prompt="fixture", model=None, generate=None):
        """Send one provider request through the pinned gateway's public inference port."""
        parsed = urlsplit(self.runtime.inference)
        codex = grant["provider"] == "codex"
        request = ({"model": model or grant["model"], "input": prompt, "stream": True}
                   if codex else
                   {"model": model or grant["model"], "max_tokens": 32,
                    "messages": [{"role": "user", "content": prompt}], "stream": True})
        if codex and generate is not None:
            request["generate"] = generate
        body = json.dumps(request)
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
    parser.add_argument("--credential-store", type=Path,
                        default=Path.home() / "Library/Application Support/concurro-relay")
    parser.add_argument("--codex-provider-source", type=Path)
    parser.add_argument("--clients", type=Path)
    parser.add_argument("--responses-bridge-bin", type=Path)
    parser.add_argument("--steps", type=int,
                        help="Directory file count: defaults to 5 for live smoke tests, 25 otherwise")
    parser.add_argument("--claude-threads", action="store_true", default=True,
                        help="Use the default compact Claude Message Threads mode")
    parser.add_argument("--claude-full-context", dest="claude_threads", action="store_false",
                        help="Use full-context Claude requests for diagnostics")
    parser.add_argument("--public-circuit-cache", type=Path,
                        help="Reuse only public circuit text files from a completed local run")
    parser.add_argument("--capture-cli-requests", action="store_true",
                        help="Keep private full CLI JSON bodies for local diagnostic comparison")
    parser.add_argument("--expire-once-at", type=int, default=-1,
                        help="Synthetic Claude provider request index for one 404 replay")
    parser.add_argument("--codex-token-length", type=int, default=85)
    parser.add_argument("--native-timeout", type=int, default=40)
    parser.add_argument("--certs", type=Path, required=True)
    parser.add_argument("--case", choices=("test_owner_gateway_relay", "test_two_links_two_turns",
                                           "test_inflight_cancel", "test_codex_tool_loop",
                                           "test_mixed_routes", "test_codex_ws_denials",
                                           "test_codex_ws_cancel", "test_portal_claude_routes",
                                           "test_portal_chatgpt_routes", "test_portal_combined_routes",
                                           "test_claude_sustained", "test_codex_sustained",
                                           "test_claude_live", "test_codex_live",
                                           "test_codex_upgrade_live"), default="test_owner_gateway_relay")
    args = parser.parse_args()
    if args.steps is None:
        args.steps = 5 if args.case.endswith("_live") else 25
    if args.case in ("test_codex_tool_loop", "test_mixed_routes", "test_codex_ws_denials",
                     "test_codex_ws_cancel", "test_codex_sustained", "test_codex_live",
                     "test_codex_upgrade_live") and (not args.codex_provider_source or not args.clients
                                               or not args.responses_bridge_bin):
        parser.error("Codex cases require --codex-provider-source, --clients and --responses-bridge-bin")
    if args.case in ("test_claude_sustained", "test_claude_live") and not args.clients:
        parser.error("sustained Claude requires --clients")
    if args.steps < 1:
        parser.error("--steps must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    if args.public_circuit_cache:
        for role in ("concurro-sessions", "sponsor-sessions"):
            source = args.public_circuit_cache / role / "public-circuits"
            target = args.output / role / "public-circuits"
            target.mkdir(parents=True)
            for circuit in source.glob("*.txt"):
                shutil.copy2(circuit, target / circuit.name)
    HostedRelayTests.options = args
    result = unittest.TestResult()
    HostedRelayTests(args.case).run(result)
    for _, failure in result.failures + result.errors:
        print(failure, file=sys.stderr)
    print(json.dumps({"passed": result.wasSuccessful(), "output": str(args.output)}), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
