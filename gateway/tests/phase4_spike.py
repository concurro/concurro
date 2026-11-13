"""Opt-in production-config phase4 acceptance against the pinned gateway and fake upstreams.

Run with --output NEW_DIRECTORY --clients PINNED_NPM_BIN_DIRECTORY. No deployment
configuration, real provider credentials or external inference endpoints are read or used.
"""

import argparse
import gzip
import http.client
import json
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "gateway/exchange")]

# isort: split
from exchange_fixtures import AUDIENCE, BINDING, EXPECTED, Signer

import admissions
import sponsorship_spike as historical
from credentials import Credential
from exchange import BODY_BUDGET, MAX_BODY, Bounded, Budget, Exchange, Gateway, Keys, Refused
from inference import UPSTREAMS, Handler
from policy import digest
from spike_fixtures import (
    CLI_VERSIONS,
    IMAGE,
    DockerGateway,
    FakeHandler,
    FakeServices,
    fake_server,
    file_digest,
    request,
    scan_paths,
)
from test_exchange_funding import MetadataVault, funded_turn, publish

ROOT = Path(__file__).resolve().parents[2]


class Providers(FakeServices):
    """Reuse the phase0 fake wire protocol for both API-key and subscription-shaped routes."""

    def provider(self, handler, body):
        """Normalize fake API-key auth for the existing fake provider's credential checker."""
        if handler.headers.get("x-api-key") and not handler.headers.get("authorization"):
            handler.headers["authorization"] = "Bearer " + handler.headers["x-api-key"]
        token = handler.headers.get("authorization", "").removeprefix("Bearer ")
        record = next((item for item in self.pending.values() if item.get("token") == token), {})
        if record.get("mode") == "verified_rate":
            reply = handler.json_reply
            with patch.object(
                handler,
                "json_reply",
                side_effect=lambda *_args, **_kwargs: reply(
                    429,
                    {
                        "error": {
                            "code": "slow_down",
                            "type": "rate_limit_error",
                            "message": "FAKE_PROVIDER_DETAIL_SENTINEL " + token,
                        }
                    },
                    {"Retry-After": "2"},
                ),
            ):
                return super().provider(handler, body)
        if record.get("mode") in ("verified_stream", "verified_stream_auth"):
            write = handler.wfile.write

            def stream_error(raw):
                """Send a documented API code in a fake200 stream error, never a subscription taxonomy claim."""
                return (
                    write(
                        b"event: error\ndata: "
                        + json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "code": "slow_down",
                                    "type": "authentication_error"
                                    if record.get("mode") == "verified_stream_auth"
                                    else "rate_limit_error",
                                    "message": "FAKE_PROVIDER_DETAIL_SENTINEL " + token,
                                },
                            }
                        ).encode()
                        + b"\n\n"
                    )
                    if raw.startswith(b"event:")
                    else write(raw)
                )

            with patch.object(handler.wfile, "write", side_effect=stream_error):
                return super().provider(handler, body)
        if record.get("mode") == "count_probe":
            reply = handler.json_reply
            with patch.object(
                handler, "json_reply", side_effect=lambda *_args, **_kwargs: reply(200, {"input_tokens": 2})
            ):
                return super().provider(handler, body)
        if record.get("mode") == "headers_delay":
            reply = handler.json_reply

            def delayed(*args, **kwargs):
                """Leave the upstream connected silently until the test releases it."""
                self.blocked.set()
                self.release.wait(90)
                try:
                    return reply(*args, **kwargs)
                except (OSError, http.client.HTTPException):
                    return None

            with patch.object(handler, "json_reply", side_effect=delayed):
                return super().provider(handler, body)
        if record.get("mode") == "scalar_error":
            reply = handler.json_reply
            with patch.object(
                handler,
                "json_reply",
                side_effect=lambda *_args, **_kwargs: reply(200, {"error": "FAKE_PROVIDER_DETAIL_SENTINEL " + token}),
            ):
                return super().provider(handler, body)
        if record.get("mode") == "error_event":
            write = handler.wfile.write

            def event(raw):
                """Keep real HTTP framing but emit an SSE error label without a matching data type."""
                return (
                    write(
                        b"event: error\ndata: "
                        + json.dumps({"message": "FAKE_PROVIDER_DETAIL_SENTINEL " + token}).encode()
                        + b"\n\n"
                    )
                    if raw.startswith(b"event:")
                    else write(raw)
                )

            with patch.object(handler.wfile, "write", side_effect=event):
                return super().provider(handler, body)
        if record.get("mode") == "gzip_capable" and "gzip" in handler.headers.get("Accept-Encoding", ""):

            def compressed(status, value, headers=None):
                """Negotiate actual compressed bytes only when the caller advertises support."""
                raw = gzip.compress(json.dumps(value).encode())
                handler.send_response(status)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Encoding", "gzip")
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)

            with patch.object(handler, "json_reply", side_effect=compressed):
                return super().provider(handler, body)
        if record.get("mode") == "budget_spoof":
            reply = handler.json_reply
            with patch.object(
                handler,
                "json_reply",
                side_effect=lambda *_args, **_kwargs: reply(
                    429,
                    {"error": {"code": "budget_exceeded", "type": "rate_limit_error", "message": "Budget exceeded"}},
                ),
            ):
                return super().provider(handler, body)
        return super().provider(handler, body)


