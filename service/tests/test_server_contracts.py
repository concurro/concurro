"""Pin Unix control and HTTP ingress protocols independently of dependency wiring."""

import io
import socket
import stat
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from service import control, ingress
from service.errors import OwnershipLost


class RequestStream(io.BytesIO):
    """Record body reads separately from the request-line and header reads."""

    def __init__(self, data):
        super().__init__(data)
        self.reads = []

    def read(self, size=-1):
        self.reads.append(size)
        return super().read(size)


class RequestSocket:
    """A deterministic socket-shaped byte stream for the actual request handlers."""

    def __init__(self, data):
        self.incoming = RequestStream(data)
        self.outgoing = bytearray()

    def makefile(self, *args):
        """Supply the input stream requested by StreamRequestHandler."""
        return self.incoming

    def sendall(self, data):
        """Capture the bytes emitted by socketserver's unbuffered writer."""
        self.outgoing.extend(data)


class ServerFixture:
    """Own temporary state and servers without changing any live service."""

    def directory(self):
        """Allocate a private, portable socket directory."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def start(self, server):
        """Serve concurrently and shut down before closing its socket."""
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def finish():
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()
            self.assertFalse(thread.is_alive())

        self.addCleanup(finish)

    def control_server(self, dependency):
        """Construct a real control server but leave requests under test control."""
        server = control.server(dependency, self.directory())
        self.addCleanup(server.server_close)
        return server

    def ingress_server(self, dependency):
        """Construct a real ingress server on an ephemeral local port."""
        server = ingress.server(dependency, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        return server


class ControlSocketTests(ServerFixture, unittest.TestCase):
    """Newline-delimited JSON, size guards and private socket lifecycle."""

    def dispatch(self, server, data):
        """Pass literal bytes through the installed control handler."""
        stream = RequestSocket(data)
        server.RequestHandlerClass(stream, "local", server)
        return bytes(stream.outgoing)

    def test_control_reply_bytes(self):
        """Valid, malformed and non-object requests keep exact reply bytes and precedence."""
        dependency = control.Control(SimpleNamespace(status=lambda: {"tasks": [7]}), None, None)
        server = self.control_server(dependency)
        cases = [
            (b'{"command": "status"}\n', b'{"ok": true, "result": {"tasks": [7]}}\n'),
            (b'{"command": "unknown"}\n', b'{"ok": false, "error": "unknown command"}\n'),
            (b'{"command": []}\n', b'{"ok": false, "error": "unknown command"}\n'),
            (b"{}\n", b'{"ok": false, "error": "\'command\'"}\n'),
            (b"[]\n", b'{"ok": false, "error": "request must be an object"}\n'),
            (b"null\n", b'{"ok": false, "error": "request must be an object"}\n'),
            (b'"status"\n', b'{"ok": false, "error": "request must be an object"}\n'),
            (
                b"{\n",
                b'{"ok": false, "error": "Expecting property name enclosed in double quotes: line 2 column 1 (char 2)"}\n',
            ),
            (b"", b'{"ok": false, "error": "Expecting value: line 1 column 1 (char 0)"}\n'),
            (b'{"command": "status"}\n{"command": "unknown"}\n', b'{"ok": true, "result": {"tasks": [7]}}\n'),
        ]
        for request, expected in cases:
            with self.subTest(request=request):
                self.assertEqual(self.dispatch(server, request), expected)

    def test_request_size_boundary_and_no_dispatch_on_refusal(self):
        """Exactly 65,536 bytes is accepted; the next byte refuses before dependency access."""
        dependency = Mock()
        dependency.handle.return_value = {"ok": True, "result": "accepted"}
        server = self.control_server(dependency)
        prefix = b'{"command":"status"}'
        self.assertEqual(control.REQUEST_LIMIT, 65_536)
        for length in (65_535, 65_536, 65_537):
            with self.subTest(length=length):
                dependency.reset_mock()
                data = prefix + b" " * (length - len(prefix) - 1) + b"\n"
                expected = (
                    b'{"ok": true, "result": "accepted"}\n'
                    if length <= 65_536
                    else b'{"ok": false, "error": "request too large"}\n'
                )
                self.assertEqual(self.dispatch(server, data), expected)
                if length <= 65_536:
                    dependency.handle.assert_called_once_with({"command": "status"})
                else:
                    dependency.handle.assert_not_called()

    def test_stale_socket_replaced_and_permissions(self):
        """Startup replaces a closed Unix socket and applies owner-only permissions."""
        directory = self.directory()
        path = directory / control.SOCKET
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
        self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
        server = control.server(control.Control(None, None, None), directory)
        self.addCleanup(server.server_close)
        self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_two_control_servers_keep_dependencies_separate(self):
        """Interleaved real socket requests use the server's own controller."""
        directories = [self.directory(), self.directory()]
        dependencies = [Mock(), Mock()]
        for index, (directory, dependency) in enumerate(zip(directories, dependencies, strict=True)):
            dependency.handle.return_value = {"ok": True, "result": index}
            self.start(control.server(dependency, directory))
        for index in (0, 1, 0, 1):
            self.assertEqual(control.request(directories[index], {"command": "status"}), {"ok": True, "result": index})
        for dependency in dependencies:
            self.assertEqual(dependency.handle.call_count, 2)
            dependency.handle.assert_called_with({"command": "status"})


