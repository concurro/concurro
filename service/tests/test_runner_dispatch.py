"""Only a recorded task session can create a fork run ref and dispatch the caller."""

import unittest
import uuid

from fixtures import KEY, ServiceFixture
from service.gateway import Disallowed, MutationGateway
from service.transport import Response

UPSTREAM, FORK, SHA = "source/project", "owner/project", "a" * 40


class RunnerDispatchTests(ServiceFixture, unittest.TestCase):
    """Session IDs are issued centrally, persisted before dispatch and fenced by generation."""

    def setUp(self):
        super().setUp()
        self.registry.bind(KEY, UPSTREAM, FORK, SHA)
        self.refs, self.sent = {}, []
        outer = self
        class Transport:
            def __init__(inner, repository):
                inner.repository = repository
            def request(inner, method, path, body=None):
                return [{"ref": ref, "object": {"sha": sha}} for ref, sha in outer.refs.items()]
            def send(inner, method, path, body):
                outer.sent.append((inner.repository, method, path, body))
                if method == "POST" and path.endswith("git/refs"):
                    outer.refs[body["ref"]] = body["sha"]
                return Response(True, 204, {}, b"")
        self.gateway = MutationGateway(self.registry, Transport, Transport, **self.pacing)
        self.generation = self.gateway.acquire(KEY, "worker")
        self.session = str(uuid.uuid4())

    def test_dispatch_is_config_pinned_and_fork_only(self):
        self.gateway.dispatch_runner(KEY, self.generation, self.session)
        self.assertEqual(len(self.sent), 2)
        ref = "concurro/runs/" + self.session
        self.assertEqual(self.refs, {"refs/heads/" + ref: SHA})
        self.assertEqual(self.sent[-1], (FORK, "POST", f"repos/{FORK}/actions/workflows/concurro-executor.yml/dispatches",
                                        {"ref": ref, "inputs": {"session": self.session}}))
        self.assertEqual(self.registry.runner_session(self.session)["generation"], self.generation)
        self.assertTrue(all(repository == FORK for repository, *_ in self.sent))

    def test_unrecorded_wrong_repo_sha_role_input_or_generation_is_refused(self):
        root = f"repos/{FORK}/"
        ref = "concurro/runs/" + self.session
        create = {"ref": "refs/heads/" + ref, "sha": SHA}
        with self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, self.generation, "POST", root + "git/refs", create)
        self.registry.register_runner(KEY, self.generation, self.session, SHA)
        requests = [(f"repos/{UPSTREAM}/git/refs", create), (root + "git/refs", {**create, "sha": "b" * 40}),
                    (root + "actions/workflows/other.yml/dispatches", {"ref": ref, "inputs": {"session": self.session}}),
                    (root + "actions/workflows/concurro-executor.yml/dispatches",
                     {"ref": ref, "inputs": {"session": self.session, "role": "develop:codex"}})]
        for path, body in requests:
            with self.assertRaises(Disallowed):
                self.gateway.mutate(KEY, self.generation, "POST", path, body)
        self.gateway.release(KEY, self.generation, None)
        generation = self.gateway.acquire(KEY, "new")
        with self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, generation, "POST", root + "git/refs", create)
        self.assertEqual(self.sent, [])

    def test_existing_ref_must_match_and_record_cannot_be_stolen(self):
        self.refs["refs/heads/concurro/runs/" + self.session] = "b" * 40
        with self.assertRaises(Disallowed):
            self.gateway.dispatch_runner(KEY, self.generation, self.session)
        self.assertEqual(self.sent, [])
        from service.registry import RegistryError
        with self.assertRaises(RegistryError):
            self.registry.register_runner((KEY[0], 99), self.generation, self.session, SHA)

    def test_cleanup_revokes_old_sessions_and_preserves_queue_and_outcome(self):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from service.worker import Worker
        from controller import Outcome
        from relay import ExchangeError
        self.gateway.dispatch_runner(KEY, self.generation, self.session)
        self.gateway.release(KEY, self.generation, Outcome("blocked"))
        self.registry.enqueue(KEY, "new-input", self.clock())
        before = self.registry.status()[0]
        worker = object.__new__(Worker)
        worker.registry, worker.gateway = self.registry, self.gateway
        worker.reader = self.gateway.reader
        worker.settings = SimpleNamespace(gateway=SimpleNamespace(url="https://gateway.invalid", operator_token_path=Path("/token")))
        with patch("service.worker.private.read", return_value=b"operator"), patch("service.worker.Operator") as operator:
            operator.return_value.end.side_effect = ExchangeError(502, "unavailable")
            worker.cleanup_runners()
            self.assertFalse(self.registry.runner_session(self.session)["removed"])
            operator.return_value.end.side_effect = None
            worker.cleanup_runners()
        self.assertTrue(self.registry.runner_session(self.session)["removed"])
        self.assertEqual(self.sent[-1][1:3], ("DELETE", f"repos/{FORK}/git/refs/heads/concurro/runs/{self.session}"))
        self.assertEqual(self.registry.status()[0]["outcome"], before["outcome"])
        self.assertEqual(self.registry.due(self.clock())[0].causes, ("new-input",))
