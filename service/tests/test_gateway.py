"""Every write is an occurrence fenced by ownership generation; unknown outcomes stop the task."""

import json
import threading
import unittest

from fixtures import KEY, ServiceFixture
from support import setUpModule, tearDownModule  # unittest module fixtures
from github import ATTENTION_LABEL, LINKED_BRANCH, NotFound, Transient
from service.errors import OwnershipLost, Unresolved
from service.gateway import LEASE_SECONDS, Disallowed, Rejected, allowed
from service.transport import Response

COMMENTS = f"repos/{KEY[0]}/issues/7/comments"
INTAKE = "concurro:stage:intake"


class FencingTests(ServiceFixture, unittest.TestCase):
    """Ownership changes and admission share one critical section per task."""

    def admitted(self):
        """Every occurrence recorded for the task, in any status."""
        return [r for status in ("in-flight", "done", "failed", "unknown") for r in self.registry.mutations(KEY, status)]

    def test_grant_waits_for_an_in_flight_write(self):
        """No handoff while a write is outstanding, even after the lease expired; then the old owner is fenced."""
        first = self.gateway.acquire(KEY, "old")
        sending, finish = threading.Event(), threading.Event()
        send = self.backend.send

        def slow(method, path, body):
            sending.set()
            finish.wait(5)
            return send(method, path, body)

        self.backend.send = slow
        writer = threading.Thread(target=self.gateway.mutate, args=(KEY, first, "POST", COMMENTS, {"body": "x"}))
        writer.start()
        self.addCleanup(writer.join, 5)
        self.addCleanup(finish.set)
        self.assertTrue(sending.wait(5))
        self.clock.now += LEASE_SECONDS + 1
        self.assertIsNone(self.gateway.acquire(KEY, "new"))
        finish.set()
        writer.join(5)
        self.backend.send = send
        second = self.gateway.acquire(KEY, "new")
        self.assertEqual(second, first + 1)
        count, writes = len(self.admitted()), len(self.backend.writes)
        for _ in range(2):
            with self.assertRaises(OwnershipLost):
                self.gateway.mutate(KEY, first, "POST", COMMENTS, {"body": "stale"})
        self.assertEqual((len(self.admitted()), len(self.backend.writes)), (count, writes))
        self.assertIsInstance(self.gateway.stop(KEY, first), OwnershipLost)
        self.assertIsNone(self.gateway.stop(KEY, second))

    def test_takeover_while_the_old_worker_survives(self):
        """A live but expired owner keeps reading; every write it attempts after takeover is rejected."""
        old = self.facade(self.gateway.acquire(KEY, "old"))
        old.sync_stage(7, "intake")
        self.clock.now += LEASE_SECONDS + 1
        new = self.facade(self.gateway.acquire(KEY, "new"))
        with self.assertRaises(OwnershipLost):
            old.sync_stage(7, "plan")
        self.assertEqual(old.repo("issues/7")["number"], 7)
        new.sync_stage(7, "plan")
        self.assertEqual(self.backend.labels(), ["concurro", "concurro:stage:plan"])
        self.assertFalse(self.gateway.release(KEY, old.generation, None))
        self.assertTrue(self.gateway.release(KEY, new.generation, None))

    def test_renewal_is_liveness_only(self):
        """A renewed lease defers takeover; a stale generation cannot renew."""
        generation = self.gateway.acquire(KEY, "worker")
        self.clock.now += LEASE_SECONDS - 1
        self.assertTrue(self.gateway.renew(KEY, generation))
        self.clock.now += LEASE_SECONDS - 1
        self.assertIsNone(self.gateway.acquire(KEY, "other"))
        self.clock.now += 2
        self.assertEqual(self.gateway.acquire(KEY, "other"), generation + 1)
        self.assertFalse(self.gateway.renew(KEY, generation))


