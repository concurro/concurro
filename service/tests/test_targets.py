"""Target selection and permission unions do not confuse issue and publication repositories."""

import unittest
from types import SimpleNamespace

import fixtures  # noqa: F401
from service.targets import Target, enrollment, permissions
from service.identity import Identity, IdentityError
from test_identity import App
from fixtures import Clock


class TargetTests(unittest.TestCase):
    """Enrollment is explicit, and access follows each repository's configured roles."""

    def test_issue_sources_and_overlap(self):
        """Both sources have separate task keys; overlapping issue sources are refused."""
        target = Target("upstream/project", "fork/project", "both")
        self.assertEqual(enrollment([target]), {"upstream/project": target, "fork/project": target})
        with self.assertRaises(ValueError):
            enrollment([target, Target("upstream/project", "another/project", "upstream")])
        with self.assertRaises(ValueError):
            enrollment([target, Target("UPSTREAM/project", "another/project", "fork")])
        # Several forks may contribute to one upstream when their issues do not overlap.
        other = Target("upstream/project", "another/project", "fork")
        self.assertEqual(set(enrollment([Target(target.upstream, target.fork, "fork"), other])),
                         {"fork/project", "another/project"})

    def test_permissions_follow_roles_and_union(self):
        """Only forks get Actions write; issue writes follow enrollment and overlaps union permissions."""
        target = Target("upstream/project", "fork/project", "fork")
        found = permissions([target])
        self.assertEqual(found[target.upstream]["write"], {"pull_requests": "write", "contents": "write"})
        self.assertEqual(found[target.fork]["write"], {"actions": "write", "contents": "write", "issues": "write"})
        self.assertEqual(found[target.upstream]["read"]["statuses"], "read")
        self.assertNotIn("administration", found[target.upstream]["read"])
        both = permissions([Target(target.upstream, target.fork, "both")])
        self.assertEqual(both[target.upstream]["write"]["issues"], "write")
        union = permissions([target, Target("fork/project", "third/project", "upstream")])
        self.assertEqual(union["fork/project"]["write"],
                         {"actions": "write", "contents": "write", "issues": "write", "pull_requests": "write"})

    def test_public_fork_network_is_required(self):
        from policy import Blocked
        from service.targets import verify
        target = Target("upstream/project", "fork/project", "fork")
        records = {target.upstream: {"full_name": target.upstream, "private": False},
                   target.fork: {"full_name": target.fork, "private": False, "fork": True,
                                 "source": {"full_name": target.upstream}}}
        reader = lambda repository: SimpleNamespace(request=lambda *args: records[repository])
        verify([target], reader)
        records[target.fork]["private"] = True
        with self.assertRaises(Blocked):
            verify([target], reader)
        records[target.fork]["private"] = False
        records[target.fork]["source"]["full_name"] = "unrelated/project"
        with self.assertRaises(Blocked):
            verify([target], reader)

    def test_identity_mints_the_repositorys_permissions(self):
        """Token minting consumes the union and never mints for an unconfigured destination."""
        clock = Clock()
        app = App(clock)
        settings = SimpleNamespace(app_id=123, private_key_path=None,
                                   targets=(Target("owner/upstream", "owner/fork", "fork"),))
        identity = Identity(settings, app.connection, clock)
        identity.app_jwt = lambda: "test-jwt"
        identity.mint("owner/upstream", "write")
        identity.mint("owner/fork", "write")
        self.assertNotIn("actions", app.minted[0][2])
        self.assertEqual(app.minted[1][2]["actions"], "write")
        before = len(app.minted)
        with self.assertRaises(IdentityError):
            identity.mint("owner/foreign", "write")
        self.assertEqual(len(app.minted), before)


if __name__ == "__main__":
    unittest.main()
