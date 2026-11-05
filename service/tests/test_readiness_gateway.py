"""PR readiness writes and CI wake-ups remain bound to their source task."""

import json
import unittest

from fixtures import KEY, ServiceFixture
from github import MARK_READY
from policy import task_branch
from service.gateway import Disallowed, MutationGateway, ready_pull, settle
from service.ingress import Ingress
from service.transport import Response

UPSTREAM = "upstream/project"
FORK = "owner/project"
SHA = "a" * 40


def body(node="PR_9"):
    """The engine's exact ready-for-review mutation."""
    return {"query": MARK_READY, "variables": {"input": {"pullRequestId": node}}}


class GatewayReadinessTests(ServiceFixture, unittest.TestCase):
    """A GraphQL node ID does not confer write authority over another PR."""

    def test_binding_and_per_mutation_settlement(self):
        self.registry.bind(KEY, UPSTREAM, FORK, SHA)
        sent = []
        class Transport:
            def __init__(inner, repo):
                inner.repo = repo
            def request(inner, method, path, data=None):
                if path == "graphql":
                    return {"data": {"node": {"number": 9 if data["variables"]["id"] == "PR_9" else 10,
                                              "repository": {"nameWithOwner": UPSTREAM}}}}
                return {"user": {"login": "concurro[bot]"}, "head": {"repo": {"full_name": FORK}, "ref": task_branch(*KEY) if path.endswith("/9") else "other"}}
            def send(inner, method, path, payload):
                sent.append((inner.repo, payload))
                return Response(True, 200, {}, json.dumps({"data": {"markPullRequestReadyForReview": {
                    "pullRequest": {"id": "PR_9", "isDraft": False}}}}).encode())
        gateway = MutationGateway(self.registry, Transport, Transport, "concurro[bot]")
        generation = gateway.acquire(KEY, "worker")
        for payload in (body("PR_10"), {**body(), "query": MARK_READY + " mutation { other }"},
                        {**body(), "variables": {"input": {"pullRequestId": "PR_9", "extra": "x"}}}):
            with self.assertRaises(Disallowed):
                gateway.mutate(KEY, generation, "POST", "graphql", payload)
        self.assertEqual(sent, [])
        gateway.mutate(KEY, generation, "POST", "graphql", body())
        self.assertEqual(sent, [(UPSTREAM, body())])
        for payload in ({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}},
                        {"data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "PR_10", "isDraft": False}}}},
                        {"data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "PR_9", "isDraft": True}}}},
                        {"errors": [{"message": "failed"}]}):
            self.assertEqual(settle("graphql", Response(True, 200, {}, json.dumps(payload).encode()), 0, body())[0], "unknown")

    def test_status_events_map_head_and_merge_commit_to_source_issue(self):
        self.registry.link(KEY, 9, UPSTREAM)
        self.registry.acknowledge(KEY, {"pull": {"head": SHA, "merge_head": "b" * 40}})
        ingress = Ingress(b"secret", self.registry, {UPSTREAM: 1}, set())
        for sha in (SHA, "b" * 40):
            self.assertEqual(ingress.keys(UPSTREAM, "status", {"sender": {"login": "ci"}, "sha": sha}), [KEY])
        self.assertEqual(ingress.keys(UPSTREAM, "status", {"sender": {"login": "ci"}, "sha": "c" * 40}), [])
        self.assertEqual(ingress.keys(FORK, "status", {"sender": {"login": "ci"}, "sha": SHA}), [])