class CapturingHandler(Handler):
    """Retain only nonsecret hop header names, intended-auth booleans and beta values."""

    def forward(self, snapshot, url, body):
        """Observe native metadata composition without writing any provider credential bytes."""
        material = self.server.inference.exchange.vault.material[snapshot["binding"]["credential"]]
        self.server.hop_headers.append(
            {
                "request": snapshot["request"],
                "names": sorted(name.lower() for name in self.headers),
                "secret_header_present": self.headers.get("x-concurro-token") is not None,
                "auth_matches": self.headers.get("authorization") == "Bearer " + material["secret"]
                or self.headers.get("x-api-key") == material["secret"],
                "anthropic_beta": self.headers.get_all("anthropic-beta"),
            }
        )
        return super().forward(snapshot, url, body)


class Secrets(MetadataVault):
    """Synthetic current metadata, exact secrets and terminal resolution failures."""

    def __init__(self):
        """Keep an independent fake material record for every admitted account/route/version."""
        super().__init__()
        self.records = []
        self.material = {}
        self.calls = []
        self.denied = {}

    def resolve(self, binding):
        """Record exact resolution identity without including material in the evidence journal."""
        self.calls.append({k: binding[k] for k in ("sponsor", "route", "credential", "credential_version")})
        if binding["credential"] in self.denied:
            raise Refused(self.denied[binding["credential"]], "synthetic credential denial")
        return dict(self.material[binding["credential"]])


