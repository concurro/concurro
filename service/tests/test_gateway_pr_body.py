"""PR-body updates have an exact shape and retain task ownership checks."""

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from service.gateway import Disallowed
from test_pr_reviews import PullRequestWrites


class BodyWrites(PullRequestWrites):
    """A body patch cannot change any other PR property or target another task."""

    def test_exact_owned_body_update(self):
        self.gateway.mutate(KEY, self.generation, "PATCH", f"repos/{KEY[0]}/pulls/9", {"body": "Readable body"})
        self.assertEqual(self.fake.pr["body"], "Readable body")
        for body in ({"title": "changed"}, {"body": "x", "state": "closed"}, {"body": None}):
            with self.assertRaises(Disallowed):
                self.gateway.mutate(KEY, self.generation, "PATCH", f"repos/{KEY[0]}/pulls/9", body)

    def test_foreign_pr_body_is_refused(self):
        self.fake.pr["head"]["ref"] = "human-branch"
        with self.assertRaises(Disallowed):
            self.gateway.mutate(KEY, self.generation, "PATCH", f"repos/{KEY[0]}/pulls/9", {"body": "Changed"})