class SerializationTests(ServiceFixture, unittest.TestCase):
    """A task's writes are sent one at a time, each admitted only after the previous one settled."""

    def hold_first(self, lost):
        """Hold the first write open, submit a second, and return the second's result once the first settles."""
        generation = self.gateway.acquire(KEY, "worker")
        sending, finish, result = threading.Event(), threading.Event(), {}
        send = self.backend.send

        def held(method, path, body):
            if not sending.is_set():
                sending.set()
                finish.wait(5)
                send(method, path, body)
                if lost:
                    raise TimeoutError("fixture lost response")
                return Response(True, 200, {}, b"{}")
            return send(method, path, body)

        def write(name, text):
            try:
                result[name] = self.gateway.mutate(KEY, generation, "POST", COMMENTS, {"body": text})
            except BaseException as error:  # noqa: B036  (the stop type is under test)
                result[name] = error

        self.backend.send = held
        first = threading.Thread(target=write, args=("first", "one"))
        second = threading.Thread(target=write, args=("second", "two"))
        for thread in (first, second):
            self.addCleanup(thread.join, 5)
        self.addCleanup(finish.set)
        first.start()
        self.assertTrue(sending.wait(5))
        second.start()
        second.join(0.2)
        self.assertTrue(second.is_alive(), "the second write did not wait")
        self.assertEqual(len(self.registry.in_flight(KEY)), 1)
        self.assertEqual(self.backend.writes, [])
        finish.set()
        first.join(5)
        second.join(5)
        return result["second"]

    def test_next_write_follows_a_settled_write(self):
        """After a successful first write, the second is admitted and sent next."""
        self.assertEqual(self.hold_first(lost=False)["body"], "two")
        self.assertEqual([c["body"] for c in self.fake.comments[-2:]], ["one", "two"])
        self.assertEqual([r.sequence for r in self.registry.mutations(KEY, "done")], [1, 2])

    def test_next_write_stops_after_an_unknown_write(self):
        """After an unknown first write, the second rechecks and stops without admission."""
        self.assertIs(type(self.hold_first(lost=True)), Unresolved)
        self.assertEqual(len(self.registry.unresolved(KEY)), 1)
        self.assertEqual(self.registry.mutations(KEY, "done"), [])
        self.assertEqual(self.backend.writes, [("POST", "issues/7/comments")])


class OccurrenceTests(ServiceFixture, unittest.TestCase):
    """Identical payloads are distinct occurrences; nothing is deduplicated or replayed."""

    def setUp(self):
        """One owned facade per test."""
        super().setUp()
        self.gh = self.facade(self.gateway.acquire(KEY, "worker"))

    def test_stage_labels_cycle(self):
        """intake → plan → intake through the engine's label sync sends every transition."""
        for stage in ("intake", "plan", "intake"):
            self.gh.sync_stage(7, stage)
        self.assertEqual(self.backend.labels(), ["concurro", INTAKE])
        adds = [r for r in self.registry.mutations(KEY, "done") if r.path.endswith("/issues/7/labels")]
        self.assertEqual(len(adds), 3)
        self.assertEqual(adds[0].digest, adds[2].digest)
        self.assertEqual(len({r.intent for r in adds}), 3)

    def test_attention_label_cycles(self):
        """Failure, recovery, failure: each change is sent and the label ends present."""
        for failed in (True, False, True):
            self.gh.sync_attention(7, failed)
        self.assertIn(ATTENTION_LABEL, self.backend.labels())
        writes = [w for w in self.backend.writes if w[1].startswith("issues/7/labels")]
        self.assertEqual([w[0] for w in writes], ["POST", "DELETE", "POST"])

    def test_writes_off_the_allow_list_are_refused_before_admission(self):
        """No reconstruction rule, another issue or repository, or a forced ref update."""
        root = f"repos/{KEY[0]}/"
        refused = [("PATCH", root + "issues/7", {"state": "closed"}),
                   ("POST", root + "issues/8/comments", {"body": "x"}),
                   ("POST", "repos/other/repo/issues/7/comments", {"body": "x"}),
                   ("PATCH", root + "git/refs/heads/concurro/7", {"sha": "a" * 40, "force": True}),
                   ("DELETE", root + "git/refs/heads/concurro/7", None),
                   ("POST", "graphql", {"query": "mutation { deleteIssue(input: {}) { clientMutationId } }",
                                        "variables": {}})]
        for method, path, body in refused:
            with self.subTest(method=method, path=path), self.assertRaises(Disallowed):
                self.gh.api(path, method, body)
        self.assertEqual((self.backend.writes, self.registry.mutations(KEY, "in-flight")), ([], []))
        self.assertTrue(allowed(KEY, "PATCH", root + "git/refs/heads/concurro/7", {"sha": "a" * 40, "force": False}))

    def test_issue_comment_edits_only_on_the_apps_marked_comments(self):
        """A human comment, an unmarked App comment, or none are refused; a Concurro request is edited."""
        bot = self.config["bot_login"]
        self.fake.comment("<!-- concurro:event:v1:" + "e" * 64 + " -->\n### ❓ Input needed", bot)
        request = self.fake.comments[-1]["id"]
        self.fake.comment("<!-- concurro:event:v1:" + "f" * 64 + " -->\nforged by a human", "writer")
        human = self.fake.comments[-1]["id"]
        self.fake.comment("An unmarked note", bot)
        unmarked = self.fake.comments[-1]["id"]
        root = f"repos/{KEY[0]}/issues/comments/"
        for refused in (human, unmarked):
            with self.subTest(comment=refused), self.assertRaises(Disallowed):
                self.gh.api(root + str(refused), "PATCH", {"body": "x"})
        self.assertEqual(self.backend.writes, [])
        self.gh.api(root + str(request), "PATCH", {"body": "<!-- concurro:event:v1:" + "e" * 64 + " -->\n✅ Answered"})
        self.assertEqual(self.backend.writes, [("PATCH", f"issues/comments/{request}")])

    def test_only_the_engine_linked_branch_mutation_for_this_issue(self):
        """The exact engine document for this task's issue; appended mutations, other issues or extra input are refused."""
        def body(query=LINKED_BRANCH, issue="issue-7", **extra):
            return {"query": query, "variables": {"input": {"issueId": issue, "name": "concurro/7", "oid": "a" * 40}, **extra}}

        appended = LINKED_BRANCH + "\nmutation { updateIssue(input: {id: \"issue-7\", title: \"x\"}) { clientMutationId } }"
        for refused in (body(appended), body(issue="issue-8"), body(clientMutationId="x"),
                        {**body(), "operationName": "x"}):
            with self.subTest(body=refused), self.assertRaises(Disallowed):
                self.gh.api("graphql", "POST", refused)
        self.assertEqual(self.backend.writes, [])
        self.gh.create_linked_branch("issue-7", "concurro/7", "a" * 40)
        self.assertEqual(self.backend.writes, [("POST", "graphql")])
        self.assertEqual(self.fake.refs, {"concurro/7": "a" * 40})

    def test_graphql_queries_are_reads(self):
        """A query document never reaches the gateway."""
        self.assertFalse(self.gh.branch_linked(7, "concurro/7"))
        self.assertEqual(self.backend.writes, [])


