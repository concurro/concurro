"""The reviewer account's stored grant, its settings and typed review-mutation outcomes."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import fixtures  # noqa: F401  (puts the service and engine on the module path)

import review_api
from github import Rejected
from service import user_grant
from service.identity import IdentityError
from service.settlement import settle
from service.transport import Response

ACTOR = {"login": "ConcurroBot", "id": 4242, "node": "U_reviewer"}


class Connection:
    """One scripted HTTPS exchange per request, recording what was sent."""

    def __init__(self, script):
        self.script, self.sent = script, []

    def __call__(self, host, timeout):
        return self

    def connect(self):
        pass

    def request(self, method, target, body=None, headers=None):
        self.sent.append((method, target, body.decode() if body else None))
        status, payload = self.script.pop(0)
        data = json.dumps(payload).encode()
        self.reply = SimpleNamespace(
            status=status,
            getheaders=lambda: [("Content-Length", str(len(data)))],
            getheader=lambda name, default=None: str(len(data)) if name.lower() == "content-length" else default,
            read=lambda size, box=[data]: (
                (box.__setitem__(0, box[0][size:]) or b"") if not box[0] else self.take(box, size)
            ),
        )

    @staticmethod
    def take(box, size):
        chunk, box[0] = box[0][:size], box[0][size:]
        return chunk

    def getresponse(self):
        return self.reply

    def close(self):
        pass


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        secret = self.root / "client-secret"
        secret.write_text("shh\n")
        self.reviewer = SimpleNamespace(user_id=4242, client_id="Iv1.client", client_secret_path=secret)
        self.now = 1000.0

    def store(self, **fields):
        grant = {"token": "t1", "expires": 5000, "refresh_token": "r1", "refresh_expires": 90000, "actor": ACTOR}
        user_grant.save(self.root / user_grant.GRANT, {**grant, **fields})

    def grant(self, script=()):
        self.connection = Connection(list(script))
        return user_grant.UserGrant(self.reviewer, self.root, self.connection, clock=lambda: self.now)

    def test_a_current_token_is_used_without_any_request(self):
        self.store()
        grant = self.grant()
        self.assertEqual((grant.get(), grant.actor.id, self.connection.sent), ("t1", 4242, []))
        self.assertEqual((self.root / user_grant.GRANT).stat().st_mode & 0o777, 0o600)

    def test_refresh_saves_the_replacement_before_returning_it(self):
        self.store()
        refreshed = {
            "access_token": "t2",
            "expires_in": 28800,
            "refresh_token": "r2",
            "refresh_token_expires_in": 100000,
        }
        grant = self.grant([(200, refreshed)])
        self.now = 4900.0
        self.assertEqual(grant.get(), "t2")
        [(method, target, body)] = self.connection.sent
        fields = parse_qs(body)
        self.assertEqual((method, target), ("POST", "/login/oauth/access_token"))
        self.assertEqual(
            (fields["grant_type"], fields["refresh_token"], fields["client_secret"]),
            (["refresh_token"], ["r1"], ["shh"]),
        )
        stored = json.loads((self.root / user_grant.GRANT).read_text())
        self.assertEqual((stored["token"], stored["refresh_token"], stored["expires"]), ("t2", "r2", 4900.0 + 28800))
        self.assertEqual(stored["actor"], ACTOR)
        self.assertEqual(grant.get(), "t2")
        self.assertEqual(len(self.connection.sent), 1)

    def test_a_non_expiring_token_is_never_refreshed_or_given_an_expiry(self):
        self.store(expires=None, refresh_token=None, refresh_expires=None)
        grant = self.grant()
        self.now = 10**9
        self.assertEqual(grant.get(), "t1")
        grant.drop("t1")
        self.assertEqual((grant.get(), self.connection.sent), ("t1", []))

    def test_a_refused_token_refreshes_on_its_next_use(self):
        self.store()
        refreshed = {"access_token": "t2", "expires_in": 100, "refresh_token": "r2", "refresh_token_expires_in": 1000}
        grant = self.grant([(200, refreshed)])
        grant.drop("stale")
        self.assertEqual(grant.get(), "t1")
        grant.drop("t1")
        self.assertEqual(grant.get(), "t2")

    def test_a_failed_refresh_requires_reauthorization_and_keeps_the_stored_grant(self):
        self.store()
        grant = self.grant([(500, {"error": "server"})])
        self.now = 4900.0
        with self.assertRaisesRegex(IdentityError, "could not be refreshed"):
            grant.get()
        self.assertEqual(json.loads((self.root / user_grant.GRANT).read_text())["token"], "t1")

    def test_a_new_authorization_replaces_an_expired_token_without_refreshing(self):
        self.store()
        grant = self.grant()
        self.store(token="t9", expires=99999)
        self.now = 4900.0
        self.assertEqual((grant.get(), self.connection.sent), ("t9", []))

    def test_a_grant_for_another_account_or_readable_by_others_is_refused(self):
        self.store(actor={**ACTOR, "id": 7})
        with self.assertRaisesRegex(IdentityError, "no reviewer grant"):
            self.grant()
        self.store()
        (self.root / user_grant.GRANT).chmod(0o644)
        with self.assertRaisesRegex(IdentityError, "no reviewer grant"):
            self.grant()

    def authorize(self, user):
        script = [
            (
                200,
                {
                    "device_code": "dc",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 0,
                },
            ),
            (200, {"error": "authorization_pending"}),
            (
                200,
                {"access_token": "t1", "expires_in": 28800, "refresh_token": "r1", "refresh_token_expires_in": 100000},
            ),
            (200, user),
            (200, {"data": {"viewer": {"login": user["login"], "id": "U_graphql"}}}),
        ]
        shown = []
        self.connection = Connection(script)
        actor = user_grant.authorize(
            self.reviewer,
            self.root,
            self.connection,
            clock=lambda: self.now,
            sleep=lambda seconds: None,
            show=shown.append,
        )
        return actor, shown

    def test_device_authorization_binds_the_configured_numeric_account(self):
        actor, shown = self.authorize({"id": 4242, "login": "RenamedBot", "node_id": "legacy"})
        self.assertEqual((actor.login, actor.id, actor.node), ("RenamedBot", 4242, "U_graphql"))
        self.assertIn("ABCD-1234", shown[0])
        self.assertNotIn("client_secret", self.connection.sent[0][2])
        stored = json.loads((self.root / user_grant.GRANT).read_text())
        self.assertEqual((stored["token"], stored["expires"], stored["actor"]["id"]), ("t1", 1000.0 + 28800, 4242))

    def test_authorization_by_another_account_stores_nothing(self):
        with self.assertRaisesRegex(IdentityError, "not the configured reviewer"):
            self.authorize({"id": 1, "login": "someone", "node_id": "x"})
        self.assertFalse((self.root / user_grant.GRANT).exists())


class OutcomeTests(unittest.TestCase):
    """A review mutation's response settles as applied, rejected or unknown."""

    BODY = review_api.request(
        "addPullRequestReview", {"pullRequestId": "PR_9", "commitOID": "a" * 40, "body": "<!-- marker -->"}
    )

    def settle(self, payload):
        return settle("graphql", Response(True, 200, {}, json.dumps(payload).encode()), 0, self.BODY)

    def test_a_refusal_of_the_mutation_itself_is_a_definite_rejection(self):
        errors = [
            {
                "type": "UNPROCESSABLE",
                "path": ["addPullRequestReview"],
                "message": "commit not part of the pull request",
            }
        ]
        status, payload, error = self.settle({"data": {"addPullRequestReview": None}, "errors": errors})
        self.assertEqual((status, payload), ("failed", None))
        self.assertIsInstance(error, Rejected)
        self.assertNotIn("commit not part", str(error))

    def test_refusals_observed_on_github_are_definite(self):
        """Responses recorded from live probes on 2026-11-15, by mutation."""
        observed = {
            "addPullRequestReview": {
                "type": "VALIDATION",
                "locations": [{"line": 1, "column": 47}],
                "message": "The commitOID is not part of the pull request",
            },
            "addPullRequestReviewThread": {
                "type": "FORBIDDEN",
                "extensions": {"saml_failure": False, "ip_allow_list_failure": False},
                "message": "Resource not accessible by integration",
            },
            "submitPullRequestReview": {"type": "UNPROCESSABLE", "message": "Could not comment pull request review."},
            "deletePullRequestReview": {"type": "NOT_FOUND", "message": "Could not resolve to a node."},
            "resolveReviewThread": {"type": "FORBIDDEN", "message": "Resource not accessible by integration"},
        }
        for name, error in observed.items():
            with self.subTest(mutation=name):
                payload = {"data": {name: None}, "errors": [{**error, "path": [name]}]}
                self.assertTrue(review_api.rejected(name, payload))
        commit = {
            "data": {"addPullRequestReview": None},
            "errors": [{**observed["addPullRequestReview"], "path": ["addPullRequestReview"]}],
        }
        status, _, error = self.settle(commit)
        self.assertEqual(status, "failed")
        self.assertIsInstance(error, Rejected)

    def test_any_other_error_bearing_response_stays_unknown(self):
        node = {"id": "R1", "state": "PENDING"}
        for payload in (
            {
                "data": {"addPullRequestReview": None},
                "errors": [{"type": "INTERNAL", "path": ["addPullRequestReview"]}],
            },
            {"data": {"addPullRequestReview": None}, "errors": [{"type": "UNPROCESSABLE", "path": ["other"]}]},
            {
                "data": {"addPullRequestReview": {"pullRequestReview": node}},
                "errors": [{"type": "UNPROCESSABLE", "path": ["addPullRequestReview"]}],
            },
            {"errors": [{"type": "UNPROCESSABLE", "path": ["addPullRequestReview"]}]},
            {"data": {"addPullRequestReview": None}, "errors": []},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.settle(payload), ("unknown", None, None))

    def test_a_refused_thread_resolution_is_definite_and_other_errors_stay_unknown(self):
        from github import RESOLVE_THREAD

        body = {"query": RESOLVE_THREAD, "variables": {"input": {"threadId": "T1"}}}

        def resolve(payload):
            return settle("graphql", Response(True, 200, {}, json.dumps(payload).encode()), 0, body)

        refusal = {"type": "FORBIDDEN", "path": ["resolveReviewThread"], "message": "Resource not accessible"}
        status, _, error = resolve({"data": {"resolveReviewThread": None}, "errors": [refusal]})
        self.assertEqual((status, error.reason), ("failed", "permission"))
        self.assertIsInstance(error, Rejected)
        unknown = {
            "data": {"resolveReviewThread": None},
            "errors": [{"type": "INTERNAL", "path": ["resolveReviewThread"]}],
        }
        self.assertEqual(resolve(unknown), ("unknown", None, None))

    def test_a_complete_result_is_applied(self):
        node = {"id": "R1", "state": "PENDING"}
        status, _, error = self.settle({"data": {"addPullRequestReview": {"pullRequestReview": node}}})
        self.assertEqual((status, error), ("done", None))


if __name__ == "__main__":
    unittest.main()
