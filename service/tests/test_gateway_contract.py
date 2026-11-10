"""Characterize gateway admission, binding, ownership and settlement before extraction."""

import json
import logging
import re
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fixtures  # noqa: F401 -- initializes service and engine test paths

from github import MARK_READY, RESOLVE_THREAD, NotFound, Transient
from policy import task_branch
from reviews import FINDING_MARKER
from service import gateway as module
from service.errors import OwnershipLost, Unresolved
from service.gateway import Disallowed, MutationGateway, Rejected, TreeUnavailable
from service.registry import Ownership, PullRequest, TaskBinding
from service.transport import Response
from service.worker import Worker

KEY = ("issues/project", 7)
UPSTREAM, FORK = "upstream/project", "owner/project"
BINDING = TaskBinding(UPSTREAM, FORK, "a" * 40)
BRANCH = task_branch(*KEY)
SESSION = "01234567-89ab-cdef-0123-456789abcdef"
RUN_REF = "concurro/runs/" + SESSION
BOT = "concurro[bot]"
READY_QUERY = "query($id:ID!) { node(id:$id) { ... on PullRequest { number repository { nameWithOwner } } } }"
THREAD_QUERY = (
    "query($id:ID!) { node(id:$id) { ... on PullRequestReviewThread { "
    "pullRequest { number repository { nameWithOwner } } "
    "comments(first:1) { nodes { author { login } body } } } } }"
)


def mutation(ready=True, identifier="node"):
    """An exact engine mutation, with its one variable."""
    return {
        "query": MARK_READY if ready else RESOLVE_THREAD,
        "variables": {"input": {"pullRequestId" if ready else "threadId": identifier}},
    }


def draft():
    """A valid bound-target PR creation body."""
    return {
        "head": "owner:" + BRANCH,
        "head_repo": "project",
        "base": "main",
        "title": "Title",
        "body": "Body",
        "draft": True,
        "maintainer_can_modify": False,
    }


def harness(binding=BINDING, owner=None):
    """A real gateway with independent in-memory registry and transport stand-ins."""
    registry = Mock()
    registry.binding.return_value = binding
    registry.current.return_value = owner or Ownership(3, "worker", 200, True)
    registry.pull.return_value = None
    registry.unresolved.return_value = []
    registry.in_flight.return_value = []
    registry.clock.return_value = 100
    registry.admit.return_value = "intent"
    reader, writer = Mock(), Mock()
    reader.request.return_value = []
    writer.send.return_value = Response(True, 204, {}, b"")
    gateway = MutationGateway(
        registry, lambda repo: writer, lambda repo: reader, BOT, clock=lambda: 0, sleep=lambda delay: None
    )
    return gateway, registry, reader, writer


