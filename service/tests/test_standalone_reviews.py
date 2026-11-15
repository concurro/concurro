"""Standalone PR reviews through ingress, registry, gateway and worker."""

import hashlib
import hmac
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fixtures import ENGINE, KEY, SERVICE, Inline, RestBackend, ServiceFixture, target_files

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import delegation
import review_api
from github import Transient
from service.actions import Actions
from service.gateway import Disallowed, MutationGateway
from service.ingress import Ingress
from service.reconciler import Reconciler
from service.scheduler import Scheduler
from service.settings import Limits, Target
from service.shadow import Shadow
from service.targets import permissions, review_targets
from service.worker import Worker
from standalone_fixtures import APP, HEAD, PR, REVIEWER, FakePull
from vocab import CONTRIBUTION, STANDALONE_REVIEW

SECRET = b"webhook-secret"
INSTALLATION = 42
TASK = (KEY[0], PR)
WRITER = {"login": "writer", "id": 77}


def pull_event(action, sender=WRITER, reviewer=REVIEWER, **pull):
    """A `pull_request` delivery for PR #9."""
    return {
        "action": action,
        "repository": {"full_name": KEY[0]},
        "installation": {"id": INSTALLATION},
        "sender": dict(sender),
        "requested_reviewer": {"login": reviewer["login"], "id": reviewer["id"]},
        "pull_request": {
            "number": PR,
            "id": 900,
            "node_id": "PR_9",
            "title": "Explain the term",
            "state": "open",
            "updated_at": "2026-11-15T10:00:00Z",
            "head": {"sha": HEAD, "ref": "explain"},
            **pull,
        },
    }


class Fixture(ServiceFixture):
    """The service over an existing contributor PR, with a configured reviewer account."""

    def setUp(self):
        super().setUp()
        self.fake = FakePull(self.config)
        self.backend = self.reader = RestBackend(self.fake)
        self.gateway = MutationGateway(
            self.registry,
            lambda repository: self.backend,
            lambda repository: self.backend,
            self.config["bot_login"],
            app=APP,
            reviewer=REVIEWER,
            reviewer_transport=lambda repository: self.backend,
            **self.pacing,
        )
        targets = (Target(KEY[0], KEY[0], "both"),)
        self.settings = SimpleNamespace(
            controller_id=SERVICE,
            engine=ENGINE,
            limits=Limits(4, 2),
            targets=targets,
            state_dir=Path(self.directory.name),
        )
        self.reconciler = Reconciler(
            self.registry,
            lambda repository: self.backend,
            [KEY[0]],
            [APP, REVIEWER],
            clock=self.clock,
            review_repositories=[KEY[0]],
        )
        self.worker = Worker(
            self.settings,
            self.gateway,
            self.registry,
            self.reconciler,
            lambda repository: self.backend,
            self.config["bot_login"],
            agents=lambda *args, **kwargs: self.agents,
            executor=lambda repository, github: None,
            actors={"app": dict(APP), "reviewer": dict(REVIEWER)},
            reviewer_reader=lambda repository: self.backend,
        )
        self.scheduler = Scheduler(
            self.registry, self.gateway, self.worker, self.settings.limits, clock=self.clock, executor=Inline()
        )
        self.ingress = Ingress(
            SECRET,
            self.registry,
            {KEY[0]: INSTALLATION},
            [APP, REVIEWER],
            clock=self.clock,
            reviewer=REVIEWER,
            review_targets=review_targets(targets),
        )
        self.deliveries = 0

    def deliver(self, event, data, delivery=None):
        """Sign and deliver one event; return `(status, text)`."""
        self.deliveries += 1
        body = json.dumps(data).encode()
        headers = {
            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or f"delivery-{self.deliveries}",
        }
        return self.ingress.handle(headers, body)

    def request(self, **options):
        return self.deliver("pull_request", pull_event("review_requested"), **options)

    def due(self):
        return [item.key for item in self.registry.due(self.clock())]

    def task_state(self):
        from checkpoints import Checkpoints

        return Checkpoints(self.fake, self.config, PR).load(self.fake.pages(f"issues/{PR}/comments"))

    def journal(self, status="done"):
        return self.registry.mutations(TASK, status)


