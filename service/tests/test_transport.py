"""The transport over a loopback server: request phases, bounds, redirects and per-repository tokens."""

import http.client
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fixtures import KEY, ServiceFixture

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import artifact_capacity
import protocol
import report_artifact
from github import NotFound, Transient
from policy import digest
from protocol import ARTIFACT_BODY_BYTES
from report_store import part_body, record_json
from service import transport
from service.errors import Unresolved
from service.gateway import MutationGateway
from service.transport import Transport
from service.worker import runner_claims

SHA = "a" * 40
OTHER = "other-owner/other-repo"


class Handler(BaseHTTPRequestHandler):
    """Answer from the server's `routes`: `(method, path) -> callable(handler, body)`."""

    def log_message(self, *_):
        pass

    def handle_any(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        self.server.routes[(self.command, self.path)](self, body)

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = handle_any


def reply(status, payload=b"", headers=None, length=None):
    """A route answering with `payload`, optionally declaring a different `Content-Length`."""

    def route(handler, _):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        handler.send_response(status)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(data) if length is None else length))
        handler.end_headers()
        handler.wfile.write(data)
        handler.wfile.flush()
        if length is not None:
            handler.close_connection = True

    return route


def hang_up(handler, _):
    """Read the request, then close without a status line."""
    handler.close_connection = True
    handler.connection.shutdown(socket.SHUT_RDWR)


class Credential:
    """A fixed token that records its use."""

    def __init__(self, token):
        self.token, self.gets, self.dropped = token, 0, []

    def get(self):
        self.gets += 1
        return self.token

    def drop(self, token):
        self.dropped.append(token)


class Loopback:
    """Two loopback servers standing in for the API and codeload hosts."""

    def start(self, case):
        self.servers = {}
        for host in (transport.API, transport.CODELOAD):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.routes, server.seen, server.daemon_threads = {}, [], True
            threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
            case.addCleanup(server.server_close)
            case.addCleanup(server.shutdown)
            self.servers[host] = server
        return self

    def connect(self, host, timeout):
        """Plain HTTP to the loopback server for `host`."""
        return http.client.HTTPConnection("127.0.0.1", self.servers[host].server_port, timeout=timeout)

    def route(self, method, path, handler, host=transport.API):
        self.servers[host].routes[(method, "/" + path)] = handler

    def seen(self, host=transport.API):
        return self.servers[host].seen