class RuntimeTests(unittest.TestCase):
    """Exercise the actual production hooks/config with native model/key/budget enforcement."""

    options = None
    native = historical.SpikeTests.native

    def setUp(self):
        """Start only loopback fakes, a private Exchange listener and one disposable gateway."""
        self.root = self.options.output / self._testMethodName
        self.root.mkdir()
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.state = Providers()
        provider_port = self.context.enter_context(fake_server(self.state))
        self.vault = Secrets()
        self.signer = Signer(self.root)
        self.exchange = Exchange(
            str(self.root / "exchange.db"),
            None,
            Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
            AUDIENCE,
            vault=self.vault,
        )
        self.addCleanup(self.exchange.db.close)
        private = Bounded(("127.0.0.1", 0), CapturingHandler)
        private.hop_headers = []
        self.hop_headers = private.hop_headers
        private.inference, private.max_body, private.body_budget = (
            self.exchange.inference,
            MAX_BODY,
            Budget(BODY_BUDGET),
        )
        thread = threading.Thread(target=private.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (private.shutdown(), private.server_close(), thread.join(5)))
        for route in self.exchange.inference.upstreams:
            self.exchange.inference.upstreams[route] = (
                f"http://127.0.0.1:{provider_port}/"
                + (
                    "codex"
                    if route == "chatgpt-subscription"
                    else "openai"
                    if route == "openai-api-key"
                    else "anthropic"
                )
                + urlsplit(UPSTREAMS[route]).path
            )
        runtime = DockerGateway(self.root, provider_port, observer=True)
        config = yaml.safe_load((ROOT / "gateway/agentgateway.yaml").read_text())
        config["config"]["adminAddr"] = "0.0.0.0:15000"
        config["config"]["database"]["url"] = "sqlite:///tmp/gateway.db"
        config["config"]["logging"]["database"]["url"] = "sqlite:///tmp/requests.db"
        # Change only local transport and poison org backends; routing/policies remain production bytes.
        for model in config["llm"]["models"]:
            if not model.get("id", "").startswith("contributed-"):
                model["params"] = {"baseUrl": f"http://host.docker.internal:{provider_port}/operator"}
                model["auth"] = {"key": "synthetic-operator-poison"}
        raw = (
            json.dumps(config)
            .replace("127.0.0.1:8081", f"host.docker.internal:{private.server_port}")
            .replace("$CHATGPT_ACCOUNT_ID", "synthetic-operator-poison")
        )
        if self._testMethodName == "test_live_readonly_native_wal_under_gateway_uid":
            self.native_volume = runtime.name + "-native"
            runtime.docker(
                "volume",
                "create",
                "--driver",
                "local",
                "--opt",
                "type=tmpfs",
                "--opt",
                "device=tmpfs",
                "--opt",
                "o=size=64m,uid=65532,gid=65532",
                self.native_volume,
            )
            python_image = (
                "python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d"
            )
            runtime.docker(
                "run",
                "--rm",
                "--user",
                "0:0",
                "--volume",
                self.native_volume + ":/native",
                python_image,
                "python",
                "-c",
                "import os;os.chown('/native',65532,65532)",
            )
            self.context.callback(runtime.docker, "volume", "rm", self.native_volume, check=False)
            original = runtime.docker

            def docker(*args, **kwargs):
                """Add only this disposable native log volume to the gateway process."""
                if args and args[0] == "run" and IMAGE in args:
                    index = args.index(IMAGE)
                    args = (*args[:index], "--volume", self.native_volume + ":/native", *args[index:])
                    args = tuple(value for value in args if value != "--rm")
                return original(*args, **kwargs)

            runtime.docker = docker
            raw = raw.replace("sqlite:///tmp/requests.db", "sqlite:///native/requests.db")

            def copy_native():
                """Retain the actual live WAL files for payload/secret scans after the mount test."""
                copied = []
                for suffix in ("", "-wal", "-shm"):
                    target = self.root / ("requests.db" + suffix)
                    result = runtime.docker(
                        "cp", runtime.name + ":/native/requests.db" + suffix, str(target), check=False
                    )
                    if result.returncode == 0:
                        copied.append(target)
                return copied

            runtime.request_database = copy_native
        runtime.config = json.loads(raw)
        runtime.config_path.write_text(raw)
        self.runtime = self.context.enter_context(runtime)
        self.exchange.gateway = Gateway(runtime.admin, runtime.inference, contributed=True)
        self.sessions = []
        self.addCleanup(self.persist)

    def issue(self, route="openai-api-key", actor="101", budget=1000, mode="ok", model=None):
        """Use genuine admission/fence/mint/claim/next paths for one explicitly sponsored route."""
        provider = "openai" if route.startswith(("openai", "chatgpt")) else "anthropic"
        previous = self.exchange.db.execute("SELECT epoch FROM sponsorship WHERE provider=?", (provider,)).fetchone()
        turn, binding, payload = funded_turn(provider, actor=actor, epoch=previous[0] + 1 if previous else 1)
        model = model or ("gpt-test" if provider == "openai" else "claude-test")
        record = digest([actor, route])
        credential = Credential(
            digest(["contributed", actor, route, record, 1]),
            "codex" if provider == "openai" else "claude",
            "contributed",
            "gateway",
            actor,
            route,
            record,
            1,
        )
        slot = payload["request"]["admission"]["grant"]
        admission = admissions.freeze(admissions.selection(credential, provider, slot), 1, 1)
        binding.update(route=route, credential=record)
        payload["request"].update(model=model, admission=admission)
        self.vault.records.append(
            {
                "user_id": actor,
                "route": route,
                "record_id": record,
                "version": 1,
                "kind": "subscription" if "subscription" in route else "api_key",
                "status": "valid",
                "models": [model],
                "expiry_known": False,
                "expires_at": None,
            }
        )
        secret = "SYNTHETIC_PROVIDER_SECRET_" + digest([actor, route])
        account = "synthetic-account-" + actor if route == "chatgpt-subscription" else None
        self.vault.material[record] = {
            "user_id": actor,
            "route": route,
            "record_id": record,
            "version": 1,
            "secret": secret,
            "account": account,
        }
        fake_route = (
            "chatgpt" if route == "chatgpt-subscription" else "openai-api" if route == "openai-api-key" else "claude"
        )
        self.state.credentials[secret] = {"route": fake_route, "account": account}
        self.state.pending[record] = {"token": secret, "account": account, "route": fake_route, "mode": mode}
        if previous:
            publish(self.exchange, binding, slot)
        reply = self.exchange.submit(
            turn, {**EXPECTED, "actor_id": "3003"}, [model], budget, 3600, payload, binding=binding
        )
        session = reply["dispatch"]
        grant = self.exchange.key(
            session,
            self.signer.token(
                ref="refs/heads/concurro/runs/" + session, actor_id="3003", run_id=str(len(self.sessions) + 77)
            ),
            engine=BINDING["engine"],
        )
        self.exchange.next(session, grant["token"], 1, 0)
        result = {
            **grant,
            "session": session,
            "turn": turn,
            "model": model,
            "provider": "codex" if provider == "openai" else "claude",
            "binding": binding,
            "record": record,
        }
        self.sessions.append(result)
        return result

    def call(self, grant, stream=False, headers=()):
        """Preserve repeated/mixed-case caller headers through the actual native inference path."""
        path = "/v1/responses" if grant["provider"] == "codex" else "/v1/messages"
        body = (
            {"model": grant["model"], "stream": stream, "input": "fixture"}
            if grant["provider"] == "codex"
            else {
                "model": grant["model"],
                "stream": stream,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "fixture"}],
            }
        )
        return request(
            self.runtime.inference + path, "POST", body, [("Authorization", "Bearer " + grant["key"]), *headers]
        )

    def persist(self):
        """Retain raw sanitized journals/logs and prove scanners catch a synthetic negative control."""
        logs = self.runtime.docker("logs", self.runtime.name, check=False)
        (self.root / "gateway.log").write_text(logs.stdout)
        (self.root / "gateway.stderr.log").write_text(logs.stderr)
        databases = self.runtime.request_database()
        events = [
            json.loads(row[0])
            for row in self.exchange.db.execute("SELECT body FROM inference_observations ORDER BY sequence")
        ]
        trace = {
            "image": {k: self.runtime.identity[k] for k in ("Id", "RepoDigests", "Architecture", "Os")},
            "provider_requests": self.state.calls,
            "resolutions": self.vault.calls,
            "hop_headers": self.hop_headers,
            "observations": events,
            "snapshots": self.exchange.db.execute(
                "SELECT id,session,turn,binding,resolved,claimed FROM inference_requests"
            ).fetchall(),
        }
        (self.root / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        needles = (
            [b"synthetic-operator-poison"]
            + [m["secret"].encode() for m in self.vault.material.values()]
            + [s["key"].encode() for s in self.sessions]
        )
        paths = [
            p
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.startswith("exchange.db") and p.name not in ("key.pem", "gateway.json")
        ]
        self.assertEqual(scan_paths(paths, needles), [])
        control = self.root / "negative-control.txt"
        control.write_bytes(needles[0] if needles else b"synthetic-marker")
        self.assertEqual(scan_paths([control], needles or [b"synthetic-marker"]), [control])
        control.unlink()
        if databases:
            with closing(sqlite3.connect(databases[0])) as db:
                self.assertGreater(db.execute("SELECT count(*) FROM request_logs").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM request_logs WHERE has_payload<>0").fetchone()[0], 0)
        self.assertEqual(
            sum(c["route"] == "operator" for c in self.state.calls),
            1 if self._testMethodName == "test_org_native_routing_and_budget_are_unchanged" else 0,
        )

    def test_four_routes_parallel_spoof_and_per_request_resolution(self):
        """Two owners and all four explicit adapters cannot borrow caller headers or org material."""
        grants = [
            self.issue(route, actor=str(101 + i), model="o3" if route == "openai-api-key" else None)
            for i, route in enumerate(
                ("openai-api-key", "chatgpt-subscription", "anthropic-api-key", "claude-subscription")
            )
        ]
        spoof = [
            ("X-Concurro-Route", "claude-subscription"),
            ("x-concurro-route", "openai-api-key"),
            ("ChatGPT-Account-ID", "wrong"),
            ("chatgpt-account-id", "wrong2"),
            ("X-Concurro-Request", "forged"),
            ("x-concurro-sponsor", "999"),
            ("x-internal-auth", "forged"),
            ("Anthropic-Beta", "synthetic-alpha"),
            ("anthropic-beta", "synthetic-beta"),
        ]
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda grant: self.call(grant, True, spoof), grants))
        self.assertTrue(all(result[0] == 200 for result in results), results)
        self.assertEqual(len(self.state.calls), 4)
        self.assertTrue(all(call["auth_matches"] for call in self.state.calls))
        for call in self.state.calls:
            path = urlsplit(call["path"]).path
            expected = (
                "/codex/backend-api/codex/responses"
                if call["route"] == "chatgpt"
                else "/openai/v1/responses"
                if call["route"] == "openai-api"
                else "/anthropic/v1/messages"
            )
            self.assertEqual(path, expected)
        for call in self.state.calls:
            self.assertFalse(any(k.startswith(("x-concurro-", "x-internal-")) for k in call["header_values"]))
        self.call(grants[0])
        self.assertTrue(
            all(not header["secret_header_present"] and header["auth_matches"] for header in self.hop_headers)
        )
        for call in self.state.calls:
            if call["route"] == "claude":
                self.assertIn("synthetic-alpha", call["headers"]["anthropic-beta"])
                self.assertIn("synthetic-beta", call["headers"]["anthropic-beta"])
        self.assertEqual(len(self.vault.calls), 5)
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM inference_requests").fetchone()[0], 5)

    def test_native_budget_snapshot_probe_and_reclaim(self):
        """Native denial joins the exact pre-auth request even after completion changes the active turn."""
        grant = self.issue(budget=4)
        self.assertEqual(self.vault.calls, [])
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM inference_requests").fetchone()[0], 0)
        self.assertEqual(self.call(grant)[0], 200)
        self.assertEqual(self.call(grant)[0], 429)
        frozen = self.exchange.db.execute("SELECT id,turn FROM inference_requests WHERE resolved=0").fetchone()
        self.exchange.complete(grant["session"], grant["token"], grant["turn"], {"outcome": "ok"})
        time.sleep(0.5)
        self.runtime.request_database()
        self.exchange.inference.native_database = self.root / "requests.db"
        self.exchange.inference.import_native()
        self.exchange.inference.import_native()
        failure = self.exchange.inference.failure(grant["turn"])
        self.assertEqual(
            (failure["request"], failure["turn"], failure["category"]), (frozen[0], frozen[1], "budget_denied")
        )
        self.assertIsNone(failure["usage"])
        self.assertEqual(len(self.state.calls), 1)
        self.assertEqual(len(self.vault.calls), 1)
        # A model-list activation after exhaustion remains non-turn and does not refill the budget.
        self.exchange.gateway.activated(grant["key"])
        self.assertEqual(len(self.state.calls), 1)

    def test_verified_api_denials_and_count_tokens_paths(self):
        """Real adapter bytes preserve exact API paths and trusted codes without a second paid request."""
        for index, (mode, expected) in enumerate(
            (("auth", "auth"), ("quota", "quota_exhausted"), ("verified_rate", "rate_limited"))
        ):
            grant = self.issue(actor=str(101 + index), mode=mode)
            before = len(self.state.calls)
            result = self.call(grant)
            self.assertIn(result[0], (401, 429))
            failure = self.exchange.inference.failure(grant["turn"])
            self.assertEqual(failure["category"], expected)
            self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", result[2])
            self.assertGreaterEqual(self.call(grant)[0], 400)
            self.assertEqual(len(self.state.calls), before + 1)
            if expected == "rate_limited":
                self.assertEqual(failure["retry_after"], 2)
        for route in ("anthropic-api-key", "claude-subscription"):
            grant = self.issue(route, actor=str(201 + len(self.sessions)), mode="count_probe")
            value = request(
                self.runtime.inference + "/v1/messages/count_tokens",
                "POST",
                {"model": grant["model"], "messages": [{"role": "user", "content": "fixture"}]},
                [("Authorization", "Bearer " + grant["key"])],
            )
            self.assertEqual(value[0], 200)
            self.assertEqual(json.loads(value[2])["input_tokens"], 2)
            self.assertEqual(urlsplit(self.state.calls[-1]["path"]).path, "/anthropic/v1/messages/count_tokens")

    def test_pinned_codex_handles_sanitized_api_stream_failure(self):
        """A pinned Responses client receives the sanitized error and cannot retry another upstream call."""
        grant = self.issue(mode="verified_stream")
        with self.assertRaises(AssertionError):
            self.native(grant, self.root / "native-stream-error", "Return a structured answer.")
        self.assertEqual(len(self.state.calls), 1)
        self.assertEqual(self.exchange.inference.failure(grant["turn"])["category"], "rate_limited")
        for path in (self.root / "native-stream-error").glob("*.log"):
            self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", path.read_text())

    def test_pinned_claude_handles_sanitized_stream_failure(self):
        """Pinned Claude accepts sanitized API authentication errors without raw detail or paid retry."""
        grant = self.issue("anthropic-api-key", mode="verified_stream_auth")
        with self.assertRaises(AssertionError):
            self.native(grant, self.root / "native-stream-error", "Return a structured answer.")
        self.assertEqual(len(self.state.calls), 1)
        self.assertEqual(self.exchange.inference.failure(grant["turn"])["category"], "auth")
        for path in (self.root / "native-stream-error").glob("*.log"):
            self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", path.read_text())

    def test_tls_delay_over_sixty_seconds_and_turn_cancel(self):
        """The actual image survives a >60s TLS response delay; turn cancellation interrupts SSL reads."""
        key, cert = self.root / "tls-key.pem", self.root / "tls-cert.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        server.state = self.state
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        for route, url in self.exchange.inference.upstreams.items():
            self.exchange.inference.upstreams[route] = (
                "https://127.0.0.1:" + str(server.server_port) + urlsplit(url).path
            )
        trusted = ssl.create_default_context(cafile=str(cert))
        grant = self.issue(mode="headers_delay")
        address = urlsplit(self.runtime.inference)
        client = http.client.HTTPConnection(address.hostname, address.port, timeout=80)
        with patch("inference.http.client._create_https_context", return_value=trusted), ThreadPoolExecutor(1) as pool:
            started = time.monotonic()
            client.request(
                "POST",
                "/v1/responses",
                json.dumps({"model": grant["model"], "input": "fixture"}),
                {"Authorization": "Bearer " + grant["key"], "Content-Type": "application/json"},
            )
            response = pool.submit(client.getresponse)
            self.assertTrue(self.state.blocked.wait(3))
            time.sleep(61)
            self.assertFalse(response.done(), "pinned gateway ended admitted request before the >60s evidence boundary")
            self.state.release.set()
            received = response.result(5)
            self.assertEqual(received.status, 200)
            received.read()
            elapsed = time.monotonic() - started
            client.close()
            self.state.blocked.clear()
            self.state.release.clear()
            pending = pool.submit(self.call, grant)
            self.assertTrue(self.state.blocked.wait(3))
            self.exchange.cancel(grant["turn"])
            self.assertGreaterEqual(pending.result(3)[0], 400)
            self.assertEqual(self.exchange.inference.connections, {})
            self.state.release.set()
        self.assertFalse(self.exchange.observations(BINDING["repository"], 7)["pending"])
        (self.root / "tls-delay.json").write_text(
            json.dumps({"verified_local_tls": True, "elapsed_seconds": elapsed, "cancel_closed_ssl_socket": True})
        )

    def test_silent_upstream_uses_admitted_lifetime_and_retires_promptly(self):
        """An admitted long request has no new60s read limit; stop interrupts a silent socket."""
        grant = self.issue(mode="headers_delay")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, grant)
            self.assertTrue(self.state.blocked.wait(3))
            sockets = list(self.exchange.inference.connections.values())
            self.assertEqual(len(sockets), 1)
            self.assertGreater(sockets[0][2].gettimeout(), 60)
            self.exchange.end(grant["session"])
            self.assertGreaterEqual(pending.result(3)[0], 400)
            self.assertEqual(self.exchange.inference.connections, {})
            self.state.release.set()

    def test_live_readonly_native_wal_under_gateway_uid(self):
        """Uid65532 imports exact native rows from the live read-only WAL volume using production code."""
        grant = self.issue(budget=4)
        self.assertEqual(self.call(grant)[0], 200)
        self.assertEqual(self.call(grant)[0], 429)
        snapshots = [
            {"id": row[0], "session": row[1], "turn": row[2], "binding": row[3], "created": row[4]}
            for row in self.exchange.db.execute("SELECT id,session,turn,binding,created FROM inference_requests")
        ]
        (self.root / "wal-snapshots.json").write_text(json.dumps(snapshots))
        code = """import os,json,runpy,sqlite3,time
Exchange=runpy.run_path('/source/gateway/exchange/exchange.py')['Exchange']
ex=Exchange('/tmp/exchange.db',None,None,'fixture')
for row in json.load(open('/fixture/wal-snapshots.json')):
 ex.db.execute('INSERT INTO inference_requests(id,session,turn,binding,created) VALUES(?,?,?,?,?)',tuple(row.values()))
ex.inference.native_database='/native/requests.db'
for attempt in range(30):
 ex.inference.import_native()
 events=ex.inference.events('upstream/project',7)
 if any(event['category']=='budget_denied' for event in events):break
 time.sleep(.1)
with sqlite3.connect('file:/native/requests.db?mode=ro',uri=True) as db:
 schema=db.execute('pragma table_info(request_logs)').fetchall()
 rows=db.execute('select count(*),sum(has_payload) from request_logs').fetchone()
try:
 open('/native/write-probe','w').close();readonly=False
except OSError:readonly=True
print(json.dumps({'uid':os.getuid(),'readonly':readonly,'journal':os.path.exists('/native/requests.db-wal'),'events':events,'schema':schema,'rows':rows}))
assert os.getuid()==65532 and readonly and rows[0]>0 and rows[1]==0
assert any(event['category']=='budget_denied' for event in events)
ex.db.close()
"""
        result = self.runtime.docker(
            "run",
            "--rm",
            "--user",
            "65532:65532",
            "--network",
            "none",
            "--volume",
            self.native_volume + ":/native:ro",
            "--volume",
            str(ROOT) + ":/source:ro",
            "--volume",
            str(self.root) + ":/fixture:ro",
            "python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d",
            "python",
            "-I",
            "-c",
            code,
        )
        (self.root / "live-wal.json").write_text(result.stdout)
        volume = json.loads(self.runtime.docker("volume", "inspect", self.native_volume).stdout)[0]
        (self.root / "live-volume.json").write_text(
            json.dumps(
                {"driver": volume["Driver"], "options": volume["Options"], "persistent_durability_tested": False},
                indent=2,
            )
        )
        observed = json.loads(result.stdout)
        self.assertEqual(observed["uid"], 65532)
        self.assertTrue(observed["readonly"])
        self.assertTrue(observed["journal"])

    def test_provider_api_denials_stop_native_cli_without_paid_retry(self):
        """The pinned CLI sees sanitized auth failure; repeated local retries cannot re-enter upstream."""
        grant = self.issue(mode="auth")
        with self.assertRaises(AssertionError):
            self.native(grant, self.root / "native-auth", "Return a structured answer.")
        self.assertEqual(len(self.state.calls), 1)
        failure = self.exchange.inference.failure(grant["turn"])
        self.assertEqual(failure["category"], "auth")
        self.assertFalse(
            any("FAKE_PROVIDER_DETAIL_SENTINEL" in p.read_text() for p in (self.root / "native-auth").glob("*.log"))
        )

    def test_org_native_routing_and_budget_are_unchanged(self):
        """Existing organization keys keep their routes/accounting and native token-budget behavior."""
        self.state.credentials["synthetic-operator-poison"] = {"route": "operator", "account": None}
        session = self.exchange.issue(EXPECTED, ["gpt-test"], 4, 3600, binding=BINDING)
        grant = self.exchange.key(
            session, self.signer.token(ref="refs/heads/concurro/runs/" + session), engine=BINDING["engine"]
        )
        value = {**grant, "provider": "codex", "model": "gpt-test", "session": session}
        self.sessions.append(value)
        self.assertEqual(self.call(value)[0], 200)
        self.assertEqual(self.call(value)[0], 429)
        self.assertEqual(self.vault.calls, [])
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM inference_requests").fetchone()[0], 0)
        self.assertEqual(self.state.calls[0]["route"], "operator")
        self.assertFalse(any(name.startswith("x-concurro-") for name in self.state.calls[0]["headers"]))

    def test_encoding_negotiation_and_structural_error_redaction(self):
        """Compressed-capable upstreams receive identity; scalar and SSE-label errors stay sanitized unknown."""
        grant = self.issue(mode="gzip_capable")
        self.assertEqual(self.call(grant, headers=[("Accept-Encoding", "gzip, br")])[0], 200)
        self.assertEqual(self.state.calls[-1]["headers"]["accept-encoding"], "identity")
        for mode in ("scalar_error", "error_event"):
            self.state.pending[grant["record"]]["mode"] = mode
            result = self.call(grant, stream=mode == "error_event")
            self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", result[2])
            self.assertNotIn(self.vault.material[grant["record"]]["secret"], result[2])
        events = self.exchange.inference.events(grant["binding"]["repository"], 7)
        self.assertEqual([event["category"] for event in events], [None, "unknown", "unknown"])
        self.assertTrue(events[0]["completed"])
        self.assertTrue(all(event["source"] == "provider" for event in events))

    def test_upstream_cannot_forge_native_budget_evidence(self):
        """A provider429 with the native-looking body is unknown, never a trusted native budget denial."""
        grant = self.issue(mode="budget_spoof")
        self.assertEqual(self.call(grant)[0], 429)
        time.sleep(0.5)
        self.runtime.request_database()
        self.exchange.inference.native_database = self.root / "requests.db"
        self.exchange.inference.import_native()
        self.assertIsNone(self.exchange.inference.failure(grant["turn"]))
        events = self.exchange.inference.events(grant["binding"]["repository"], 7)
        self.assertFalse(any(e["category"] == "budget_denied" for e in events))
        self.assertTrue(any(e["category"] == "unknown" for e in events))

    def test_denial_outage_revoke_and_no_operator_fallback(self):
        """Exact-version denial/outage stops upstream; revoking one owner leaves the peer usable."""
        first = self.issue()
        other = self.issue("anthropic-api-key", actor="202")
        for status in (503, 410):
            self.vault.denied[first["record"]] = status
            self.assertGreaterEqual(self.call(first)[0], 400)
        self.assertEqual(len(self.state.calls), 0)
        self.assertEqual(self.call(other)[0], 200)
        self.assertEqual(self.exchange.inference.failure(first["turn"])["category"], "auth")
        publish(
            self.exchange,
            first["binding"],
            {"epoch": 2, "status": "withdrawn", "sponsor_id": None, "grant_id": "f" * 64},
            reason="withdraw",
            actor="101",
            cutoff=1,
        )
        self.assertGreaterEqual(self.call(first)[0], 400)
        self.assertEqual(self.call(other)[0], 200)

    def test_stream_delay_cancellation_and_unknown_errors(self):
        """Response streaming precedes completion, retains frozen identity and stops early on cancellation."""
        grant = self.issue("chatgpt-subscription", mode="delay")
        future = ThreadPoolExecutor(1)
        self.addCleanup(future.shutdown)
        pending = future.submit(self.call, grant, True)
        self.assertTrue(self.state.blocked.wait(5))
        self.assertFalse(pending.done())
        self.state.release.set()
        self.assertEqual(pending.result(10)[0], 200)
        event = self.exchange.inference.events(grant["binding"]["repository"], 7)[0]
        self.assertEqual((event["turn"], event["binding"]), (grant["turn"], grant["binding"]))
        self.state.pending[grant["record"]]["mode"] = "flood"
        connection = http.client.HTTPConnection("127.0.0.1", int(self.runtime.inference.rsplit(":", 1)[1]), timeout=10)
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps({"model": grant["model"], "input": "fixture", "stream": True}),
            {"Content-Type": "application/json", "Authorization": "Bearer " + grant["key"]},
        )
        response = connection.getresponse()
        self.assertTrue(response.readline())
        connection.close()
        self.assertTrue(self.state.flood_stopped.wait(10))
        self.assertLess(self.state.flood_written, 2048)
        self.state.pending[grant["record"]]["mode"] = "stream_error"
        result = self.call(grant, True)
        self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", result[2])
        self.assertNotIn(self.vault.material[grant["record"]]["secret"], result[2])
        self.assertIsNone(self.exchange.inference.failure(grant["turn"]))

    def test_pinned_native_clients(self):
        """Both pinned native CLIs use real command builders through contributed production routing."""
        for route in ("openai-api-key", "chatgpt-subscription", "anthropic-api-key", "claude-subscription"):
            grant = self.issue(route, actor=str(101 + len(self.sessions)))
            binary = self.options.clients / grant["provider"]
            version = historical.subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
            self.assertIn(CLI_VERSIONS[grant["provider"]], version.stdout)
            thread, answer = self.native(
                grant, self.root / route, "Remember MEMORY_ALICE. Return it in the required answer schema."
            )
            self.assertEqual(answer["answer"], "MEMORY_ALICE")
            self.assertTrue(thread)
            if "subscription" in route:
                second = self.issue(route, actor=str(201 + len(self.sessions)))
                archive = historical.turns.pack((self.root / route) / "retained")
                resumed, answer = self.native(
                    second,
                    self.root / (route + "-resume"),
                    "Return the earlier memory marker in the required answer schema.",
                    resume=thread,
                    archive=archive,
                )
                self.assertEqual(resumed, thread)
                self.assertEqual(answer["answer"], "MEMORY_ALICE")