class IngressHttpTests(ServerFixture, unittest.TestCase):
    """HTTP status, body, headers and exception boundaries stay byte-identical."""

    def dispatch(self, server, data):
        """Render a complete HTTP response with fixed standard server/date headers."""
        stream = RequestSocket(data)
        with (
            patch.object(ingress.Handler, "version_string", return_value="test-server"),
            patch.object(ingress.Handler, "date_time_string", return_value="test-date"),
        ):
            server.RequestHandlerClass(stream, ("127.0.0.1", 1), server)
        return stream

    def response(self, status, reason, body):
        """The literal wire contract, including header order and trailing newline."""
        return (
            f"HTTP/1.0 {status} {reason}\r\nServer: test-server\r\nDate: test-date\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body

    def test_length_early_returns(self):
        """Missing, malformed and excessive lengths cause no body read or ingress call."""
        dependency = Mock()
        server = self.ingress_server(dependency)
        self.assertEqual(ingress.BODY_LIMIT, 25_000_000)
        for length, status, reason, body in (
            (None, 411, "Length Required", b"length required\n"),
            ("no", 411, "Length Required", b"length required\n"),
            ("-1", 411, "Length Required", b"length required\n"),
            ("25000001", 413, HTTPStatus(413).phrase, b"payload too large\n"),
        ):
            headers = b"" if length is None else f"Content-Length: {length}\r\n".encode()
            with self.subTest(length=length):
                stream = self.dispatch(server, b"POST / HTTP/1.0\r\n" + headers + b"\r\nbody")
                self.assertEqual(bytes(stream.outgoing), self.response(status, reason, body))
                self.assertEqual(stream.incoming.reads, [])
        dependency.handle.assert_not_called()

    def test_get_routes(self):
        """Only the exact health path is healthy; GET never calls ingress."""
        dependency = Mock()
        server = self.ingress_server(dependency)
        for path, status, reason, body in (
            ("/health", 200, "OK", b"ok\n"),
            ("/", 404, "Not Found", b"not found\n"),
            ("/health?x=1", 404, "Not Found", b"not found\n"),
        ):
            with self.subTest(path=path):
                stream = self.dispatch(server, f"GET {path} HTTP/1.0\r\n\r\n".encode())
                self.assertEqual(bytes(stream.outgoing), self.response(status, reason, body))
        dependency.handle.assert_not_called()

    def test_post_path_headers_body_and_logging(self):
        """POST accepts any path, passes only the three headers, and reads exactly its length."""
        dependency = Mock()
        dependency.handle.return_value = (202, "queued")
        server = self.ingress_server(dependency)
        with patch.object(ingress.log, "debug") as log:
            stream = self.dispatch(
                server,
                b"POST /anything?query=1 HTTP/1.0\r\nContent-Length: 2\r\nX-Hub-Signature-256: sig\r\nX-GitHub-Event: issue_comment\r\nX-GitHub-Delivery: delivery\r\nIgnored: value\r\n\r\n{}extra",
            )
        dependency.handle.assert_called_once_with(
            {"X-Hub-Signature-256": "sig", "X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "delivery"}, b"{}"
        )
        self.assertEqual(
            list(dependency.handle.call_args.args[0]), ["X-Hub-Signature-256", "X-GitHub-Event", "X-GitHub-Delivery"]
        )
        self.assertEqual(stream.incoming.reads, [2])
        self.assertEqual(bytes(stream.outgoing), self.response(202, "Accepted", b"queued\n"))
        log.assert_called_once_with('ingress: "%s" %s %s', "POST /anything?query=1 HTTP/1.0", "202", "-")

    def test_zero_and_maximum_length_missing_headers(self):
        """Zero and the maximum length reach the dependency; absent delivery headers are empty."""
        dependency = Mock()
        dependency.handle.return_value = (200, "ok")
        server = self.ingress_server(dependency)
        for length in (0, 25_000_000):
            with self.subTest(length=length):
                stream = self.dispatch(server, f"POST / HTTP/1.0\r\nContent-Length: {length}\r\n\r\n".encode())
                self.assertEqual(stream.incoming.reads, [length])
                dependency.handle.assert_called_with(
                    {"X-Hub-Signature-256": "", "X-GitHub-Event": "", "X-GitHub-Delivery": ""}, b""
                )
                self.assertEqual(bytes(stream.outgoing), self.response(200, "OK", b"ok\n"))

    def test_exception_response_and_gateway_stop_propagation(self):
        """Ordinary exceptions produce 503, while GatewayStop escapes without a response."""
        dependency = Mock()
        server = self.ingress_server(dependency)
        dependency.handle.side_effect = ValueError("private detail")
        with patch.object(ingress.log, "warning") as log:
            stream = self.dispatch(server, b"POST / HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
        self.assertEqual(bytes(stream.outgoing), self.response(503, "Service Unavailable", b"unavailable\n"))
        log.assert_called_once_with("delivery failed: %s", "ValueError")
        stopped = OwnershipLost("stop")
        dependency.handle.side_effect = stopped
        stream = RequestSocket(b"POST / HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
        with patch.object(ingress.log, "warning") as log, self.assertRaises(OwnershipLost) as raised:
            server.RequestHandlerClass(stream, ("127.0.0.1", 1), server)
        self.assertIs(raised.exception, stopped)
        self.assertEqual(bytes(stream.outgoing), b"")
        log.assert_not_called()

    def test_two_ingress_servers_keep_dependencies_separate(self):
        """Interleaved handlers use their server's dependency, even after another is created."""
        dependencies = [Mock(), Mock()]
        servers = []
        for index, dependency in enumerate(dependencies):
            dependency.handle.return_value = (202, f"server {index}")
            servers.append(self.ingress_server(dependency))
        for index in (0, 1, 0, 1):
            stream = self.dispatch(servers[index], b"POST / HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
            self.assertEqual(bytes(stream.outgoing), self.response(202, "Accepted", f"server {index}\n".encode()))
        self.assertEqual([dependency.handle.call_count for dependency in dependencies], [2, 2])