def refused(host, timeout):
    """A connection to a port nothing listens on."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)


class ReadTests(unittest.TestCase):
    """Reads return JSON or raise a typed error; nothing ambiguous leaks."""

    def setUp(self):
        self.net = Loopback().start(self)
        self.credential = Credential("read-token")
        self.reader = Transport(KEY[0], self.credential, lambda: 1_000_000, self.net.connect)
        self.root = f"repos/{KEY[0]}/"

    def test_statuses(self):
        """404 is `NotFound`; 5xx, 408, throttling and dropped connections are `Transient`; others name the status."""
        cases = [
            (reply(404, {}), NotFound),
            (reply(502, {}), Transient),
            (reply(408, {}), Transient),
            (
                reply(
                    403,
                    {"message": "API rate limit exceeded"},
                    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000030"},
                ),
                Transient,
            ),
            (hang_up, Transient),
            (reply(403, {"message": "Resource not accessible"}, {"X-RateLimit-Remaining": "4999"}), RuntimeError),
            (reply(200, b'{"partial":', length=100), Transient),
        ]
        for index, (handler, error) in enumerate(cases):
            with self.subTest(case=index):
                self.net.route("GET", self.root + f"case/{index}", handler)
                with self.assertRaises(error) as raised:
                    self.reader.request("GET", self.root + f"case/{index}")
                self.assertIs(type(raised.exception), error)
        self.net.route("GET", self.root + "ok", reply(200, {"number": 7}))
        self.assertEqual(self.reader.request("GET", self.root + "ok"), {"number": 7})
        self.assertEqual(self.net.seen()[-1][2]["Authorization"], "Bearer read-token")

    def test_consolidated_artifact_page_and_immutable_history_fit_transport(self):
        """Ten large text parts fit the existing artifact response allowance, including escaped HTML."""
        # Ampersands expand fivefold in rendered HTML; large data parts avoid JSON highlighting.
        from html import escape

        body = part_body("a" * 64, 1, 1, "&" * (ARTIFACT_BODY_BYTES - 1000), "", "text")
        artifact = {
            "number": 1,
            "name": "Concurro review data",
            "body": body,
            "body_html": "<pre><code>" + escape(body) + "</code></pre>",
        }
        responses = {
            "issues/7/artifacts?per_page=10&page=1": [artifact] * 10,
            "issues/7/artifacts/1": {"artifact": artifact, "versions": [artifact | {"version": 1}]},
        }
        for path, payload in responses.items():
            with self.subTest(path=path):
                self.assertLess(len(json.dumps(payload).encode()), 16 * transport.MB)
                self.net.route("GET", self.root + path, reply(200, payload))
                self.assertEqual(self.reader.request("GET", self.root + path), payload)

    def test_shared_packing_budget_and_transport_shapes(self):
        """POST, detail and one-item listing use the same budget as the actual part writer."""
        from markdown_it import MarkdownIt

        value = {"evidence": '"雪 --> \\u003e ' * 5000}
        encoded = record_json({"value": value, "references": []})
        key = digest(value)
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 160_000):
            layout = report_artifact.pack(key, encoded, report_artifact.submitted(value), True, "owner/repo", 7)
            view = report_artifact.view_for(value, layout)
            bodies = report_artifact.packing_bodies(key, encoded, layout, view, "owner/repo", 7)
            self.assertGreater(len(bodies), 1)
            for body, visible in bodies:
                artifact = {
                    "number": 1,
                    "body": body,
                    "body_html": MarkdownIt("commonmark", {"html": True}).render(visible),
                }
                responses = {
                    "post": ("POST", "issues/7/artifacts", artifact),
                    "listing": ("GET", "issues/7/artifacts?per_page=1&page=1", [artifact]),
                    "detail": (
                        "GET",
                        "issues/7/artifacts/1?per_page=1&page=1",
                        {"artifact": artifact, "versions": [artifact | {"version": 1}]},
                    ),
                }
                for shape, (method, path, payload) in responses.items():
                    with self.subTest(shape=shape):
                        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                        self.assertLessEqual(len(wire), artifact_capacity.response_sizes(body, visible)[shape])
                        self.assertLessEqual(len(wire), protocol.ARTIFACT_RESPONSE_BYTES)
                        self.net.route(method, self.root + path, reply(200, wire))
                        if method == "POST":
                            self.assertEqual(self.reader.send(method, self.root + path, {"body": body}).json(), payload)
                        else:
                            self.assertEqual(self.reader.request(method, self.root + path), payload)
            self.assertEqual(self.reader.limit(self.root + "issues/7/artifacts"), 160_000)
            self.assertEqual(self.reader.limit(self.root + "issues/7"), 4_000_000)
            self.assertEqual(self.reader.limit(self.root + "git/trees/head?recursive=1"), 16_000_000)

    def test_changed_artifact_budget_is_enforced_at_the_wire(self):
        """Changing the shared setting changes exact-byte acceptance without touching generic endpoints."""
        limit = 40_000
        path = self.root + "issues/7/artifacts/1?per_page=1&page=1"
        exact = b'{"p":"' + b"x" * (limit - 8) + b'"}'
        self.assertEqual(len(exact), limit)
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", limit):
            self.net.route("GET", path, reply(200, exact))
            self.assertEqual(self.reader.request("GET", path), json.loads(exact))
            self.net.route("GET", path, reply(200, exact[:-2] + b'x"}'))
            with self.assertRaisesRegex(RuntimeError, "response exceeds limit"):
                self.reader.request("GET", path)

    def test_throttled_delay_is_not_shortened(self):
        """A server-requested delay longer than the local backoff cap is kept."""
        self.net.route("GET", self.root + "retry", reply(403, {}, {"Retry-After": "7200"}))
        self.net.route(
            "GET",
            self.root + "reset",
            reply(429, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(1_000_000 + 7200)}),
        )
        for path in ("retry", "reset"):
            with self.subTest(path=path), self.assertRaises(Transient) as raised:
                self.reader.request("GET", self.root + path)
            self.assertEqual((raised.exception.code, raised.exception.retry_after), ("throttled", 7200))

    def test_graphql_throttling_with_http_200(self):
        """A rate-limit error in a 200 GraphQL response is `Transient`; other GraphQL errors go to the engine."""
        limited = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        cases = [
            (limited, {"Retry-After": "30"}, 30),
            (limited, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(1_000_000 + 7200)}, 7200),
            ({"errors": [{"message": "You have exceeded a secondary rate limit"}]}, {}, 60),
        ]
        query = {"query": "query { viewer { login } }"}
        for index, (payload, headers, wait) in enumerate(cases):
            with self.subTest(case=index):
                self.net.route("POST", "graphql", reply(200, payload, headers))
                with self.assertRaises(Transient) as raised:
                    self.reader.request("POST", "graphql", query)
                self.assertEqual((raised.exception.code, raised.exception.retry_after), ("throttled", wait))
        other = {"data": None, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}
        self.net.route("POST", "graphql", reply(200, other, {"X-RateLimit-Remaining": "0"}))
        self.assertEqual(self.reader.request("POST", "graphql", query), other)
        self.net.route("GET", self.root + "issues/7", reply(200, limited))
        self.assertEqual(self.reader.request("GET", self.root + "issues/7"), limited)

    def test_unauthorized_drops_the_token(self):
        """A 401 is transient and the cached token is forgotten."""
        self.net.route("GET", self.root + "issues/7", reply(401, {}))
        with self.assertRaises(Transient):
            self.reader.request("GET", self.root + "issues/7")
        self.assertEqual(self.credential.dropped, ["read-token"])

    def test_endpoint_limits(self):
        """The measured artifact response and the worst-case bound are read; one byte over is refused."""
        body = 'x"' * 12000
        measured = {"artifact": {"body": body}, "versions": [{"body": body, "version": v} for v in range(30)]}
        data = json.dumps(measured).encode()
        data = data[:-1] + b', "pad": "' + b"p" * (1_493_376 - len(data) - 11) + b'"}'
        self.assertEqual(len(data), 1_493_376)
        worst = json.dumps({"versions": [{"body": "\x01" * 60_000}] * 31}).encode()
        self.assertGreater(len(worst), 11_000_000)
        limit = 16 * transport.MB
        exact = b'{"p": "' + b"p" * (limit - 9) + b'"}'
        self.assertEqual(len(exact), limit)
        artifacts = self.root + "issues/7/artifacts/3"
        for index, payload in enumerate((data, worst, exact)):
            with self.subTest(case=index):
                self.net.route("GET", artifacts, reply(200, payload))
                self.assertIsInstance(self.reader.request("GET", artifacts), dict)
        self.net.route("GET", artifacts, reply(200, exact[:-1] + b'p"}'))
        with self.assertRaisesRegex(RuntimeError, "exceeds limit"):
            self.reader.request("GET", artifacts)
        self.net.route("GET", self.root + "issues/7", reply(200, data * 3))
        with self.assertRaisesRegex(RuntimeError, "exceeds limit"):
            self.reader.request("GET", self.root + "issues/7")

    def test_large_blob_json_is_not_subject_to_the_generic_json_limit(self):
        """Base64 source data may exceed 4 MB without enlarging unrelated API responses."""
        blob = {"encoding": "base64", "content": "eA==" * 1_100_000}
        path = self.root + "git/blobs/" + "a" * 40
        self.net.route("GET", path, reply(200, blob))
        self.assertEqual(self.reader.request("GET", path), blob)
        other = self.root + "issues/7"
        self.net.route("GET", other, reply(200, blob))
        with self.assertRaisesRegex(RuntimeError, "exceeds limit"):
            self.reader.request("GET", other)

    def test_foreign_paths_are_refused_before_a_token_is_used(self):
        """Another repository, or an App endpoint, never sees this transport's token."""
        for path in (f"repos/{OTHER}/issues/7", "installation/repositories", f"repos/{KEY[0]}x/issues/7"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.reader.request("GET", path)
        self.assertEqual((self.credential.gets, self.net.seen()), (0, []))

    def test_repository_metadata_uses_only_its_read_token(self):
        self.net.route("GET", f"repos/{KEY[0]}", reply(200, {"full_name": KEY[0], "private": False}))
        self.assertEqual(self.reader.request("GET", f"repos/{KEY[0]}")["full_name"], KEY[0])
        with self.assertRaises(ValueError):
            self.reader.request("PATCH", f"repos/{KEY[0]}", {})

    def test_runner_claims_are_read_through_the_repositorys_transport(self):
        """GraphQL for the repository and one public bot profile; no other account or repository is readable."""
        self.net.route(
            "POST",
            "graphql",
            reply(
                200,
                {
                    "data": {
                        "repository": {
                            "databaseId": 11,
                            "owner": {"databaseId": 22},
                            "defaultBranchRef": {"name": "main"},
                        }
                    }
                },
            ),
        )
        self.net.route("GET", "users/concurro%5Bbot%5D", reply(200, {"id": 33, "login": "concurro[bot]"}))
        claims = runner_claims(
            self.reader, KEY[0], "o/r/.github/workflows/agent.yml@refs/heads/main", SHA, "concurro[bot]"
        )
        self.assertEqual(
            (claims["repository_id"], claims["repository_owner_id"], claims["actor_id"], claims["ref"]),
            ("11", "22", "33", ""),
        )
        for method, path in (
            ("GET", "users/concurro%5Bbot%5D/repos"),
            ("GET", "users/a/../../orgs"),
            ("PATCH", "users/concurro%5Bbot%5D"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.reader.request(method, path)
        with self.assertRaises(ValueError):
            self.reader.send("GET", "users/concurro%5Bbot%5D")

    def test_each_repository_carries_its_own_token(self):
        """Two targets' transports never mix tokens."""
        other = Transport(OTHER, Credential("other-token"), lambda: 0, self.net.connect)
        self.net.route("GET", self.root + "issues/7", reply(200, {}))
        self.net.route("GET", f"repos/{OTHER}/issues/7", reply(200, {}))
        self.reader.request("GET", self.root + "issues/7")
        other.request("GET", f"repos/{OTHER}/issues/7")
        tokens = [(path, headers["Authorization"]) for _, path, headers in self.net.seen()]
        self.assertEqual(
            tokens,
            [("/" + self.root + "issues/7", "Bearer read-token"), (f"/repos/{OTHER}/issues/7", "Bearer other-token")],
        )


class Short:
    """A connection whose response ends early without raising, as `http.client` may for a declared length."""

    status = 200

    def __init__(self, host, timeout):
        self.sent = False

    def connect(self):
        pass

    def request(self, *_, **__):
        pass

    def getresponse(self):
        return self

    def getheaders(self):
        return [("Content-Length", "100")]

    def getheader(self, name, default=None):
        return "100" if name.lower() == "content-length" else default

    def read(self, size):
        chunk, self.sent = (b"" if self.sent else b"{}"), True
        return chunk

    def close(self):
        pass


class ExchangeTests(unittest.TestCase):
    """A body shorter than its declared length is incomplete, even when nothing raised."""

    def test_short_body(self):
        response = transport.exchange(Short, transport.API, "GET", "/x", {}, None, 1000, 1)
        self.assertEqual((response.status, response.body, response.body_error), (200, None, "incomplete"))


class ArchiveTests(unittest.TestCase):
    """One redirect to codeload, followed without the API token."""

    def setUp(self):
        self.net = Loopback().start(self)
        self.reader = Transport(KEY[0], Credential("read-token"), lambda: 0, self.net.connect)
        self.path = f"repos/{KEY[0]}/tarball/{SHA}"

    def redirect(self, location):
        self.net.route("GET", self.path, reply(302, b"", {"Location": location}))

    def test_follows_one_redirect_without_authorization(self):
        """The codeload hop carries no `Authorization` header."""
        self.redirect(f"https://codeload.github.com/{KEY[0]}/legacy.tar.gz/{SHA}?token=temporary")
        self.net.route(
            "GET", f"{KEY[0]}/legacy.tar.gz/{SHA}?token=temporary", reply(200, b"tarball"), host=transport.CODELOAD
        )
        self.assertEqual(self.reader.archive(self.path), b"tarball")
        [(_, _, headers)] = self.net.seen(transport.CODELOAD)
        self.assertNotIn("Authorization", headers)

    def test_other_hosts_and_second_redirects_are_refused(self):
        """Neither another host nor a further redirect is followed."""
        for location in (
            "https://evil.example/x",
            "http://codeload.github.com/x",
            "https://codeload.github.com:8443/x",
            "https://user@codeload.github.com/x",
        ):
            with self.subTest(location=location):
                self.redirect(location)
                with self.assertRaisesRegex(RuntimeError, "archive"):
                    self.reader.archive(self.path)
        self.redirect("https://codeload.github.com/again")
        self.net.route(
            "GET", "again", reply(302, b"", {"Location": "https://codeload.github.com/x"}), host=transport.CODELOAD
        )
        with self.assertRaisesRegex(RuntimeError, "archive"):
            self.reader.archive(self.path)
        self.assertEqual(len(self.net.seen(transport.CODELOAD)), 1)

    def test_only_pinned_archives(self):
        """A ref name or another repository is not an archive path."""
        for path in (f"repos/{KEY[0]}/tarball/main", f"repos/{OTHER}/tarball/{SHA}"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.reader.archive(path)


class WriteTests(ServiceFixture, unittest.TestCase):
    """The gateway over the real transport: the request phase decides the settlement."""

    def setUp(self):
        super().setUp()
        self.net = Loopback().start(self)
        self.connection = self.net.connect
        writer = Transport(
            KEY[0], Credential("write-token"), self.clock, lambda host, timeout: self.connection(host, timeout)
        )
        self.gateway = MutationGateway(
            self.registry, lambda repository: writer, lambda repository: self.backend, **self.pacing
        )
        self.generation = self.gateway.acquire(KEY, "worker")
        self.path = f"repos/{KEY[0]}/issues/7/comments"

    def comment(self):
        return self.gateway.mutate(KEY, self.generation, "POST", self.path, {"body": "hello"})

    def test_connection_refused_is_not_sent(self):
        """No request byte was written: `failed`, `Transient`, and the task is not unresolved."""
        self.connection = refused
        with self.assertRaises(Transient) as raised:
            self.comment()
        self.assertEqual(raised.exception.code, "unavailable")
        self.assertEqual((len(self.registry.mutations(KEY, "failed")), self.registry.unresolved(KEY)), (1, []))

    def test_ambiguous_after_the_request_starts(self):
        """A dropped connection after the request, or a 5xx, leaves the task unresolved."""
        for handler in (hang_up, reply(502, {})):
            with self.subTest(handler=handler):
                self.net.route("POST", self.path, handler)
                with self.assertRaises(Unresolved):
                    self.comment()
                [record] = self.registry.unresolved(KEY)
                self.gateway.release(KEY, self.generation, None)
                self.registry.resolve(record.intent, "not-applied", "no such comment", "operator")
                self.generation = self.gateway.acquire(KEY, "worker")

    def test_truncated_success_is_done(self):
        """A 2xx status line establishes the write applied; the lost body becomes `response_lost`."""
        self.net.route("POST", self.path, reply(201, b'{"id": 1', length=100))
        with self.assertRaises(Transient) as raised:
            self.comment()
        self.assertEqual(raised.exception.code, "response_lost")
        self.assertEqual((len(self.registry.mutations(KEY, "done")), self.registry.unresolved(KEY)), (1, []))
        self.assertEqual(self.net.seen()[0][2]["Authorization"], "Bearer write-token")


if __name__ == "__main__":
    unittest.main()
