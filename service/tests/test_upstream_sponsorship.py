"""Issue-source and precharge permission checks use the configured upstream after integration."""

import json
import unittest
from copy import deepcopy

from fixtures import KEY

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import test_shadow as fixture
import test_sponsored_admissions as admission_fixture
import test_transport as http_fixture
from policy import CredentialsUnavailable
from service.actions import Actions
from service.targets import Target
from service.transport import Transport


class UpstreamConsentTests(unittest.TestCase):
    """Exercise real source confirmation, provider fences and admission selection on fork issues."""

    def setUp(self):
        """Reuse the admitted-task fixture without inheriting or rerunning its test methods."""
        self.case = admission_fixture.AdmissionTests(
            "test_batch_preflight_has_no_charge_when_any_required_owner_lacks_coverage"
        )
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.denied, self.permissions = set(), []
        original = self.case.request

        def request(method, path, data=None):
            """Distinguish current upstream denial from fork writer authority on the real transport seam."""
            if "/collaborators/" in path:
                self.permissions.append(path)
                login = path.split("/collaborators/")[1].split("/")[0]
                if path.startswith("repos/upstream/project/") and login in self.denied:
                    return {"permission": "read"}
            return original(method, path, data)

        self.case.request = request
        self.case.config["upstream"] = "upstream/project"

    def test_comment_confirmation_rejects_new_fork_only_sponsor_but_accepts_withdrawal(self):
        """Human source comments cannot bypass upstream policy, including before a task exists."""
        self.denied.add("alice")
        comments = [
            fixture.comment(2, "/concurro sponsor openai", "alice"),
            fixture.comment(3, "/concurro withdraw anthropic", "alice"),
        ]
        projection = self.case.shadow.confirm(KEY, comments, [], self.case.config["bot_login"], config=self.case.config)
        self.assertEqual([(f["command"], f["actor_id"]) for f in projection["facts"]], [("withdraw", "202")])
        self.assertIn("repos/upstream/project/collaborators/alice/permission", self.permissions)
        self.assertFalse(any(p.startswith("repos/" + KEY[0] + "/collaborators/") for p in self.permissions))
        self.assertEqual(self.case.agents.calls, [])
        self.assertNotIn("task", projection)

    def test_lost_upstream_authority_fences_only_own_providers_before_charge(self):
        """Existing history remains, while a fork writer cannot fund a new admitted attempt."""
        state = self.case.sponsors()
        before = deepcopy(state["sponsorships"])
        self.denied.add("alice")
        with self.case.current() as controller:
            controller.config["upstream"] = "upstream/project"
            calls = controller.state["calls"]
            with self.assertRaises(CredentialsUnavailable):
                controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(controller.state["calls"], calls)
            self.assertEqual(controller.state["sponsorships"]["anthropic"], before["anthropic"])
            self.assertNotEqual(controller.state["sponsorships"]["openai"]["status"], "active")
            self.assertEqual(controller.state["sponsorship_history"][-1]["actor"], "202")
            self.assertTrue(controller.state["sponsorship_history"][-1]["cutoff"])
        self.assertIn("repos/upstream/project/collaborators/alice/permission", self.permissions)

    def test_delegated_control_uses_upstream_and_withdraw_has_no_grant_gate(self):
        """The actual private action entrypoint signs the caller, not a supplied actor or fork writer."""
        self.denied.add("alice")
        target = Target("upstream/project", KEY[0], "fork")
        actions = Actions(
            fixture.KEY_BYTES,
            self.case.registry,
            lambda repo: self.case,
            lambda repo: self.case.backend,
            [target],
            user=lambda token: ("alice", 202),
            visible=lambda *args: True,
            clock=self.case.clock,
        )
        sponsor = json.dumps({"repository": KEY[0], "issue": 7, "command": "sponsor", "argument": "openai"}).encode()
        self.assertEqual(actions.handle("Bearer fixture", sponsor)[0], 403)
        withdraw = json.dumps(
            {"repository": KEY[0], "issue": 7, "command": "withdraw", "argument": "openai", "actor_id": 303}
        ).encode()
        self.assertEqual(actions.handle("Bearer fixture", withdraw)[0], 201)
        raw = self.case.fake.comments[-1]
        effective = fixture.delegation.resolve(raw, fixture.KEY_BYTES, *KEY, self.case.config["bot_login"])
        self.assertEqual(effective["user"]["id"], 202)
        self.assertEqual(effective["body"], "/concurro withdraw openai")

    def test_real_transport_confirms_and_preflights_stable_upstream_identity(self):
        """Production path validation permits numeric profiles and preserves live precharge denial."""
        self.case.sponsors()
        net = http_fixture.Loopback().start(self)
        for login, actor in fixture.ACTORS.items():
            net.route("GET", f"user/{actor}", http_fixture.reply(200, {"id": actor, "login": login}))
            net.route(
                "GET",
                f"repos/upstream/project/collaborators/{login}/permission",
                http_fixture.reply(200, {"permission": "write", "user": {"id": actor}}),
            )
        net.route("GET", f"repos/{KEY[0]}/issues/{KEY[1]}/events?per_page=100&page=1", http_fixture.reply(200, []))
        credential = http_fixture.Credential("synthetic-reader-token")
        self.case.shadow.reader = lambda repo: Transport(repo, credential, self.case.clock, net.connect)
        projection = self.case.shadow.confirm(
            KEY, self.case.fake.comments, [], self.case.config["bot_login"], config=self.case.config
        )
        self.assertEqual(projection["slots"]["openai"]["sponsor_id"], "202")
        self.assertEqual(projection["slots"]["anthropic"]["sponsor_id"], "303")
        with self.case.current() as controller:
            calls = controller.state["calls"]
            selected = controller.select_admissions([("codex", "intake", {})])
            self.assertTrue(selected)
            self.assertEqual(controller.state["calls"], calls)
            net.route(
                "GET",
                "repos/upstream/project/collaborators/alice/permission",
                http_fixture.reply(200, {"permission": "read", "user": {"id": 202}}),
            )
            with self.assertRaises(CredentialsUnavailable):
                controller.select_admissions([("codex", "intake", {})])
            self.assertEqual(controller.state["calls"], calls)
        self.assertIn("/user/202", [row[1] for row in net.seen()])
        for row in net.seen():
            self.assertEqual(row[2]["Authorization"], "Bearer synthetic-reader-token")