class SettlementTests(ServiceFixture, unittest.TestCase):
    """2xx is done, a rejection failed, anything ambiguous unknown."""

    def setUp(self):
        """One owned facade per test."""
        super().setUp()
        self.generation = self.gateway.acquire(KEY, "worker")
        self.gh = self.facade(self.generation)

    def comment(self):
        """Create one comment through the facade."""
        return self.gh.repo("issues/7/comments", "POST", {"body": "hello"})

    def test_ambiguous_results_stop_the_task(self):
        """5xx, 429 and lost responses are unknown: the task is unresolved and cannot be handed over."""
        for fault in ({"status": 500}, {"status": 429}, {"lost": True}):
            with self.subTest(fault=fault):
                self.backend.fail("POST", "issues/7/comments", **fault)
                with self.assertRaises(Unresolved):
                    self.comment()
                [record] = self.registry.unresolved(KEY)
                with self.assertRaises(Unresolved):
                    self.comment()
                self.assertEqual(len(self.registry.unresolved(KEY)), 1)
                self.assertTrue(self.gateway.release(KEY, self.generation, None))
                self.assertIsNone(self.gateway.acquire(KEY, "worker"))
                self.registry.resolve(record.intent, "not-applied", "no comment with this body", "operator")
                self.generation = self.gateway.acquire(KEY, "worker")
                self.gh = self.facade(self.generation)
        self.assertEqual(len(self.registry.mutations(KEY, "failed")), 3)

    def test_rejections_fail_normally(self):
        """A 422 or 404 did not apply: the engine handles it as an ordinary error and may continue."""
        self.backend.fail("POST", "issues/7/comments", status=422)
        with self.assertRaises(Rejected):
            self.comment()
        self.backend.fail("POST", "issues/7/comments", status=404)
        with self.assertRaises(NotFound):
            self.comment()
        self.assertEqual(self.registry.unresolved(KEY), [])
        self.assertEqual(self.comment()["body"], "hello")

    def test_requests_not_sent_or_refused_before_execution(self):
        """Not sent, 401 and a throttled 403 did not apply: `failed`, `Transient`, and nothing to resolve."""
        cases = [(lambda *_: Response(sent=False), "unavailable", None),
                 (None, "unavailable", {"status": 401}),
                 (None, "throttled", {"status": 403, "headers": {"retry-after": "7200"}}),
                 (None, "throttled", {"status": 403, "headers": {"x-ratelimit-remaining": "0",
                                                                  "x-ratelimit-reset": str(1_000_000 + 7200)}}),
                 (None, "throttled", {"status": 403, "message": "You have exceeded a secondary rate limit"})]
        send = self.backend.send
        for index, (replacement, code, fault) in enumerate(cases):
            with self.subTest(case=index):
                self.backend.send = replacement or send
                if fault:
                    self.backend.fail("POST", "issues/7/comments", **fault)
                with self.assertRaises(Transient) as raised:
                    self.comment()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retry_after, {2: 7200, 3: 7200, 4: 60}.get(index))
        self.backend.send = send
        self.assertEqual((len(self.registry.mutations(KEY, "failed")), self.registry.unresolved(KEY)), (5, []))
        self.assertEqual(self.backend.writes, [])

    def test_ordinary_quota_headers_are_not_throttling(self):
        """A permission 403 that carries the usual quota headers is a plain rejection."""
        self.backend.fail("POST", "issues/7/comments", status=403, message="Resource not accessible by integration",
                          headers={"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1000060"})
        with self.assertRaises(Rejected) as raised:
            self.comment()
        self.assertNotIsInstance(raised.exception, Transient)

    def test_rest_success_with_a_lost_body_is_done(self):
        """The status line shows the write applied; the engine gets `response_lost`, the task stays schedulable."""
        self.backend.fail("POST", "issues/7/comments", truncated=True)
        with self.assertRaises(Transient) as raised:
            self.comment()
        self.assertEqual(raised.exception.code, "response_lost")
        self.assertEqual((len(self.registry.mutations(KEY, "done")), self.registry.unresolved(KEY)), (1, []))
        self.assertEqual(self.fake.comments[-1]["body"], "hello")

    def test_graphql_settles_done_only_on_a_complete_result(self):
        """Errors of any type, a null result, or an unreadable body leave the linked branch unknown."""
        ref = {"linkedBranch": {"ref": {"name": "concurro/7", "target": {"oid": "a" * 40}}}}
        responses = [{"errors": [{"type": "INTERNAL"}], "data": {"createLinkedBranch": ref}},
                     {"errors": [{"type": "UNPROCESSABLE"}], "data": {"createLinkedBranch": None}},
                     {"errors": [{"type": "NOT_FOUND"}], "data": None},
                     {"data": {"createLinkedBranch": None}}, None]
        for index, payload in enumerate(responses):
            with self.subTest(case=index):
                body = None if payload is None else json.dumps(payload).encode()
                self.backend.send = lambda *_: Response(True, 200, {}, body, None if body else "incomplete")
                with self.assertRaises(Unresolved):
                    self.gh.create_linked_branch("issue-7", "concurro/7", "a" * 40)
                [record] = self.registry.unresolved(KEY)
                self.gateway.release(KEY, self.generation, None)
                self.registry.resolve(record.intent, "not-applied", "no linked branch", "operator")
                self.generation = self.gateway.acquire(KEY, "worker")
                self.gh = self.facade(self.generation)

    def test_resolution_makes_the_task_schedulable_without_resending(self):
        """An applied resolution settles the occurrence as done; nothing is sent again."""
        self.backend.fail("POST", "issues/7/comments", lost=True)
        with self.assertRaises(Unresolved):
            self.comment()
        self.gateway.release(KEY, self.generation, None)
        [record] = self.registry.unresolved(KEY)
        writes = list(self.backend.writes)
        self.registry.resolve(record.intent, "applied", "comment 11 by the controller", "operator")
        self.assertIsNotNone(self.gateway.acquire(KEY, "worker"))
        self.assertEqual(self.backend.writes, writes)
        self.assertEqual([r.intent for r in self.registry.mutations(KEY, "done")], [record.intent])


if __name__ == "__main__":
    unittest.main()
