"""Literal service request contracts before sharing transport plumbing."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from service import cutover, identity, reconciler, transport

BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "concurro-service",
}


class ServiceGitHubIOContractTests(unittest.TestCase):
    """Record calls at the HTTP boundary, including auth and body ordering."""

    def setUp(self):
        """Use fixed test credentials and a fake exchange; no network is involved."""
        self.connection = object()
        self.credential = Mock()
        self.credential.get.return_value = "test-token"
        self.reader = transport.Transport("owner/repo", self.credential, lambda: 0, self.connection)
        self.identity = identity.Identity(SimpleNamespace(app_id=1, private_key_path=None), self.connection)

    def test_reconciler_pagination_edges(self):
        """The service always requests 100, even for artifact paths, and preserves key/query behaviour."""
        for path, key in [
            ("repos/owner/repo/issues/7/artifacts", None),
            ("repos/owner/repo/actions/runs?x=y", "runs"),
            ("repos/owner/repo/issues", ""),
        ]:
            with self.subTest(path=path, key=key):
                reader = Mock()
                batches = [list(range(100)), ["last"]]
                reader.request.side_effect = [{key: b} if key else b for b in batches]
                self.assertEqual(reconciler.pages(reader, path, key), batches[0] + batches[1])
                sep = "&" if "?" in path else "?"
                self.assertEqual(
                    [c.args for c in reader.request.call_args_list],
                    [("GET", f"{path}{sep}per_page=100&page={p}") for p in (1, 2)],
                )

    def test_reconciler_pagination_past_old_bound(self):
        """Reconciliation retains entries beyond the old 100-page ceiling."""
        reader = Mock()
        reader.request.side_effect = [[0] * 100 for _ in range(101)] + [["last"]]
        result = reconciler.pages(reader, "repos/owner/repo/issues")
        self.assertEqual(len(result), 10101)
        self.assertEqual(result[-1], "last")
        self.assertEqual(reader.request.call_count, 102)

    def test_authenticated_headers_and_body_bytes(self):
        """None alone omits Content-Type; default JSON spacing, escaping and key order stay exact."""
        for data, body in [
            (None, None),
            ({}, b"{}"),
            ([], b"[]"),
            (False, b"false"),
            ({"z": "雪", "é": 1}, b'{"z": "\\u96ea", "\\u00e9": 1}'),
        ]:
            for client in ("transport", "identity"):
                with (
                    self.subTest(client=client, data=data),
                    patch(
                        "service.transport.exchange", return_value=transport.Response(True, 200, {}, b"{}")
                    ) as exchange,
                ):
                    if client == "transport":
                        self.reader.call("POST", "repos/owner/repo/issues", data)
                        limit = 4_000_000
                    else:
                        self.identity.api("POST", "app/installations/1/access_tokens", "test-token", data)
                        limit = identity.APP_LIMIT
                    headers = {**BASE_HEADERS, "Authorization": "Bearer test-token"}
                    if data is not None:
                        headers["Content-Type"] = "application/json"
                    path = "/repos/owner/repo/issues" if client == "transport" else "/app/installations/1/access_tokens"
                    exchange.assert_called_once_with(
                        self.connection, "api.github.com", "POST", path, headers, body, limit, 45
                    )
                    # Header insertion order also remains unchanged at each caller.
                    order = (
                        ["Authorization", *BASE_HEADERS] if client == "identity" else [*BASE_HEADERS, "Authorization"]
                    )
                    if data is not None:
                        order.append("Content-Type")
                    self.assertEqual(list(exchange.call_args.args[4]), order)

    def test_authorization_failure_precedes_serialization(self):
        """A credential exception returns not-sent without touching an unserializable body."""
        self.credential.get.side_effect = RuntimeError("unavailable")
        with patch("service.transport.exchange") as exchange, patch("service.transport.json.dumps") as dumps:
            self.assertEqual(
                self.reader.call("POST", "repos/owner/repo/issues", object()), transport.Response(sent=False)
            )
        dumps.assert_not_called()
        exchange.assert_not_called()

    def test_token_dropped_after_401_and_not_for_unauthenticated_call(self):
        """Authorization, exchange and invalidation keep their order and token identity."""
        trace = []
        self.credential.get.side_effect = lambda: trace.append("get") or "test-token"
        self.credential.drop.side_effect = lambda token: trace.append(("drop", token))

        def exchange(*args):
            """Record the request before returning a refusal."""
            trace.append(("send", dict(args[4]), args[5]))
            return transport.Response(True, 401)

        with patch("service.transport.exchange", side_effect=exchange):
            self.reader.call("POST", "repos/owner/repo/issues", {})
            self.reader.call("GET", "/archive", None, authorize=False)
        self.assertEqual(
            trace,
            [
                "get",
                (
                    "send",
                    {**BASE_HEADERS, "Authorization": "Bearer test-token", "Content-Type": "application/json"},
                    b"{}",
                ),
                ("drop", "test-token"),
                ("send", BASE_HEADERS, None),
            ],
        )

    def test_fresh_header_dicts_and_empty_token(self):
        """Each call owns its headers; an empty token still emits Bearer, without a 401 drop."""
        with patch("service.transport.exchange", return_value=transport.Response(True, 401)) as exchange:
            self.reader.call("POST", "/first", {})
            first = exchange.call_args.args[4]
            first["sentinel"] = "first only"
            self.reader.call("GET", "/second", None, authorize=False)
            second = exchange.call_args.args[4]
            self.assertIsNot(first, second)
            self.assertEqual(second, BASE_HEADERS)
            self.credential.reset_mock()
            self.credential.get.return_value = ""
            self.reader.call("GET", "/third", None)
            self.assertEqual(exchange.call_args.args[4], {**BASE_HEADERS, "Authorization": "Bearer "})
            self.credential.drop.assert_not_called()

    def test_cutover_compare_is_unauthenticated(self):
        """Compare remains a single unauthenticated request with its own bound."""
        with patch(
            "service.transport.exchange", return_value=transport.Response(True, 200, {}, b'{"status":"ahead"}')
        ) as exchange:
            self.assertEqual(cutover.compare("owner/repo", "base", "head", self.connection), "ahead")
        exchange.assert_called_once_with(
            self.connection,
            "api.github.com",
            "GET",
            "/repos/owner/repo/compare/base...head",
            BASE_HEADERS,
            None,
            cutover.COMPARE_LIMIT,
            45,
        )

    def test_archive_redirect_drops_authorization(self):
        """Only the first of the two requests carries credentials, with exact hosts and paths."""
        sha = "a" * 40
        responses = [
            transport.Response(True, 302, {"location": "https://codeload.github.com/owner/repo/tar.gz/a?x=y"}),
            transport.Response(True, 200, {}, b"archive"),
        ]
        with patch("service.transport.exchange", side_effect=responses) as exchange:
            self.assertEqual(self.reader.archive("repos/owner/repo/tarball/" + sha), b"archive")
        self.assertEqual(
            [c.args for c in exchange.call_args_list],
            [
                (
                    self.connection,
                    "api.github.com",
                    "GET",
                    "/repos/owner/repo/tarball/" + sha,
                    {**BASE_HEADERS, "Authorization": "Bearer test-token"},
                    None,
                    4_000_000,
                    45,
                ),
                (
                    self.connection,
                    "codeload.github.com",
                    "GET",
                    "/owner/repo/tar.gz/a?x=y",
                    BASE_HEADERS,
                    None,
                    100_000_000,
                    120,
                ),
            ],
        )
        self.credential.get.assert_called_once_with()

    def test_static_token_drop_is_noop(self):
        """The existing cutover import keeps its fixed credential contract."""
        token = cutover.Token("fixed")
        self.assertEqual(token.get(), "fixed")
        self.assertIsNone(token.drop("fixed"))
        self.assertEqual(token.get(), "fixed")