class EnrollmentTests(Fixture, unittest.TestCase):
    def test_request_enrolls_and_a_round_runs_end_to_end(self):
        self.assertEqual(self.request(), (202, "accepted"))
        self.assertEqual(
            (self.registry.kind(TASK), self.registry.review_target(TASK)), (STANDALONE_REVIEW, (KEY[0], KEY[0]))
        )
        self.assertEqual(self.registry.task(*TASK), TASK)
        self.assertEqual(self.due(), [TASK])
        self.drain()
        state = self.task_state()
        self.assertEqual((state["stage"], state["round"], state["calls"]), ("complete", 1, 2))
        self.assertEqual([r["user"]["login"] for r in self.fake.app_reviews], [REVIEWER["login"]] * 2)
        [run] = self.fake.check_runs
        self.assertEqual((run["status"], run["conclusion"], run["head_sha"]), ("completed", "neutral", HEAD))
        self.assertEqual(self.fake.comments, [])
        self.assertEqual(self.fake.issue["labels"], [])
        self.assertEqual(self.registry.outcome(TASK).kind, "await-human")
        self.assertEqual(self.registry.binding(TASK).upstream, KEY[0])

    def test_each_write_records_the_account_that_sent_it(self):
        self.request()
        self.drain()
        senders = {}
        for record in self.journal():
            kind = "review" if record.path == "graphql" else record.path.split("/")[3].split("?")[0]
            senders.setdefault(kind, set()).add(record.actor["login"])
        self.assertEqual(senders["review"], {REVIEWER["login"]})
        self.assertEqual(senders["issues"], {APP["login"]})
        self.assertEqual(senders["check-runs"], {APP["login"]})

    def test_the_app_resolves_the_reviewers_finding_threads(self):
        from fakes import FakeAgents
        from github import RESOLVE_THREAD
        from service.gateway import request_digest

        self.agents = FakeAgents(fix=True)
        self.request()
        self.drain()
        self.request()
        self.drain()
        [finding] = self.task_state()["findings"]
        self.assertEqual((finding["status"], finding["resolution"]), ("fixed", "done"))
        self.assertEqual(finding["resolved_by"]["login"], APP["login"])
        self.assertEqual(finding["author"]["login"], REVIEWER["login"])
        body = {"query": RESOLVE_THREAD, "variables": {"input": {"threadId": finding["thread"]}}}
        [resolution] = [r for r in self.journal() if r.digest == request_digest("POST", "graphql", body)]
        self.assertEqual(resolution.actor["login"], APP["login"])
        others = {r.actor["login"] for r in self.journal() if r.path == "graphql" and r.intent != resolution.intent}
        self.assertEqual(others, {REVIEWER["login"]})

    def test_redelivery_and_concurrent_requests_share_one_task(self):
        self.assertEqual(self.request(delivery="same"), (202, "accepted"))
        self.assertEqual(self.request(delivery="same"), (200, "duplicate"))
        self.assertEqual(self.request(delivery="other"), (202, "accepted"))
        self.assertEqual(len(self.registry.review_requests(TASK)), 2)
        self.drain()
        state = self.task_state()
        self.assertEqual((len(state["rounds"]), state["calls"], state["rounds"][0]["joined"]), (1, 2, ["other"]))

    def test_request_for_another_reviewer_enrolls_nothing(self):
        other = {"login": REVIEWER["login"], "id": 999}
        status, _ = self.deliver("pull_request", pull_event("review_requested", reviewer=other))
        self.assertEqual((status, self.registry.kind(TASK), self.due()), (202, CONTRIBUTION, []))

    def test_reviewer_matches_by_numeric_id_after_a_rename(self):
        renamed = {"login": "RenamedBot", "id": REVIEWER["id"]}
        self.assertEqual(
            self.deliver("pull_request", pull_event("review_requested", reviewer=renamed)), (202, "accepted")
        )
        self.assertEqual(self.registry.kind(TASK), STANDALONE_REVIEW)

    def test_contribution_owner_keeps_its_pull_request(self):
        self.registry.link((KEY[0], 7), PR, KEY[0])
        self.assertEqual(self.request(), (202, "ignored"))
        self.assertEqual(
            (self.registry.task(*TASK), self.registry.kind(TASK), self.due()), ((KEY[0], 7), CONTRIBUTION, [])
        )

    def test_removal_is_recorded_only_for_an_enrolled_review(self):
        self.assertEqual(self.deliver("pull_request", pull_event("review_request_removed")), (202, "ignored"))
        self.request()
        self.assertEqual(self.deliver("pull_request", pull_event("review_request_removed")), (202, "accepted"))
        self.assertEqual([r["action"] for r in self.registry.review_requests(TASK)], ["requested", "removed"])

    def test_requester_without_write_access_starts_nothing(self):
        self.deliver("pull_request", pull_event("review_requested", sender={"login": "stranger", "id": 5}))
        self.drain()
        self.assertEqual((self.fake.artifacts, self.fake.check_runs, self.agents.calls), ({}, [], []))


class WakeupTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.request()
        self.drain()
        self.assertEqual(self.due(), [])

    def event(self, **fields):
        return {
            "repository": {"full_name": KEY[0]},
            "installation": {"id": INSTALLATION},
            "sender": dict(WRITER),
            **fields,
        }

    def test_ci_pushes_and_edits_do_not_wake_a_review(self):
        check = {"head_sha": HEAD, "pull_requests": [{"number": PR}]}
        for name, data in (
            ("check_run", self.event(action="completed", check_run=check)),
            ("check_suite", self.event(action="completed", check_suite=check)),
            ("status", self.event(sha=HEAD, state="success")),
            ("push", self.event(ref="refs/heads/main")),
            ("pull_request", pull_event("synchronize")),
            ("pull_request", pull_event("edited")),
            ("pull_request_review", self.event(action="submitted", pull_request={"number": PR})),
            ("pull_request_review_thread", self.event(action="resolved", pull_request={"number": PR})),
        ):
            with self.subTest(event=name, action=data.get("action")):
                self.assertEqual(self.deliver(name, data), (202, "ignored"))
        self.assertEqual(self.due(), [])

    def test_a_shared_ci_event_reaches_only_the_contribution_task(self):
        contribution = (KEY[0], 7)
        self.registry.link(contribution, 12, KEY[0])
        self.registry.acknowledge(contribution, {"_stage": "gates", "pull": {"head": HEAD}})
        check = {"head_sha": HEAD, "pull_requests": [{"number": 12}, {"number": PR}]}
        self.assertEqual(self.deliver("check_run", self.event(action="completed", check_run=check)), (202, "accepted"))
        self.assertEqual(self.due(), [contribution])

    def test_closure_and_reopening_wake_it(self):
        self.assertEqual(self.deliver("pull_request", pull_event("closed")), (202, "accepted"))
        self.assertEqual(self.due(), [TASK])

    def comment(self, body, login="writer", action="created", **fields):
        return self.event(
            action=action,
            issue={"number": PR, "pull_request": {}, "labels": [], "user": {"login": "contributor"}},
            comment={"id": 501, "body": body, "user": {"login": login, "type": "User"}, **fields},
        )

    def test_only_commands_in_the_pr_conversation_wake_it(self):
        self.assertEqual(self.deliver("issue_comment", self.comment("Looks good to me.")), (202, "ignored"))
        self.assertEqual(self.deliver("issue_comment", self.comment("/concurro stop")), (202, "accepted"))
        self.assertEqual(self.due(), [TASK])

    def test_own_echoes_do_not_wake_it(self):
        for login in (APP["login"], REVIEWER["login"]):
            data = self.comment("/concurro stop", login=login)
            data["sender"] = {"login": login}
            self.assertEqual(self.deliver("issue_comment", data), (202, "ignored"))

    def test_pr_comments_of_other_pull_requests_stay_ignored(self):
        data = self.comment("/concurro stop")
        data["issue"]["number"] = 10
        self.assertEqual(self.deliver("issue_comment", data), (202, "ignored"))

    def test_command_runs_and_is_acknowledged_once(self):
        self.fake.comment("/concurro retry")
        self.deliver("issue_comment", self.comment("/concurro retry"))
        self.drain()
        [ack] = self.fake.bot_comments()
        self.assertIn("**No-op**", ack["body"])
        self.deliver("issue_comment", self.comment("/concurro retry"), delivery="again")
        self.drain()
        self.assertEqual(len(self.fake.bot_comments()), 1)

    def test_new_request_runs_another_round(self):
        self.request()
        self.drain()
        state = self.task_state()
        self.assertEqual((state["round"], state["calls"], len(self.fake.check_runs)), (2, 4, 2))


class AdmissionTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.request()
        self.generation = self.gateway.acquire(TASK, "test", consume=False)
        self.root = f"repos/{KEY[0]}/"

    def refused(self, method, path, body=None):
        with self.assertRaises(Disallowed):
            self.gateway.mutate(TASK, self.generation, method, path, body)

    def check(self, **fields):
        return {
            "name": "Concurro review - PR 9",
            "head_sha": HEAD,
            "external_id": f"concurro:{KEY[0]}#9:1:1",
            "details_url": f"https://github.com/{KEY[0]}/pull/9",
            "status": "completed",
            "conclusion": "neutral",
            "output": {"title": "Finished", "summary": "Done."},
            **fields,
        }

    def test_a_review_task_writes_only_its_own_surfaces(self):
        for method, path, body in (
            ("POST", "issues/9/labels", {"labels": ["x"]}),
            ("POST", "issues/7/artifacts", {"type": "generic", "name": "n", "body": "b"}),
            ("POST", "git/refs", {"ref": "refs/heads/x", "sha": HEAD}),
            ("POST", "pulls/9/reviews", {"commit_id": HEAD, "event": "COMMENT", "body": "b", "comments": []}),
            ("PATCH", "pulls/9", {"body": "rewritten"}),
            ("PATCH", "issues/comments/1", {"body": "edited"}),
            ("POST", "check-runs", self.check(conclusion="success")),
            ("POST", "check-runs", self.check(conclusion="failure")),
            ("POST", "check-runs", self.check(name="CI")),
            ("POST", "check-runs", self.check(details_url="https://backoffice.example/tasks/9")),
            ("POST", "check-runs", self.check(external_id="concurro:other/repo#9:1:1")),
        ):
            with self.subTest(path=path, body=body):
                self.refused(method, self.root + path, body)

    def test_check_updates_are_bound_to_the_tasks_own_run(self):
        created = self.gateway.mutate(TASK, self.generation, "POST", self.root + "check-runs", self.check())
        update = {"status": "completed", "conclusion": "neutral", "output": {"title": "Finished", "summary": "x"}}
        self.gateway.mutate(TASK, self.generation, "PATCH", f"{self.root}check-runs/{created['id']}", update)
        self.fake.check_runs.append({"id": 4000, "name": "CI", "external_id": "ci", "head_sha": HEAD})
        self.refused("PATCH", self.root + "check-runs/4000", update)

    def test_draft_to_ready_and_foreign_threads_are_refused(self):
        from github import MARK_READY, RESOLVE_THREAD

        self.refused("POST", "graphql", {"query": MARK_READY, "variables": {"input": {"pullRequestId": "PR_9"}}})
        self.fake.thread(self.fake.review_comment({"body": "A human thread."}, path="docs/guide.md"))
        self.fake.review_comments[-1]["user"] = {"login": "human"}
        self.refused("POST", "graphql", {"query": RESOLVE_THREAD, "variables": {"input": {"threadId": "T1"}}})

    def test_contribution_tasks_gain_no_check_or_reviewer_writes(self):
        key = (KEY[0], 7)
        generation = self.gateway.acquire(key, "test", consume=False)
        with self.assertRaises(Disallowed):
            self.gateway.mutate(key, generation, "POST", self.root + "check-runs", self.check())
        self.assertEqual(
            self.gateway.sender(
                key, "graphql", review_api.request("deletePullRequestReview", {"pullRequestReviewId": "R1"})
            )[1].login,
            APP["login"],
        )

    def test_review_writes_need_a_configured_reviewer(self):
        self.gateway.reviewer_transport = None
        body = review_api.request(
            "addPullRequestReview",
            {"pullRequestId": "PR_9", "commitOID": HEAD, "body": f"<!-- concurro:review:v1:{KEY[0]}#9:1 -->"},
        )
        with self.assertRaises(Disallowed):
            self.gateway.mutate(TASK, self.generation, "POST", "graphql", body)

    def test_unknown_check_outcome_is_reconciled_without_an_operator(self):
        self.backend.fail("POST", "check-runs", lost=True)
        with self.assertRaises(Transient):
            self.gateway.mutate(TASK, self.generation, "POST", self.root + "check-runs", self.check())
        self.assertEqual(self.registry.unresolved(TASK), [])
        [record] = self.journal("failed")
        self.assertEqual(record.resolution["disposition"], "discarded")
        self.gateway.release(TASK, self.generation, None)
        self.drain()
        self.assertEqual(self.task_state()["stage"], "complete")
        self.assertEqual(len(self.fake.check_runs), 1)