class ShapeAdmissionTests(unittest.TestCase):
    """Every allow-listed shape, including intentional legacy/target differences."""

    def test_all_write_shapes(self):
        rows = [
            ("POST", "issues/7/labels", {}, KEY[0]),
            ("DELETE", "issues/7/labels/concurro", None, KEY[0]),
            ("POST", "labels", {}, KEY[0]),
            ("POST", "issues/7/comments", {}, KEY[0]),
            ("PATCH", "issues/comments/8", {}, KEY[0]),
            ("POST", "issues/7/artifacts", {}, KEY[0]),
            ("PATCH", "issues/7/artifacts/8", {}, KEY[0]),
            ("POST", "git/refs", {"ref": "refs/heads/" + BRANCH, "sha": "a" * 40}, FORK),
            ("POST", "git/trees", {}, FORK),
            ("POST", "git/blobs", {"content": "text", "encoding": "utf-8"}, FORK),
            ("POST", "git/commits", {}, FORK),
            ("PATCH", "git/refs/heads/" + BRANCH, {"sha": "a" * 40, "force": False}, FORK),
            ("POST", "pulls", draft(), UPSTREAM),
            ("PATCH", "pulls/9", {"body": "Updated description"}, UPSTREAM),
            (
                "POST",
                "pulls/9/reviews",
                {"commit_id": "a" * 40, "event": "COMMENT", "body": "", "comments": []},
                UPSTREAM,
            ),
            (
                "POST",
                "pulls/9/comments",
                {"commit_id": "a" * 40, "path": "file", "subject_type": "file", "body": ""},
                UPSTREAM,
            ),
            ("POST", "pulls/9/comments/8/replies", {"body": "reply"}, UPSTREAM),
        ]
        covered = set()
        for method, path, body, repository in rows:
            with self.subTest(method=method, path=path):
                self.assertTrue(module.allowed(KEY, method, f"repos/{KEY[0]}/{path}", body))
                self.assertTrue(module.allowed_target(KEY, BINDING, method, f"repos/{repository}/{path}", body))
                self.assertFalse(module.allowed_target(KEY, BINDING, method, f"repos/other/project/{path}", body))
                self.assertFalse(module.allowed(KEY, "PUT", f"repos/{KEY[0]}/{path}", body))
                covered.update(
                    (verb, pattern) for verb, pattern in module.WRITES if verb == method and re.fullmatch(pattern, path)
                )
        self.assertEqual(covered, set(module.WRITES))

    def test_body_and_branch_boundaries(self):
        rows = [("POST", "issues/8/comments", {}, False, False)]
        for content, encoding, accepted in (
            ("x", "base64", False),
            (b"x", "utf-8", False),
            ("x\0", "utf-8", False),
            ("\ud800", "utf-8", True),
        ):
            rows.append(("POST", "git/blobs", {"content": content, "encoding": encoding}, accepted, accepted))
        for body in (
            None,
            {},
            {"ref": "refs/heads/other", "sha": "a" * 40},
            {"ref": "refs/heads/" + BRANCH, "sha": "bad"},
        ):
            rows.append(("POST", "git/refs", body, False, False))
        rows += [
            ("PATCH", "git/refs/heads/" + BRANCH, {"sha": "bad", "force": False}, True, False),
            ("PATCH", "git/refs/heads/other", {"sha": "a" * 40, "force": False}, True, False),
            ("PATCH", "git/refs/heads/" + BRANCH, {"sha": "a" * 40, "force": 0}, False, False),
            ("POST", "git/trees", None, True, False),
            ("POST", "git/commits", [], True, False),
            (
                "POST",
                "pulls/9/reviews",
                {"commit_id": "a", "event": "APPROVE", "body": "", "comments": []},
                False,
                False,
            ),
            (
                "POST",
                "pulls/9/comments",
                {"commit_id": "a", "path": "f", "subject_type": "line", "body": ""},
                False,
                False,
            ),
            ("POST", "pulls/9/comments/8/replies", {"body": "", "extra": True}, False, False),
        ]
        for field, values in (
            ("draft", (False, 1)),
            ("maintainer_can_modify", (True, 0, None)),
            ("head", ("owner:other",)),
            ("head_repo", ("other",)),
            ("base", ("bad branch", 1)),
        ):
            for value in values:
                rows.append(("POST", "pulls", {**draft(), field: value}, True, False))
        rows.append(("POST", "pulls", {k: v for k, v in draft().items() if k != "maintainer_can_modify"}, True, False))
        for method, path, body, legacy, target in rows:
            repository = FORK if path.startswith("git/") else UPSTREAM if path.startswith("pulls") else KEY[0]
            with self.subTest(path=path, body=body):
                self.assertEqual(module.allowed(KEY, method, f"repos/{KEY[0]}/{path}", body), legacy)
                self.assertEqual(
                    module.allowed_target(KEY, BINDING, method, f"repos/{repository}/{path}", body), target
                )

    def test_graphql_shapes(self):
        for ready in (True, False):
            for identifier, accepted in (("node", True), (" ", True), ("", False), (None, False), (1, False)):
                body = mutation(ready, identifier)
                for method in ("POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                    with self.subTest(ready=ready, identifier=identifier, method=method):
                        expected = accepted and method == "POST"
                        self.assertEqual(module.allowed(KEY, method, "graphql", body), expected)
                        self.assertEqual(module.allowed_target(KEY, BINDING, method, "graphql", body), expected)
            for body in (
                None,
                {},
                {**mutation(ready), "extra": 1},
                {**mutation(ready), "query": "query { viewer { id } }"},
                {**mutation(ready), "variables": {"input": {}, "extra": 1}},
            ):
                self.assertFalse(module.allowed(KEY, "POST", "graphql", body))
                self.assertFalse(module.allowed_target(KEY, BINDING, "POST", "graphql", body))

    def test_runner_shapes(self):
        create = {"ref": "refs/heads/" + RUN_REF, "sha": "not-validated-by-shape"}
        dispatch = {"ref": RUN_REF, "inputs": {"session": SESSION}}
        rows = [
            ("POST", "git/refs", create, SESSION),
            ("DELETE", "git/refs/heads/" + RUN_REF, None, SESSION),
            ("POST", "actions/workflows/concurro-executor.yml/dispatches", dispatch, SESSION),
            ("POST", "git/refs", {**create, "ref": "refs/heads/concurro/runs/bad"}, None),
            ("POST", "git/refs", {**create, "extra": 1}, None),
            ("DELETE", "git/refs/heads/" + RUN_REF, {}, None),
            ("DELETE", "git/refs/heads/concurro/runs/bad", None, None),
            ("POST", "actions/workflows/other.yml/dispatches", dispatch, None),
        ]
        for change in (
            {"ref": "other"},
            {"inputs": {"session": SESSION, "extra": 1}},
            {"inputs": {"session": "bad"}},
            {"extra": 1},
        ):
            rows.append(("POST", "actions/workflows/concurro-executor.yml/dispatches", {**dispatch, **change}, None))
        for method, path, body, expected in rows:
            with self.subTest(path=path, body=body):
                self.assertEqual(module.runner_write(f"repos/{FORK}/{path}", method, body), expected)


class StatefulAdmissionTests(unittest.TestCase):
    """Registry authority and the runner/target/issue routing order remain separate."""

    def test_empty_thread_refused_before_binding_or_write(self):
        """Empty IDs fail shape admission before binding reads or mutation recording."""
        gateway, registry, reader, writer = harness()
        reader.request.return_value = {"data": {"node": {}}}
        with patch.object(gateway, "bound", wraps=gateway.bound) as bound:
            with self.assertRaisesRegex(Disallowed, "^write is not on the gateway allow-list$"):
                gateway.mutate(KEY, 3, "POST", "graphql", mutation(False, ""))
        bound.assert_not_called()
        reader.request.assert_not_called()
        registry.admit.assert_not_called()
        writer.send.assert_not_called()

    def test_runner_records(self):
        record = {"repository": KEY[0], "issue": KEY[1], "generation": 3, "config_sha": "a" * 40, "removed": 0}
        rows = [
            ("valid", {}, BINDING, FORK, "a" * 40, True, True),
            ("missing", None, BINDING, FORK, "a" * 40, False, False),
            ("removed", {"removed": 1}, BINDING, FORK, "a" * 40, False, False),
            ("task", {"issue": 8}, BINDING, FORK, "a" * 40, False, False),
            ("binding", {}, None, FORK, "a" * 40, False, False),
            ("destination", {}, BINDING, UPSTREAM, "a" * 40, False, False),
            ("sha", {}, BINDING, FORK, "b" * 40, False, True),
            ("generation", {"generation": 2}, BINDING, FORK, "a" * 40, False, True),
        ]
        for name, change, binding, repo, sha, create_ok, delete_ok in rows:
            with self.subTest(name=name):
                gateway, registry, _, _ = harness(binding)
                registry.runner_session.return_value = {**record, **change} if change is not None else None
                self.assertEqual(
                    gateway.runner_allowed(
                        KEY, 3, "POST", f"repos/{repo}/git/refs", {"ref": "refs/heads/" + RUN_REF, "sha": sha}
                    ),
                    create_ok,
                )
                self.assertEqual(
                    gateway.runner_allowed(KEY, 3, "DELETE", f"repos/{repo}/git/refs/heads/{RUN_REF}", None), delete_ok
                )
        gateway, registry, _, writer = harness(owner=Ownership(4, "new", 200, True))
        registry.runner_session.return_value = {**record, "generation": 2}
        with self.assertRaises(OwnershipLost):
            gateway.mutate(KEY, 3, "DELETE", f"repos/{FORK}/git/refs/heads/{RUN_REF}")
        writer.send.assert_not_called()
        registry.admit.assert_not_called()

    def test_routing_order(self):
        rows = [
            (BINDING, True, False, "graphql", True, ["runner", "bound", "send"]),
            (BINDING, False, True, "graphql", True, ["runner", "target", "bound", "send"]),
            (BINDING, False, False, "graphql", True, ["runner", "target"]),
            (BINDING, False, True, "graphql", False, ["runner", "target", "bound"]),
        ]
        for relative in ("issues/7/comments", "labels", "git/trees", "pulls", "graphql"):
            path = relative if relative == "graphql" else f"repos/{KEY[0]}/{relative}"
            allowed = relative in ("issues/7/comments", "labels")
            rows.append(
                (None, False, True, path, True, ["runner", "issue", "bound", "send"] if allowed else ["runner"])
            )
        for binding, runner, shape, path, bound, expected in rows:
            with self.subTest(binding=binding, runner=runner, shape=shape, path=path, bound=bound):
                gateway, _, _, _ = harness(binding)
                trace = []
                with (
                    patch.object(gateway, "runner_allowed", side_effect=lambda *args: trace.append("runner") or runner),
                    patch.object(module, "allowed_target", side_effect=lambda *args: trace.append("target") or shape),
                    patch.object(module, "allowed", side_effect=lambda *args: trace.append("issue") or shape),
                    patch.object(gateway, "bound", side_effect=lambda *args: trace.append("bound") or bound),
                    patch.object(gateway, "_send", side_effect=lambda *args: trace.append("send")),
                ):
                    if expected[-1] == "send":
                        gateway.mutate(KEY, 3, "POST", path, {})
                    else:
                        with self.assertRaisesRegex(Disallowed, "^write is not on the gateway allow-list$"):
                            gateway.mutate(KEY, 3, "POST", path, {})
                self.assertEqual(trace, expected)


class BindingTests(unittest.TestCase):
    """Pin each binding predicate and the complete ordered GitHub read requests."""

    def test_empty_thread_uses_unmatched_path_fallback(self):
        """Binding does not duplicate shape validation or look up an empty thread."""
        gateway, _, reader, _ = harness()
        reader.request.return_value = {"data": {"node": {}}}
        self.assertTrue(gateway.bound(KEY, "graphql", mutation(False, "")))
        reader.request.assert_not_called()

    def test_binding_table(self):
        rows = [
            ("ready", {}, True, ["node", "pull"]),
            ("thread", {}, True, ["node", "pull"]),
            ("thread-short-login", {"login": "concurro"}, True, ["node", "pull"]),
            ("thread-human", {"login": "human"}, False, ["node", "pull"]),
            ("thread-unmarked", {"finding": "plain"}, False, ["node", "pull"]),
            ("ready-empty-node", {"node": None}, False, ["node"]),
            ("thread-empty-node", {"node": {}}, False, ["node"]),
            ("ready-bool", {"number": True}, False, ["node"]),
            ("thread-bool", {"number": True}, True, ["node", "pull"]),
            ("ready-wrong-repo", {"node_repo": "other/project"}, False, ["node"]),
            ("thread-wrong-repo", {"node_repo": "other/project"}, False, ["node"]),
            ("ready-missing-binding", {"binding": None}, False, ["node", "pull"]),
            ("ready-no-bot", {"bot": None}, False, ["node", "pull"]),
            ("ready-wrong-branch", {"branch": "other"}, False, ["node", "pull"]),
            ("ready-wrong-fork", {"fork": "other/project"}, False, ["node", "pull"]),
            ("ready-wrong-author", {"author": "human"}, False, ["node", "pull"]),
            ("ready-recorded-number", {"recorded": PullRequest(UPSTREAM, 10)}, False, ["node"]),
            ("ready-recorded-repo", {"recorded": PullRequest("other/project", 9)}, False, ["node"]),
            ("edit", {}, True, ["edit"]),
            ("edit-other-issue", {"issue": 8}, False, ["edit"]),
            ("edit-unmarked", {"editable": "plain"}, False, ["edit"]),
            ("edit-human", {"author": "human"}, False, ["edit"]),
            ("reply", {}, True, ["pull", "reply"]),
            ("reply-other-pr", {"reply_pr": 10}, False, ["pull", "reply"]),
            ("reply-wrong-branch", {"branch": "other"}, False, ["pull"]),
            ("ordinary", {}, True, []),
        ]
        for name, changes, expected, reads in rows:
            with self.subTest(name=name):
                binding = changes.get("binding", BINDING)
                repo = binding.upstream if binding else KEY[0]
                number = changes.get("number", 9)
                node_pr = {"number": number, "repository": {"nameWithOwner": changes.get("node_repo", repo)}}
                first = {
                    "author": {"login": changes.get("login", BOT)},
                    "body": changes.get("finding", FINDING_MARKER + "F1 -->"),
                }
                ready = name.startswith("ready")
                node = node_pr if ready else {"pullRequest": node_pr, "comments": {"nodes": [first]}}
                node = changes.get("node", node)
                pr = {
                    "user": {"login": changes.get("author", BOT)},
                    "head": {"ref": changes.get("branch", BRANCH), "repo": {"full_name": changes.get("fork", FORK)}},
                }
                edit_path = f"repos/{KEY[0]}/issues/comments/8"
                pull_path = f"repos/{repo}/pulls/{number}"
                reply_path = f"repos/{repo}/pulls/comments/8"
                query = READY_QUERY if ready else THREAD_QUERY
                requests = {
                    "node": (repo, "POST", "graphql", {"query": query, "variables": {"id": "node"}}),
                    "pull": (repo, "GET", pull_path, None),
                    "edit": (KEY[0], "GET", edit_path, None),
                    "reply": (repo, "GET", reply_path, None),
                }
                responses = {
                    "graphql": {"data": {"node": node}},
                    pull_path: pr,
                    edit_path: {
                        "user": {"login": changes.get("author", BOT)},
                        "body": changes.get("editable", "<!-- concurro:event:v1:x -->"),
                        "issue_url": "https://github.invalid/issues/" + str(changes.get("issue", 7)),
                    },
                    reply_path: {
                        **first,
                        "user": first["author"],
                        "pull_request_url": "https://github.invalid/pulls/" + str(changes.get("reply_pr", 9)),
                    },
                }
                gateway, registry, _, _ = harness(binding)
                registry.pull.return_value = changes.get("recorded")
                gateway.bot_login = changes.get("bot", BOT)
                actual = []

                def reader(repository):
                    def request(method, path, body=None):
                        actual.append((repository, method, path, body))
                        return deepcopy(responses[path])

                    return SimpleNamespace(request=request)

                gateway.reader = reader
                if name.startswith(("ready", "thread")):
                    path, body = "graphql", mutation(ready)
                elif name.startswith("edit"):
                    path, body = edit_path, {"body": "edited"}
                elif name.startswith("reply"):
                    path, body = f"repos/{repo}/pulls/9/comments/8/replies", {"body": "reply"}
                else:
                    path, body = f"repos/{FORK}/git/trees", {}
                self.assertEqual(gateway.bound(KEY, path, body), expected)
                self.assertEqual(actual, [requests[k] for k in reads])
                if name == "ready":
                    self.assertTrue(gateway.bound(KEY, path, body))
                    self.assertEqual(actual, [requests["node"], requests["pull"], requests["node"]])


class OwnershipTests(unittest.TestCase):
    """Generation authority, stop replay and worker lease expiry retain distinct meanings."""

    def test_gateway_ownership_table(self):
        for site in ("check", "dispatch", "send"):
            for owner in (
                None,
                Ownership(3, "w", 99, True),
                Ownership(3, "w", 100, True),
                Ownership(3, "w", 101, True),
                Ownership(4, "w", 101, True),
                Ownership(3, "w", 101, False),
            ):
                with self.subTest(site=site, owner=owner):
                    gateway, registry, _, writer = harness()
                    registry.current.return_value = owner
                    gateway.mutate = Mock()
                    valid = owner is not None and owner.live and owner.generation == 3

                    def invoke():
                        if site == "check":
                            return gateway.check_owner(KEY, 3)
                        if site == "dispatch":
                            return gateway.dispatch_runner(KEY, 3, SESSION)
                        return gateway._send(KEY, 3, "POST", f"repos/{KEY[0]}/issues/7/comments", {}, BINDING)

                    if valid:
                        invoke()
                    else:
                        with self.assertRaises(OwnershipLost) as error:
                            invoke()
                        expected = (
                            "generation 3 no longer owns issues/project#7"
                            if site == "send"
                            else "runner task ownership changed"
                        )
                        self.assertEqual(str(error.exception), expected)
                        registry.admit.assert_not_called()
                        writer.send.assert_not_called()
                    self.assertEqual(registry.register_runner.called, valid and site == "dispatch")

    def test_first_stop_and_dispatch_order(self):
        gateway, registry, reader, _ = harness()
        gateway.stops[(KEY, 3)] = Unresolved("first stop")
        for invoke in (
            lambda: gateway.check_owner(KEY, 3),
            lambda: gateway._send(KEY, 3, "POST", "path", {}, BINDING),
            lambda: gateway.halt(KEY, 3, OwnershipLost("later stop")),
        ):
            with self.assertRaises(Unresolved) as error:
                invoke()
            self.assertEqual(str(error.exception), "first stop")
            registry.current.assert_not_called()
        # dispatch_runner registers and reads before mutate gets a chance to replay a stop.
        order = []
        registry.register_runner.side_effect = lambda *args: order.append("register")
        reader.request.side_effect = lambda *args: order.append("read") or []
        gateway.mutate = Mock(side_effect=lambda *args: order.append("mutate"))
        gateway.dispatch_runner(KEY, 3, SESSION)
        self.assertEqual(order, ["register", "read", "mutate", "mutate"])
        self.assertEqual(str(gateway.stop(KEY, 3)), "first stop")

    def test_unresolved_messages(self):
        for site in ("check", "send"):
            gateway, registry, _, writer = harness()
            registry.unresolved.return_value = ["unknown"]
            with self.assertRaises(Unresolved) as error:
                if site == "check":
                    gateway.check_owner(KEY, 3)
                else:
                    gateway._send(KEY, 3, "POST", "path", {}, BINDING)
            self.assertEqual(
                str(error.exception),
                "runner task has an unresolved mutation"
                if site == "check"
                else "issues/project#7 has an unresolved write",
            )
            writer.send.assert_not_called()
            registry.admit.assert_not_called()

    def test_worker_lease_boundary(self):
        for until, ended in ((99, True), (100, True), (101, False)):
            with self.subTest(until=until):
                gateway, registry, _, _ = harness(owner=Ownership(3, "w", until, True))
                registry.runner_sessions.return_value = [(SESSION, *KEY, 3)]
                worker = object.__new__(Worker)
                worker.registry, worker.gateway = registry, gateway
                worker.settings = SimpleNamespace(
                    gateway=SimpleNamespace(url="https://gateway.invalid", operator_token_path=Path("token"))
                )
                with (
                    patch("service.worker.private.read", return_value=b"operator"),
                    patch("service.worker.Operator") as operator,
                    patch.object(gateway, "acquire", return_value=None) as acquire,
                ):
                    worker.cleanup_runners()
                self.assertEqual(operator.return_value.end.called, ended)
                self.assertEqual(acquire.called, ended)


class SettlementTests(unittest.TestCase):
    """Exact status, payload, exception and warning contracts for both APIs."""

    def test_thread_confirmation_requires_nonempty_request_id(self):
        """A valid thread needs only isResolved=True, while an empty request stays unknown."""
        for identifier in ("", "node"):
            for thread in ({"isResolved": True}, {"id": "different", "isResolved": True}):
                with self.subTest(identifier=identifier, thread=thread):
                    payload = {"data": {"resolveReviewThread": {"thread": thread}}}
                    response = Response(True, 200, {}, json.dumps(payload).encode())
                    result = module.settle("graphql", response, 100, mutation(False, identifier))
                    self.assertEqual(result, ("done", payload, None) if identifier else ("unknown", None, None))

    def test_rest_settlement_table(self):
        path = f"repos/{FORK}/git/commits"
        rows = [
            (False, 422, {}, b"{}", "failed", Transient, "GitHub unavailable; retry later.", None),
            (True, 401, {}, b"{}", "failed", Transient, "GitHub unavailable; retry later.", None),
            (
                True,
                403,
                {"retry-after": "8"},
                b"{}",
                "failed",
                Transient,
                "GitHub rate limit reached; retry later.",
                None,
            ),
            (
                True,
                403,
                {},
                b'{"message":"denied"}',
                "failed",
                Rejected,
                "GitHub rejected the write (HTTP 403): denied",
                "denied",
            ),
            (True, 404, {}, b"{}", "failed", NotFound, "GitHub rejected the write (HTTP 404)", None),
            (True, 204, {}, None, "done", None, None, None),
            (
                True,
                201,
                {},
                None,
                "done",
                Transient,
                "GitHub response lost after the write applied; reconstruct on retry.",
                None,
            ),
            (True, 200, {}, b'{"id":1}', "done", None, None, None),
            (
                True,
                422,
                {},
                b'{"message":"Tree SHA does not exist"}',
                "failed",
                TreeUnavailable,
                "GitHub rejected the write (HTTP 422): Tree SHA does not exist",
                "Tree SHA does not exist",
            ),
            (
                True,
                422,
                {},
                b'{"errors":[{"field":"tree","code":"missing"}]}',
                "failed",
                TreeUnavailable,
                "GitHub rejected the write (HTTP 422): no message; field=tree code=missing",
                "no message; field=tree code=missing",
            ),
            (
                True,
                422,
                {},
                b'{"message":"invalid"}',
                "failed",
                Rejected,
                "GitHub rejected the write (HTTP 422): invalid",
                "invalid",
            ),
            (
                True,
                400,
                {},
                b"bad",
                "failed",
                Rejected,
                "GitHub rejected the write (HTTP 400): no message",
                "no message",
            ),
        ]
        rows += [(True, status, {}, b"{}", "unknown", None, None, None) for status in (301, 302, 408, 429, 500, 503)]
        for sent, status, headers, raw, expected, error_type, message, warning in rows:
            with self.subTest(status=status, raw=raw):
                with patch.object(logging.getLogger("concurro.service"), "warning") as log:
                    result, payload, error = module.settle(
                        path, Response(sent, status, headers, raw), 100, {"tree": "a" * 40}
                    )
                self.assertEqual(result, expected)
                self.assertIs(type(error) if error else None, error_type)
                self.assertEqual(str(error) if error else None, message)
                self.assertEqual(payload, {"id": 1} if raw == b'{"id":1}' else None)
                if status == 403 and headers:
                    self.assertEqual(error.code, "throttled")
                    self.assertEqual(error.retry_after, 8)
                if warning is None:
                    log.assert_not_called()
                else:
                    log.assert_called_once_with("GitHub rejected a write to %s (HTTP %s): %s", path, status, warning)

    def test_graphql_settlement_table(self):
        rows = []
        for ready in (True, False):
            good = {"data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "node", "isDraft": False}}}}
            if not ready:
                good = {"data": {"resolveReviewThread": {"thread": {"id": "different", "isResolved": True}}}}
            rows += [(ready, 200, json.dumps(good).encode(), "done")]
            for raw in (
                None,
                b"bad",
                b"{}",
                b"null",
                json.dumps({**good, "errors": []}).encode(),
                json.dumps({**good, "errors": [{"message": "error"}]}).encode(),
            ):
                rows.append((ready, 200, raw, "unknown"))
            rows.append((ready, 201, json.dumps(good).encode(), "unknown"))
        for pr in (
            {"id": "wrong", "isDraft": False},
            {"id": "node", "isDraft": True},
            {"id": "node"},
            {"id": "node", "isDraft": 0},
        ):
            rows.append(
                (
                    True,
                    200,
                    json.dumps({"data": {"markPullRequestReadyForReview": {"pullRequest": pr}}}).encode(),
                    "unknown",
                )
            )
        for thread in ({"isResolved": False}, {}, {"isResolved": 1}):
            rows.append(
                (False, 200, json.dumps({"data": {"resolveReviewThread": {"thread": thread}}}).encode(), "unknown")
            )
        for ready, status, raw, expected in rows:
            with self.subTest(ready=ready, status=status, raw=raw):
                with patch.object(logging.getLogger("concurro.service"), "warning") as log:
                    result, payload, error = module.settle(
                        "graphql", Response(True, status, {}, raw), 100, mutation(ready)
                    )
                self.assertEqual((result, error), (expected, None))
                self.assertEqual(payload, json.loads(raw) if expected == "done" else None)
                log.assert_not_called()