def main():
    """Refuse existing output roots and retain identities/results even on a failed experiment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clients", type=Path, required=True)
    parser.add_argument("--case", action="append", help="Run a named case; omitted runs the full matrix")
    options = parser.parse_args()
    options.output.mkdir(exist_ok=False)
    source_paths = [
        "gateway/exchange/exchange.py",
        "gateway/exchange/inference.py",
        "gateway/provider_protocol.py",
        "gateway/agentgateway.yaml",
        "gateway/tests/phase4_spike.py",
        "gateway/tests/spike_fixtures.py",
        "gateway/tests/spike_observer.py",
        "gateway/tests/exchange_fixtures.py",
        "gateway/tests/test_exchange_funding.py",
        "engine/credential_routes.py",
        "engine/agent_entry.py",
        "engine/agent-clients.txt",
        "engine/turns.py",
        "engine/processes.py",
    ]
    source_hashes = {name: file_digest(ROOT / name) for name in source_paths}
    (options.output / "sources-at-launch.json").write_text(json.dumps(source_hashes, indent=2) + "\n")
    RuntimeTests.options = options
    suite = (
        unittest.TestSuite(RuntimeTests(name) for name in options.case)
        if options.case
        else unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeTests)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    manifest = {
        "base": historical.subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "image": IMAGE,
        "clis": CLI_VERSIONS,
        "production_config": file_digest(ROOT / "gateway/agentgateway.yaml"),
        "sources": source_hashes,
        "sources_unchanged": all(file_digest(ROOT / name) == value for name, value in source_hashes.items()),
        "success": result.wasSuccessful(),
        "tests": result.testsRun,
        "artifacts": {
            str(p.relative_to(options.output)): file_digest(p)
            for p in options.output.rglob("*")
            if p.is_file() and not p.name.startswith("exchange.db") and p.name != "key.pem"
        },
    }
    (options.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    assert manifest["sources_unchanged"], "runtime sources changed during experiment"
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