class DelegatedCommandTests(Fixture, unittest.TestCase):
    """A signed backoffice command addresses the reviewed PR and is authorized against its upstream."""

    KEY = b"d" * 32

    def setUp(self):
        super().setUp()
        self.worker.delegation_key = self.KEY
        self.reconciler.command_comment = self.ingress.command_comment = delegation.marked
        permissions = SimpleNamespace(request=lambda method, path, data=None: {"permission": "write"})
        self.actions = Actions(
            self.KEY,
            self.registry,
            lambda repository: permissions,
            lambda repository: self.backend,
            self.settings.targets,
            user=lambda token: ("writer", 77),
            clock=self.clock,
        )
        self.request()
        self.scheduler.dispatch()

    def command(self, kind, issue=PR):
        body = {"repository": KEY[0].upper(), "issue": issue, "command": kind}
        return self.actions.handle("Bearer token", json.dumps(body).encode())

    def test_a_delegated_stop_pauses_the_review_and_shares_its_outcome_record(self):
        status, reply = self.command("stop")
        self.assertEqual((status, reply["posted"], reply["actor"]), (201, "/concurro stop", "writer"))
        posted = self.fake.comments[-1]
        self.assertTrue(delegation.marked(posted["body"]))
        self.reconciler.observe_pending()
        self.drain()
        state = self.task_state()
        self.assertEqual(state["stage"], "stopped")
        self.assertEqual(state["command_receipts"][str(posted["id"])]["outcome"], "Applied")
        acknowledgements = [c for c in self.fake.bot_comments() if "**Applied**" in c["body"]]
        self.assertEqual(len(acknowledgements), 1)

    def test_an_upstream_that_supplies_no_issues_accepts_only_its_reviewed_pull_requests(self):
        actions = Actions(
            self.KEY,
            self.registry,
            lambda repository: SimpleNamespace(request=lambda *args: {"permission": "write"}),
            lambda repository: self.backend,
            (Target(KEY[0], "fork/repo", "fork"),),
            user=lambda token: ("writer", 77),
            clock=self.clock,
        )

        def command(issue):
            body = {"repository": KEY[0], "issue": issue, "command": "stop"}
            return actions.handle("Bearer token", json.dumps(body).encode())[0]

        self.assertEqual((command(PR), command(PR + 1)), (201, 400))


