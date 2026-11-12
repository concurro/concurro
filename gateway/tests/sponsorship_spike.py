"""Opt-in, fake-only pinned-gateway and native-CLI sponsorship experiments.

Run with --output NEW_DIRECTORY --clients PINNED_NPM_BIN_DIRECTORY. Docker publishes
only loopback ports; the fake services never make outbound provider requests.
"""

import argparse
import http.client
import json
import re
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from spike_fixtures import (
    CLI_VERSIONS,
    IMAGE,
    SCHEMA,
    DockerGateway,
    FakeServices,
    fake_server,
    file_digest,
    request,
    scan_paths,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "gateway/tests"), str(ROOT / "gateway/exchange")]
from exchange_fixtures import AUDIENCE, BINDING, EXPECTED, Signer

import agent_entry
import processes
import turns
from exchange import Exchange, Gateway, Keys, Refused


class SpikeTests(unittest.TestCase):
    """Use actual gateway/Exchange paths; fake only vault/provider data and native answers."""

    options = None

    def setUp(self):
        """Allocate a fresh database, fake servers and pinned-image gateway per experiment."""
        self.root = self.options.output / self._testMethodName
        self.root.mkdir()
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.state = FakeServices()
        port = self.context.enter_context(fake_server(self.state))
        self.runtime = self.context.enter_context(
            DockerGateway(
                self.root, port, self.options.docker_host, observer=self._testMethodName.startswith("test_observer_")
            )
        )
        signer = Signer(self.root)
        self.signer = signer
        self.gateway = Gateway(self.runtime.admin, self.runtime.inference)
        self.exchange = Exchange(
            str(self.root / "exchange.db"), self.gateway, Keys(fetch=lambda: {"keys": [signer.jwk]}), AUDIENCE
        )
        self.addCleanup(self.exchange.db.close)
        self.expected = {**EXPECTED, "actor_id": "3003", "event_name": "workflow_dispatch"}
        self.sessions = []
        self.addCleanup(self.persist)

    def persist(self):
        """Keep raw fake journals, runtime identities and logs even when assertions fail."""
        value = {
            "provider_requests": self.state.calls,
            "resolver_requests": self.state.resolutions,
            "adapter_observations": self.state.observations,
            "image": {key: self.runtime.identity[key] for key in ("Id", "RepoDigests", "Architecture", "Os")},
        }
        (self.root / "trace.json").write_text(json.dumps(value, indent=2) + "\n")
        logs = self.runtime.docker("logs", self.runtime.name, check=False)
        (self.root / "gateway.log").write_text(logs.stdout)
        (self.root / "gateway.stderr.log").write_text(logs.stderr)
        forbidden = [record["token"].encode() for record in self.state.records.values()]
        forbidden += [record["key"].encode() for record in self.sessions]
        targets = [
            path
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.startswith("exchange.db") and path.name != "gateway.json"
        ]
        self.assertFalse(any(call["route"] == "operator" for call in self.state.calls), "operator fallback reached")
        database_rows = None
        if self._testMethodName.startswith("test_observer_"):
            targets.extend(self.runtime.request_database())
            database = self.root / "requests.db"
            self.assertTrue(database.exists(), "chosen candidate did not create its request-log DB")
            with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as connection:
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                database_rows = {
                    table: connection.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                    for table in tables
                    if not table.startswith("sqlite_")
                }
                self.assertGreater(database_rows.get("request_logs", 0), 0, "request-log database had no request rows")
                self.assertEqual(database_rows.get("request_log_payloads"), 0, "request payload capture was enabled")
        leaks = [str(path.relative_to(self.root)) for path in scan_paths(targets, forbidden)]
        (self.root / "secret-scan.json").write_text(
            json.dumps({"files_scanned": len(targets), "leaks": leaks, "request_database_rows": database_rows}) + "\n"
        )
        self.assertEqual(leaks, [], "synthetic provider/session token found in a prohibited artifact")

    def issue(self, account="alice", provider="codex", route=None, budget=10000, revoke_at=None):
        """Admit/claim a real Exchange turn, with a test-owned synthetic credential association."""
        model = "gpt-5.4" if provider == "codex" else "claude-sonnet-4-5"
        identifier = str(uuid.uuid4())
        reply = self.exchange.submit(
            identifier,
            self.expected,
            [model],
            budget,
            3600,
            {"request": {"stage": "plan", "provider": provider}},
            binding={**BINDING, "role": "plan:" + provider, "phase": uuid.uuid4().hex},
        )
        session = reply["dispatch"]
        self.state.records[session] = {
            "token": "fake-provider-"
            + account
            + "-"
            + provider
            + "-"
            + (route or ("chatgpt" if provider == "codex" else "claude")),
            "account": "fake-account-" + account,
            "route": route or ("chatgpt" if provider == "codex" else "claude"),
            "turn": identifier,
        }
        self.state.credentials[self.state.records[session]["token"]] = {
            "account": "fake-account-" + account,
            "route": self.state.records[session]["route"],
        }
        token = self.signer.token(
            actor_id="3003",
            event_name="workflow_dispatch",
            ref="refs/heads/concurro/runs/" + session,
            run_id=str(len(self.sessions) + 77),
        )
        if revoke_at == "before_key":
            self.state.records[session]["revoked"] = True
        activation_status = []
        activated = self.gateway.activated

        def activation(key):
            """Inject revocation at the actual activation probe without simulating a new guard."""
            if revoke_at == "activation":
                self.state.records[session]["revoked"] = True
            if revoke_at:
                activation_status.append(
                    self.gateway.call(
                        "GET", self.runtime.inference + "/v1/models", headers={"Authorization": "Bearer " + key}
                    )[0]
                )
            return activated(key)

        with patch.object(self.gateway, "activated", side_effect=activation):
            grant = self.exchange.key(session, token)
        if revoke_at == "before_next":
            self.state.records[session]["revoked"] = True
        self.exchange.next(session, grant["token"], 1, 0)
        record = {
            "session": session,
            "turn": identifier,
            "model": model,
            "provider": provider,
            "activation_status": activation_status,
            **grant,
        }
        self.sessions.append(record)
        return record

    def call(self, grant, stream=False, headers=()):
        """Call real inference with a session key, preserving supplied duplicate headers."""
        if grant["provider"] == "codex":
            body = {"model": grant["model"], "input": "fixture:" + grant["turn"], "stream": stream}
            path = "/v1/responses"
        else:
            body = {
                "model": grant["model"],
                "messages": [{"role": "user", "content": "fixture:" + grant["turn"]}],
                "max_tokens": 64,
                "stream": stream,
            }
            path = "/v1/messages"
        return request(
            self.runtime.inference + path, "POST", body, [("Authorization", "Bearer " + grant["key"]), *headers]
        )

    def access_logs(self):
        """Read actual gateway request logs, excluding model-list activation probes."""
        result = self.runtime.docker("logs", self.runtime.name)
        logs = result.stdout + "\n" + result.stderr
        parsed = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
        return [row for row in parsed if row.get("scope") == "request" and row.get("http.method") == "POST"]

    def test_parallel_isolation_and_spoofed_headers(self):
        """Authenticate two accounts per provider and reject client-selected payer metadata."""
        grants = [self.issue(account, provider) for provider in ("codex", "claude") for account in ("alice", "bob")]
        spoof = [
            ("x-concurro-plan", "openai-api"),
            ("X-Concurro-Plan", "operator"),
            ("chatgpt-account-id", "fake-account-mallory"),
            ("ChatGPT-Account-ID", "fake-account-bob"),
            ("x-phase0-request", "forged"),
            ("X-Phase0-Request", "forged-again"),
            ("x-concurro-sponsor", "bob"),
            ("x-internal-auth", "forged"),
            ("X-Phase0-Token", "forged-token"),
            ("x-phase0-account", "forged-account"),
            ("x-phase0-route", "operator"),
            ("x-phase0-turn", "forged-turn"),
        ]
        with ThreadPoolExecutor(4) as pool:
            replies = list(pool.map(lambda grant: self.call(grant, headers=spoof), grants))
        self.assertEqual([reply[0] for reply in replies], [200] * 4)
        if self._testMethodName.startswith("test_observer_"):
            deadline = time.monotonic() + 3
            while len(self.state.observations) < len(grants) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(self.state.observations), len(grants))
        for _status, response_headers, body in replies:
            self.assertFalse(any(name.lower().startswith("x-phase0-") for name in response_headers))
            self.assertFalse(any(record["token"] in body for record in self.state.records.values()))
        for grant in grants:
            calls = [row for row in self.state.calls if "fixture:" + grant["turn"] in json.dumps(row["body"])]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["account"], self.state.records[grant["session"]]["account"])
            self.assertTrue(calls[0]["auth_matches"])
            resolution = [row for row in self.state.resolutions if row["session"] == grant["session"]][-1]
            if self._testMethodName.startswith("test_observer_"):
                observation = next(row for row in self.state.observations if row["request"] == resolution["request"])
                self.assertEqual(observation["session"], grant["session"])
                self.assertEqual(observation["account"], calls[0]["account"])
            else:
                self.assertEqual(calls[0]["request"], resolution["request"])
            if grant["provider"] == "codex":
                self.assertEqual(calls[0]["header_values"]["chatgpt-account-id"], [calls[0]["account"]])
            self.assertNotIn("/operator", calls[0]["path"])
            for header in (
                "x-concurro-plan",
                "x-concurro-sponsor",
                "x-internal-auth",
                "x-phase0-token",
                "x-phase0-account",
                "x-phase0-route",
                "x-phase0-turn",
            ):
                self.assertNotIn(header, calls[0]["headers"])
        beta = self.call(grants[-1], headers=[("anthropic-beta", "fixture-a"), ("Anthropic-Beta", "fixture-b")])
        self.assertEqual(beta[0], 200)
        self.assertEqual(
            set(self.state.calls[-1]["headers"]["anthropic-beta"].split(",")),
            {"fixture-a", "fixture-b", "oauth-2025-04-20"},
        )

    def test_resolver_denial_outage_missing_and_expiry(self):
        """Native authorization denial and unavailable/missing fixture records never reach upstream."""
        grant = self.issue()
        self.assertEqual(self.call(grant)[0], 200)
        outcomes = {}
        for mode in ("revoked", "expired", "outage", "missing"):
            with self.subTest(mode=mode):
                record = self.state.records[grant["session"]]
                if mode == "missing":
                    del self.state.records[grant["session"]]
                elif mode == "outage":
                    self.state.outage = True
                else:
                    record[mode] = True
                before = len(self.state.calls)
                status = self.call(grant)[0]
                outcomes[mode] = status
                self.assertGreaterEqual(status, 400)
                self.assertEqual(len(self.state.calls), before)
                self.state.records[grant["session"]] = record
                record.pop(mode, None)
                self.state.outage = False
                self.assertEqual(self.call(grant)[0], 200)
        (self.root / "denial-statuses.json").write_text(json.dumps(outcomes) + "\n")

    def test_revocation_before_key_activation_and_next_documents_existing_gap(self):
        """The resolver blocks inference although today's Exchange lacks the planned vault boundary checks."""
        outcomes = {}
        for point in ("before_key", "activation", "before_next"):
            grant = self.issue(point, revoke_at=point)
            self.assertTrue(grant["key"])
            self.assertEqual(self.exchange.observation(grant["turn"])["status"], "running")
            before = len(self.state.calls)
            status = self.call(grant)[0]
            self.assertGreaterEqual(status, 400)
            self.assertEqual(len(self.state.calls), before)
            outcomes[point] = {
                "existing_exchange_issued_key": True,
                "existing_next_returned_turn": True,
                "activation_probe_statuses": grant["activation_status"],
                "resolver_inference_status": status,
                "upstream_calls": 0,
            }
        (self.root / "boundary-gaps.json").write_text(json.dumps(outcomes, indent=2) + "\n")

    def test_candidate_account_and_route_validation(self):
        """The independent fake inventory rejects account mismatch and checks selected route forwarding."""
        grant = self.issue()
        record = self.state.records[grant["session"]]
        record["account"] = "fake-account-wrong"
        self.assertEqual(self.call(grant)[0], 401)
        self.assertEqual(self.state.calls[-1]["account"], "fake-account-alice")
        self.assertFalse(self.state.calls[-1]["auth_matches"])
        record["account"] = "fake-account-alice"
        self.assertEqual(self.call(grant)[0], 200)
        other_route = self.issue(route="openai-api")
        self.assertEqual(self.call(other_route)[0], 200)
        self.assertEqual(self.state.calls[-1]["path"], "/openai/responses")
        before = len(self.state.calls)
        status = request(
            self.runtime.inference + "/v1/responses",
            "POST",
            {"model": "claude-sonnet-4-5", "input": "forged model"},
            [("Authorization", "Bearer " + grant["key"])],
        )[0]
        self.assertGreaterEqual(status, 400)
        self.assertEqual(len(self.state.calls), before)

    def test_error_stream_usage_and_log_attribution(self):
        """Measure actual statuses, streaming failure visibility and native token usage fields."""
        grant = self.issue()
        outcomes = {}
        for mode in ("ok", "auth", "quota", "rate", "unknown", "stream_error", "disconnect"):
            self.state.records[grant["session"]]["mode"] = mode
            status, headers, body = self.call(grant, stream=mode in ("ok", "stream_error", "disconnect"))
            outcomes[mode] = {
                "status": status,
                "retry_after": headers.get("retry-after") or headers.get("Retry-After"),
                "body": body,
            }
        self.assertEqual(
            {mode: result["status"] for mode, result in outcomes.items()},
            {"ok": 200, "auth": 401, "quota": 429, "rate": 429, "unknown": 503, "stream_error": 200, "disconnect": 200},
        )
        logs = self.access_logs()
        self.assertEqual(len(logs), 7)
        for log in logs:
            self.assertEqual(log["phase0_session"], grant["session"])
            self.assertEqual(log["phase0_turn"], grant["turn"])
        self.assertEqual(logs[0]["gen_ai.usage.input_tokens"], 2)
        self.assertEqual(logs[0]["gen_ai.usage.output_tokens"], 3)
        self.assertNotIn("gen_ai.usage.input_tokens", logs[-1])
        # Native logs expose HTTP status, not the fixture's structured quota/rate distinction.
        self.assertEqual(logs[2]["http.status"], logs[3]["http.status"])
        self.assertEqual(logs[5]["http.status"], 200)
        # Preserve measured native disclosure as a boolean, with synthetic token bytes redacted in the artifact.
        raw = json.dumps(outcomes, indent=2)
        self.assertIn("FAKE_PROVIDER_DETAIL_SENTINEL", raw)
        for record in self.state.records.values():
            raw = raw.replace(record["token"], "<synthetic-provider-token-redacted>")
        (self.root / "outcomes.json").write_text(raw + "\n")

    def test_delayed_stream_keeps_authorization_snapshot(self):
        """Usage and log identity stay with an earlier turn after a synthetic active-turn change."""
        grant = self.issue()
        record = self.state.records[grant["session"]]
        record["mode"] = "delay"
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, grant, True)
            self.assertTrue(self.state.blocked.wait(5))
            record["turn"] = "fixture-later-turn"
            record["mode"] = "ok"
            self.assertEqual(self.call(grant, True)[0], 200)
            self.state.release.set()
            self.assertEqual(pending.result(timeout=10)[0], 200)
        logs = self.access_logs()
        self.assertEqual({row["phase0_turn"] for row in logs}, {grant["turn"], "fixture-later-turn"})
        self.assertEqual(len({row["phase0_request"] for row in logs}), 2)
        log = next(row for row in logs if row["phase0_turn"] == grant["turn"])
        self.assertEqual(log["gen_ai.usage.output_tokens"], 3)
        self.assertEqual({row["turn"] for row in self.state.calls}, {grant["turn"], "fixture-later-turn"})

    def test_budget_is_native_and_has_no_refill_on_key_reclaim(self):
        """The actual virtual-key token budget blocks after reported usage and is not re-minted."""
        grant = self.issue(budget=4)
        self.assertEqual(self.call(grant)[0], 200)
        before = len(self.state.calls)
        status = self.call(grant)[0]
        self.assertGreaterEqual(status, 400)
        self.assertEqual(len(self.state.calls), before)
        reclaimed = self.exchange.key(
            grant["session"],
            self.signer.token(
                actor_id="3003",
                event_name="workflow_dispatch",
                ref="refs/heads/concurro/runs/" + grant["session"],
                run_id="77",
            ),
        )
        self.assertEqual(reclaimed["key"], grant["key"])
        self.assertGreaterEqual(self.call(grant)[0], 400)
        self.assertEqual(len(self.state.calls), before)
        self.assertEqual(len(self.gateway.list()), 1)
        (self.root / "budget.json").write_text(
            json.dumps(
                {
                    "limit": 4,
                    "reported_total": self.access_logs()[0]["gen_ai.usage.input_tokens"]
                    + self.access_logs()[0]["gen_ai.usage.output_tokens"],
                    "next_status": status,
                }
            )
            + "\n"
        )

    def test_retirement_partial_deletion_restart_and_inflight(self):
        """Real Exchange retirement survives failed deletion/reopen while a paid stream may finish."""
        first, other = self.issue(), self.issue("bob", "claude")
        self.state.records[first["session"]]["mode"] = "delay"
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, first, True)
            self.assertTrue(self.state.blocked.wait(5))
            self.state.records[first["session"]]["revoked"] = True
            with patch.object(self.gateway, "delete", return_value=False), self.assertRaises(Refused):
                self.exchange.end(first["session"])
            self.assertIsNotNone(self.exchange.row(first["session"])["ended"])
            self.assertEqual(self.exchange.observation(first["turn"])["status"], "lost")
            self.state.release.set()
            self.assertEqual(pending.result(timeout=10)[0], 200)
        with self.assertRaises(Refused):
            self.exchange.complete(first["session"], first["token"], first["turn"], {"response": "late"})
        reopened = Exchange(str(self.root / "exchange.db"), self.gateway, self.exchange.keys, AUDIENCE)
        try:
            reopened.reconcile()
            reopened.end(first["session"])  # Lost retirement acknowledgments are replay-safe.
            self.assertIsNone(reopened.row(first["session"])["key"])
        finally:
            reopened.db.close()
        self.assertIsNone(self.gateway.find(first["key"]))
        self.state.records[first["session"]].pop("revoked")
        self.assertEqual(self.call(other)[0], 200)
        before = len(self.state.resolutions)
        self.assertEqual(self.call(first)[0], 401)
        self.assertEqual(len(self.state.resolutions), before)

    def test_observer_structured_errors_streaming_and_native_budget(self):
        """The extra response hop exports typed sanitized observations while native budgets remain in force."""
        for provider in ("codex", "claude"):
            grant = self.issue(provider=provider)
            expected = {
                "ok": None,
                "auth": "auth",
                "quota": "quota_exhausted",
                "rate": "rate_limited",
                "unknown": "unknown",
                "stream_error": "rate_limited",
                "disconnect": "unknown",
            }
            for mode, category in expected.items():
                with self.subTest(provider=provider, mode=mode):
                    self.state.records[grant["session"]]["mode"] = mode
                    before = len(self.state.observations)
                    status, _headers, body = self.call(grant, stream=True)
                    deadline = time.monotonic() + 3
                    while len(self.state.observations) == before and time.monotonic() < deadline:
                        time.sleep(0.01)
                    observed = self.state.observations[-1]
                    self.assertEqual(observed["category"], category)
                    self.assertEqual(observed["turn"], grant["turn"])
                    self.assertEqual(observed["session"], grant["session"])
                    self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", body)
                    self.assertNotIn(self.state.records[grant["session"]]["token"], body)
                    self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", json.dumps(observed))
                    self.assertNotIn(self.state.records[grant["session"]]["token"], json.dumps(observed))
                    if mode == "ok":
                        self.assertEqual(status, 200)
                        self.assertEqual(observed["usage"], {"input_tokens": 2, "output_tokens": 3})
                    if mode == "rate":
                        self.assertEqual(observed["retry_after"], 2)
                    if mode == "disconnect":
                        self.assertIsNone(observed["usage"])
        limited = self.issue("limited", budget=4)
        self.assertEqual(self.call(limited, stream=True)[0], 200)
        before = len(self.state.calls)
        self.assertGreaterEqual(self.call(limited)[0], 400)
        self.assertEqual(len(self.state.calls), before)
        self.assertEqual(self.access_logs()[-1]["http.status"], 429)

    def test_observer_stream_arrives_before_completion_and_keeps_snapshot(self):
        """The hop forwards a first SSE event before upstream completion and freezes attribution."""
        grant = self.issue()
        self.state.records[grant["session"]]["mode"] = "delay"
        from urllib.parse import urlsplit

        endpoint = urlsplit(self.runtime.inference)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=10)
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps({"model": grant["model"], "input": "fixture", "stream": True}),
            {"Content-Type": "application/json", "Authorization": "Bearer " + grant["key"]},
        )
        response = connection.getresponse()
        self.assertTrue(self.state.blocked.wait(3))
        first = response.readline()
        self.assertIn(b"response.created", first)
        self.assertFalse(self.state.release.is_set())
        self.state.records[grant["session"]]["turn"] = "fixture-next-turn"
        self.state.records[grant["session"]]["mode"] = "ok"
        self.assertEqual(self.call(grant, True)[0], 200)
        self.state.release.set()
        response.read()
        connection.close()
        deadline = time.monotonic() + 3
        while len(self.state.observations) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual({row["turn"] for row in self.state.observations}, {grant["turn"], "fixture-next-turn"})
        for row in self.state.observations:
            resolution = next(item for item in self.state.resolutions if item["request"] == row["request"])
            self.assertEqual(row["turn"], resolution["turn"])
            self.assertTrue(row["completed"])

    def test_observer_parallel_isolation_and_spoofed_headers(self):
        """Repeat the real spoof/concurrency assertions through the additional response hop."""
        self.test_parallel_isolation_and_spoofed_headers()
        for call in self.state.calls:
            self.assertFalse(any(name.startswith("x-phase0-") for name in call["headers"]))

    def test_observer_multiple_requests_keep_one_exchange_turn(self):
        """Three HTTP requests retain one actual turn identity; this does not simulate engine charging."""
        grant = self.issue()
        for _ in range(3):
            self.assertEqual(self.call(grant, stream=True)[0], 200)
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 1)
        self.assertEqual({row["turn"] for row in self.state.observations}, {grant["turn"]})
        self.assertEqual(len(self.state.observations), 3)
        self.assertEqual(
            sum(row["usage"]["input_tokens"] + row["usage"]["output_tokens"] for row in self.state.observations), 15
        )

    def test_observer_client_cancellation_bounds_upstream_work(self):
        """Dropping the client stops the offered stream early; exception type records the mechanism."""
        grant = self.issue()
        self.state.records[grant["session"]]["mode"] = "flood"
        from urllib.parse import urlsplit

        endpoint = urlsplit(self.runtime.inference)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=10)
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps({"model": grant["model"], "input": "fixture", "stream": True}),
            {"Content-Type": "application/json", "Authorization": "Bearer " + grant["key"]},
        )
        response = connection.getresponse()
        self.assertIn(b"response.created", response.readline())
        response.close()
        connection.close()
        self.assertTrue(self.state.flood_stopped.wait(5))
        self.assertLess(self.state.flood_written, 2050)
        deadline = time.monotonic() + 4
        while not self.state.observations and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.state.observations[-1]["cancelled"])
        self.assertEqual(self.state.observations[-1]["category"], "unknown")
        self.assertIsNone(self.state.observations[-1]["usage"])
        (self.root / "cancellation.json").write_text(
            json.dumps(
                {
                    "chunks_written": self.state.flood_written,
                    "offered_chunks": 2050,
                    "chunk_bytes": 16384,
                    "termination": self.state.observations[-1].get("termination"),
                    "elapsed_seconds": self.state.observations[-1].get("elapsed_seconds"),
                }
            )
            + "\n"
        )

    def test_observer_pinned_native_fresh_resume_and_fake_account_transfer(self):
        """The additional response hop preserves both pinned native CLI request/history contracts."""
        self.test_pinned_native_fresh_resume_and_fake_account_transfer()

    def native(self, grant, directory, prompt, resume=None, identifier=None, archive=None):
        """Run the pinned native CLI with fresh fake-only homes and the real command builder."""
        directory.mkdir()
        for name in ("workspace", "retained", "scratch", "tmp", "home"):
            (directory / name).mkdir(exist_ok=True)
        if archive is not None:
            turns.unpack(archive, directory / "retained")
        for name in ("codex", "claude", "copilot"):
            (directory / "retained" / name).mkdir(exist_ok=True)
        schema = directory / "schema.json"
        schema.write_text(json.dumps(SCHEMA))
        output = directory / "answer.json"
        provider = grant["provider"]
        configured = {
            "provider": provider,
            "model": grant["model"],
            "effort": "low" if provider == "codex" else "medium",
            "stage": "plan",
            "session": {"scope": "fixture"},
            "session_id": identifier or str(uuid.uuid4()),
            "resume_id": resume,
            "schema": SCHEMA,
            "gateway": {"url": self.runtime.inference, "headers": {}},
        }
        command = agent_entry.command_for(configured, str(schema), str(output))
        command[0] = str(self.options.clients / provider)
        if provider == "claude":
            command += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", ""]
        environment = {
            "PATH": str(self.options.clients) + ":/opt/homebrew/bin:/usr/bin:/bin",
            "HOME": str(directory / "home"),
            "TMPDIR": str(directory / "tmp"),
            "CODEX_HOME": str(directory / "retained/codex"),
            "CLAUDE_CONFIG_DIR": str(directory / "retained/claude"),
            "COPILOT_HOME": str(directory / "retained/copilot"),
            agent_entry.KEY_VARIABLE: grant["key"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        }
        if provider == "claude":
            environment.update({"ANTHROPIC_BASE_URL": self.runtime.inference, "ANTHROPIC_AUTH_TOKEN": grant["key"]})
        process = subprocess.Popen(
            command,
            cwd=directory / "workspace",
            env=environment,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(prompt, timeout=40)
        finally:
            processes.kill_group(process.pid)
            process.wait()
        (directory / "stdout.log").write_text(stdout)
        (directory / "stderr.log").write_text(stderr)
        (directory / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        self.assertEqual(process.returncode, 0, stderr[-1200:])
        if provider == "codex":
            events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
            thread = next(row["thread_id"] for row in events if row.get("type") == "thread.started")
            answer = json.loads(output.read_text())
        else:
            value = json.loads(stdout)
            self.assertFalse(value.get("is_error"), value)
            thread, answer = value["session_id"], value["structured_output"]
        self.assertEqual(set(answer), {"answer"})
        return thread, answer

    def test_pinned_native_fresh_resume_and_fake_account_transfer(self):
        """Restore real CLI history into fresh homes and change only the fake upstream account."""
        versions = {}
        for provider in CLI_VERSIONS:
            binary = self.options.clients / provider
            version = subprocess.run(
                [str(binary), "--version"], check=True, capture_output=True, text=True, timeout=15
            ).stdout.strip()
            versions[provider] = version
            self.assertRegex(version, r"(?<![0-9.])" + re.escape(CLI_VERSIONS[provider]) + r"(?![0-9.])")
            first, second = self.issue("alice", provider), self.issue("bob", provider)
            one, two = self.root / (provider + "-first"), self.root / (provider + "-resume")
            thread, answer = self.native(first, one, "Remember MEMORY_ALICE. Return it in the required answer schema.")
            self.assertEqual(answer, {"answer": "MEMORY_ALICE"})
            archive = turns.pack(one / "retained")
            resumed, result = self.native(
                second,
                two,
                "Return the earlier memory marker, in the required answer schema.",
                resume=thread,
                archive=archive,
            )
            self.assertEqual(resumed, thread)
            self.assertEqual(result, {"answer": "MEMORY_ALICE"})
            for grant in (first, second):
                calls = [
                    row
                    for row in self.state.calls
                    if row["account"] == self.state.records[grant["session"]]["account"]
                    and row["model"] == grant["model"]
                ]
                self.assertTrue(calls)
                for call in calls:
                    self.assertTrue(call["auth_matches"])
                    self.assertIn("MEMORY_ALICE", json.dumps(call["body"]))
                    if provider == "codex":
                        self.assertEqual(call["body"]["reasoning"]["effort"], "low")
                    else:
                        self.assertEqual(call["body"]["thinking"]["type"], "enabled")
                        self.assertEqual(call["body"]["thinking"]["budget_tokens"], 31999)
                        self.assertIn("oauth-2025-04-20", call["headers"]["anthropic-beta"].split(","))
        (self.root / "cli-versions.json").write_text(json.dumps(versions, indent=2) + "\n")


def main():
    """Run only explicit isolated experiments and retain exact result/source identities."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--clients", type=Path, required=True, help="npm node_modules/.bin with exact workflow CLI versions"
    )
    parser.add_argument("--docker-host")
    parser.add_argument("--schema", type=Path, help="public v1.7.0 schema file to fingerprint")
    parser.add_argument("--only", help="one unittest method name for bounded fixture development")
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=False)
    SpikeTests.options = options
    manifest = {
        "source": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip(),
        "image": IMAGE,
        "python": sys.version,
        "cli_pins": CLI_VERSIONS,
        "engine_command_builder_sha256": file_digest(ROOT / "engine/agent_entry.py"),
        "workflow_sha256": file_digest(ROOT / ".github/workflows/agent.yml"),
        "production_gateway_config_sha256": file_digest(ROOT / "gateway/agentgateway.yaml"),
        "schema": {
            "url": "https://raw.githubusercontent.com/agentgateway/agentgateway/v1.7.0/schema/config.json",
            "sha256": file_digest(options.schema) if options.schema else None,
        },
        "fixture_hashes": {
            path.name: file_digest(path)
            for path in (
                Path(__file__),
                Path(__file__).with_name("spike_fixtures.py"),
                Path(__file__).with_name("spike_observer.py"),
            )
        },
        "live_account_validation": "pending; explicitly outside this isolated-only run",
    }
    workflow = (ROOT / ".github/workflows/agent.yml").read_text()
    assert "CODEX_VERSION: " + CLI_VERSIONS["codex"] in workflow
    assert "CLAUDE_VERSION: " + CLI_VERSIONS["claude"] in workflow
    assert IMAGE in (ROOT / "gateway/compose.yaml").read_text()
    manifest["working_tree_status"] = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    manifest["source_hashes"] = {
        str(path): file_digest(ROOT / path)
        for path in (
            Path("gateway/exchange/exchange.py"),
            Path("gateway/tests/exchange_fixtures.py"),
            Path("engine/turns.py"),
            Path("engine/processes.py"),
            Path("gateway/compose.yaml"),
            Path("gateway/tests/test_spike_observer.py"),
        )
    }
    suite = (
        unittest.defaultTestLoader.loadTestsFromName(options.only, SpikeTests)
        if options.only
        else unittest.defaultTestLoader.loadTestsFromTestCase(SpikeTests)
    )
    with (options.output / "tests.log").open("w") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, durations=0).run(suite)
    manifest["config_hashes"] = {
        str(path.relative_to(options.output)): file_digest(path) for path in options.output.glob("*/gateway.json")
    }
    manifest["artifact_hashes"] = {
        str(path.relative_to(options.output)): file_digest(path)
        for path in options.output.rglob("*")
        if path.is_file()
        and (
            path.name
            in (
                "gateway.json",
                "gateway.log",
                "gateway.stderr.log",
                "trace.json",
                "secret-scan.json",
                "tests.log",
                "cli-versions.json",
                "boundary-gaps.json",
                "cancellation.json",
                "budget.json",
                "denial-statuses.json",
                "outcomes.json",
            )
            or path.suffix == ".db"
            or path.name.endswith((".db-wal", ".db-shm"))
        )
    }
    manifest["cases"] = {}
    for case in options.output.glob("test_*"):
        if not (case / "trace.json").exists():
            continue
        trace = json.loads((case / "trace.json").read_text())
        manifest.setdefault("image_identity", trace["image"])
        manifest["cases"][case.name] = {
            "provider_requests": len(trace["provider_requests"]),
            "observations": len(trace["adapter_observations"]),
            "observation_categories": sorted(
                {row["category"] for row in trace["adapter_observations"]}, key=lambda value: value or ""
            ),
            "secret_scan": json.loads((case / "secret-scan.json").read_text())
            if (case / "secret-scan.json").exists()
            else None,
        }
        for name in (
            "cancellation.json",
            "budget.json",
            "boundary-gaps.json",
            "cli-versions.json",
            "denial-statuses.json",
        ):
            if (case / name).exists():
                manifest["cases"][case.name][name.removesuffix(".json")] = json.loads((case / name).read_text())
    manifest["result"] = {
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "successful": result.wasSuccessful(),
    }
    (options.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["result"]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