class FundingTests(Fixture, unittest.TestCase):
    """The production funding component on a reviewed PR."""

    def setUp(self):
        super().setUp()
        backend = self.backend

        def request(method, path, data=None):
            if path.startswith("user/"):
                return {"id": int(path.split("/")[1]), "login": "writer"}
            return backend.request(method, path, data)

        # Routed providers, so initialization completes and reaches its notice.
        self.config["provider_credential_routes"] = {"openai": "openai-api-key", "anthropic": "anthropic-api-key"}
        self.fake.config = self.config
        backend.files = target_files(self.config)
        metadata = SimpleNamespace(read=lambda actor, at=None: [])
        self.shadow = Shadow(self.registry, lambda repository: SimpleNamespace(request=request), Mock, metadata)
        self.worker.sponsorship = self.shadow

    def test_enrollment_records_funding_without_a_lifecycle_comment(self):
        self.request()
        self.drain()
        state = self.task_state()
        self.assertEqual(state["stage"], "complete")
        self.assertIn("funding_initialization", state)
        self.assertEqual(self.fake.comments, [])

    def test_credential_recovery_needs_no_approved_plan(self):
        self.request()
        self.scheduler.dispatch()
        generation = self.gateway.acquire(TASK, "test", consume=False)
        try:
            controller = self.worker_controller(generation)
            self.assertTrue(controller._validate_authority([], stage="review"))
        finally:
            self.gateway.release(TASK, generation, None)

    def worker_controller(self, generation):
        from service.gateway import GatewayGitHub
        from standalone import ReviewController

        target = self.worker.load(*TASK)
        facade = GatewayGitHub(TASK[0], TASK[1], generation, self.gateway, self.backend)
        controller = ReviewController(
            facade, self.agents, target.config, requests=lambda: [], bundle=target.bundle, controller_id=SERVICE
        )
        controller.issue = facade.repo(f"issues/{PR}")
        controller.load([])
        return controller


class PolicyObservationTests(unittest.TestCase):
    def test_review_upstreams_are_observed_even_when_they_supply_no_issues(self):
        worker = Worker.__new__(Worker)
        target = Target("up/repo", "fork/repo", "fork")
        worker.targets, worker.review_targets, worker.sponsorship = {"fork/repo": target}, {"up/repo": target}, None
        seen = []
        worker.load = lambda repository, latest=False: seen.append((repository, latest))
        worker.observe_policies()
        self.assertEqual(seen, [("fork/repo", True), ("up/repo", True)])


class PermissionTests(unittest.TestCase):
    def test_reviews_add_checks_and_issue_writes_upstream_only(self):
        targets = (Target("up/repo", "fork/repo", "fork"),)
        plain, reviewing = permissions(targets), permissions(targets, reviews=True)
        self.assertEqual(reviewing["fork/repo"], plain["fork/repo"])
        self.assertEqual(reviewing["up/repo"]["read"], plain["up/repo"]["read"])
        self.assertEqual(
            reviewing["up/repo"]["write"], {**plain["up/repo"]["write"], "issues": "write", "checks": "write"}
        )
        self.assertNotIn("workflows", reviewing["up/repo"]["write"])

    def test_an_upstream_needs_one_executor_fork(self):
        with self.assertRaises(ValueError):
            review_targets((Target("up/repo", "a/repo", "fork"), Target("up/repo", "b/repo", "fork")))


if __name__ == "__main__":
    unittest.main()
