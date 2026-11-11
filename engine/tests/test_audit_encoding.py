"""Compact audit encoding, paginated provenance and durable display recovery."""

import json
import re
import unittest
from copy import deepcopy
from unittest.mock import patch

from markdown_it import MarkdownIt

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from checkpoints import (
    MARKER,
    PLAN_NAME,
    REF_MARKER,
    STATE_PREFIX,
    CheckpointError,
    Checkpoints,
    legacy_state,
)
from controller import Controller
from fakes import CONFIG, PLAN, FakeAgents, FakeGitHub
from policy import digest


class AuditEncodingTests(unittest.TestCase):
    """Use production checkpoint writes with an issue-local fake GitHub."""

    def setUp(self):
        """Start with no dashboard and independent artifact storage."""
        self.gh = FakeGitHub(CONFIG)
        self.gh.comments = []
        self.cp = Checkpoints(self.gh, CONFIG, 7)
        self.state = {"version": 2, "issue": 7, "stage": "stopped", "calls": 3}

    def comment(self, cp=None, text="Current state"):
        """Render a small dashboard with its genuine recovery pointer."""
        return MARKER + "\n" + text + "\n" + (cp or self.cp).links()[1] + "\n"

    def load(self):
        """Reload without sharing trusted process memory."""
        cp = Checkpoints(self.gh, CONFIG, 7)
        state = cp.load(self.gh.comments)
        return cp, state

    def window(self):
        """Drop old versions as the live endpoint does, preserving monotonic revision numbers."""
        for item in self.gh.artifacts[7].values():
            item["versions"][:] = item["versions"][:100]

    def test_comment_payload_round_trip_does_not_render_or_unescape_literal_sequences(self):
        """Every comment terminator is inert and JSON alone decodes literal escape text."""
        content = '<!--> <!---> --> --!> </script> & "quote" 😀 literal \\u003e \\u003c'
        self.state["hostile"] = content
        self.cp.save(self.state)
        body = self.cp.audit["body"]
        self.assertIn("<!-- concurro:audit-data:v1\n", body)
        self.assertNotIn(STATE_PREFIX, body)
        self.assertEqual(legacy_state(body)["state"]["hostile"], content)
        html = MarkdownIt("commonmark", {"html": True}).render(body)
        # Raw HTML comments survive the renderer but contain no closing delimiter in data.
        without_comments = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        self.assertNotIn("literal", without_comments)
        self.assertEqual(self.load()[1], self.state)

    def test_latest_only_read_and_history_on_demand(self):
        """An old pointer fetches history, while a current pointer only needs one version."""
        self.cp.save(self.state)
        self.gh.comment(self.comment(), CONFIG["bot_login"])
        for index in range(35):
            self.cp.save(self.state | {"calls": index + 4})
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as request:
            self.assertEqual(self.load()[1]["calls"], 38)
        reads = [c.args[0] for c in request.call_args_list if "?" in c.args[0]]
        self.assertIn("issues/7/artifacts/1?per_page=1&page=1", reads)
        self.assertTrue(any("per_page=30" in p for p in reads))
        self.gh.summary()["body"] = self.comment()
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as request:
            self.load()
        self.assertFalse(any("per_page=30" in c.args[0] for c in request.call_args_list))

    def test_confirmed_pointer_survives_more_than_one_hundred_audit_saves(self):
        """Witness metadata preserves validation without changing task-state authority."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        for index in range(120):
            self.cp.save(self.state | {"calls": index + 4})
            self.window()
        cp, state = self.load()
        self.assertEqual(state["calls"], 123)
        self.assertEqual(cp.dashboard["confirmed"]["pointer"]["revision"], 1)
        self.assertFalse(cp.dashboard_dirty(self.comment(cp)))
        self.assertTrue(cp.dashboard_dirty(self.comment(cp, "A changed display")))

    def test_uncertain_pointer_survives_failed_readback_and_old_pointer_observation(self):
        """Both possible pointers remain accepted until the new write is confirmed."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        old = deepcopy(self.gh.summary())
        self.cp.save(self.state | {"calls": 4})
        pending = self.comment(text="Next state")
        original = self.gh.repo

        def failed(path, method="GET", data=None):
            """Lose the comment PATCH and its readback without affecting artifact writes."""
            if path.startswith("issues/comments/"):
                raise RuntimeError("unavailable")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=failed), self.assertRaises(CheckpointError):
            self.cp.publish_dashboard(pending)
        for index in range(120):
            self.cp.save(self.state | {"calls": index + 5})
            self.window()
        cp, state = self.load()
        self.assertEqual(self.gh.summary(), old)
        self.assertIn("pending", cp.dashboard)
        self.gh.summary()["body"] = pending  # The original timed-out PATCH lands later.
        recovered, actual = self.load()
        self.assertEqual(actual, state)
        self.assertNotIn("pending", recovered.dashboard)
        self.assertEqual(recovered.dashboard["confirmed"]["pointer"], cp.dashboard["pending"]["pointer"])

    def test_crash_before_receipt_save_reconciles_latest_revision(self):
        """The last durable audit supplies the candidate when the in-memory receipt is lost."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        self.cp.save(self.state | {"calls": 4})
        pointer = self.cp.reference()
        cp, state = self.load()  # Simulate a crash with a possibly unacknowledged dashboard PATCH.
        cp.save(state)
        self.assertEqual(cp.dashboard["pending"]["pointer"], pointer)
        for index in range(110):
            cp.save(state | {"calls": index + 5})
            self.window()
        self.gh.summary()["body"] = MARKER + "\nLate write\n" + REF_MARKER + json.dumps(pointer) + " -->\n"
        self.assertEqual(self.load()[1]["calls"], 114)

    def test_forged_pointer_cannot_use_visible_content_or_witness(self):
        """Display equality is not authority to accept an unrelated checkpoint digest."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        self.cp.save(self.state)
        self.gh.summary()["body"] = self.gh.summary()["body"].replace(
            self.cp.dashboard["confirmed"]["pointer"]["digest"], "0" * 64
        )
        with self.assertRaises(CheckpointError) as caught:
            self.load()
        self.assertEqual(caught.exception.code, "audit_history")

    def test_idle_controller_does_not_rewrite_dashboard_for_witness_only_save(self):
        """Receipt persistence cannot cause a revision-driven save loop."""
        agents = FakeAgents()
        c = Controller(self.gh, agents, CONFIG)
        c.advance(7)
        c.state["stage"] = "stopped"
        c.save()
        c.checkpoint()  # Persist the confirmation without publishing a new dashboard.
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as request:
            for _ in range(3):
                Controller(self.gh, agents, CONFIG).advance(7)
        self.assertFalse([x for x in request.call_args_list if len(x.args) > 1 and x.args[1] in ("PATCH", "POST")])

    def test_unbound_plan_without_origin_is_preserved_and_replaced(self):
        """A new view comes only from task state and retains approvals and spent budgets."""
        self.state.update(plan=deepcopy(PLAN), plan_id=digest(PLAN), base="b" * 40)
        self.cp.save(self.state)
        old = self.cp.plan
        self.cp.plan_binding = None
        self.cp.audit = self.cp.write(self.cp.audit, "generic", self.cp.audit_body(self.state, bind_plan=False))
        stored = self.gh.artifacts[7][old["number"]]
        stored["versions"][0]["version"] = 101
        stored["versions"][:] = stored["versions"][:1]
        before = deepcopy(stored)
        cp, state = self.load()
        self.assertEqual(state, self.state)
        cp.save(state)
        self.assertEqual(self.gh.artifacts[7][old["number"]], before)
        self.assertNotEqual(cp.plan["id"], old["id"])
        self.assertEqual(self.load()[1], self.state)
        self.assertEqual(len([x for x in self.gh.artifacts[7].values() if x["artifact"]["name"] == PLAN_NAME]), 2)

    def test_history_read_detects_concurrent_audit_change(self):
        """History pages cannot silently mix two different current audit snapshots."""
        self.cp.save(self.state)
        self.gh.comment(self.comment(), CONFIG["bot_login"])
        self.cp.save(self.state | {"calls": 4})
        original = self.gh.repo

        def changed(path, method="GET", data=None):
            """Change the current artifact after the initial latest-only read."""
            if "per_page=30" in path:
                original("issues/7/artifacts/1", "PATCH", {"body": "changed"})
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=changed), self.assertRaises(CheckpointError):
            self.load()

    def test_dashboard_readback_failure_carries_receipt_without_extra_audit_write(self):
        """A landed write with failed readback remains recoverable after history expires."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        self.cp.save(self.state | {"calls": 4})
        pointer = self.cp.reference()
        original = self.gh.repo

        def fail_readback(path, method="GET", data=None):
            """Apply the PATCH but lose only the subsequent GET."""
            if path.startswith("issues/comments/") and method == "GET":
                raise RuntimeError("read unavailable")
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=fail_readback) as requests:
            with self.assertRaises(CheckpointError):
                self.cp.publish_dashboard(self.comment(text="New progress"))
        self.assertFalse(any("/artifacts" in c.args[0] for c in requests.call_args_list))
        self.assertEqual(self.cp.dashboard["pending"]["pointer"], pointer)
        for index in range(110):
            self.cp.save(self.state | {"calls": index + 5})
            self.window()
        cp, state = self.load()
        self.assertNotIn("pending", cp.dashboard)
        self.assertEqual(cp.dashboard["confirmed"]["pointer"], pointer)
        cp.save(state)  # Persist promotion; its predecessor can now be retired.
        again, _ = self.load()
        self.assertEqual(again.dashboard["confirmed"]["pointer"], pointer)

    def test_pending_pointer_is_reconciled_before_a_third_pointer_is_published(self):
        """New display content cannot introduce a third candidate during uncertain recovery."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        self.cp.save(self.state | {"calls": 4})
        pending = self.cp.reference()
        with patch.object(self.cp, "request", side_effect=CheckpointError("artifact_request")):
            with self.assertRaises(CheckpointError):
                self.cp.publish_dashboard(self.comment(text="Second display"))
        self.cp.save(self.state | {"calls": 5})
        cp, _ = self.load()
        self.assertNotEqual(cp.reference(), pending)
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as requests:
            cp.publish_dashboard(self.comment(cp, "Newest display"))
        writes = [c for c in requests.call_args_list if len(c.args) > 1 and c.args[1] == "PATCH"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(cp.comment_pointer(writes[0].args[2]["body"]), pending)
        self.assertNotIn("pending", cp.dashboard)
        self.assertEqual(cp.dashboard["confirmed"]["pointer"], pending)

    def test_config_and_plan_links_affect_display_freshness(self):
        """Visible links change freshness even when the authoritative task state is identical."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment(text="[Plan](https://example.com/old) · team A"))
        self.cp.save(self.state)
        cp, state = self.load()
        self.assertEqual(state, self.state)
        for content in ("[Plan](https://example.com/new) · team A", "[Plan](https://example.com/old) · team B"):
            self.assertTrue(cp.dashboard_dirty(self.comment(cp, content)))
        self.assertFalse(cp.dashboard_dirty(self.comment(cp, "[Plan](https://example.com/old) · team A")))

    def test_replacement_recovers_creation_and_binding_lost_replies(self):
        """Persist intent first, then recover exactly one new view without touching the original."""
        for failure in ("creation", "binding"):
            with self.subTest(failure=failure):
                self.setUp()
                self.state.update(plan=deepcopy(PLAN), plan_id=digest(PLAN), base="b" * 40)
                self.cp.save(self.state)
                old = self.cp.plan
                self.cp.plan_binding = None
                self.cp.audit = self.cp.write(self.cp.audit, "generic", self.cp.audit_body(self.state, bind_plan=False))
                stored = self.gh.artifacts[7][old["number"]]
                stored["versions"][0]["version"] = 101
                before = deepcopy(stored)
                cp, state = self.load()
                original = self.gh.repo

                def lose_reply(path, method="GET", data=None):
                    """Lose the reply after applying the chosen write, checking the prior intent."""
                    create = method == "POST" and (data or {}).get("type") == "plan"
                    bind = method == "PATCH" and "/artifacts/" in path and '"plan_artifact"' in data.get("body", "")
                    if create:
                        payload = legacy_state(self.gh.artifacts[7][1]["artifact"]["body"])
                        self.assertEqual(payload["plan_replacement"]["preserved"], [cp.plan_identity(old)])
                        self.assertEqual(payload["plan_replacement"]["digest"], digest(data["body"]))
                    result = original(path, method, data)
                    if create and failure == "creation" or bind and failure == "binding":
                        raise RuntimeError("lost write reply")
                    return result

                with patch.object(self.gh, "repo", side_effect=lose_reply), self.assertRaises(CheckpointError):
                    cp.save(state)
                recovered, actual = self.load()
                self.assertEqual(actual, self.state)
                recovered.save(actual)
                self.assertEqual(self.gh.artifacts[7][old["number"]], before)
                self.assertEqual(len([v for v in self.gh.artifacts[7].values() if v["artifact"]["type"] == "plan"]), 2)
                self.assertNotEqual(recovered.plan_binding["id"], old["id"])
                for _ in range(3):
                    recovered, actual = self.load()
                    recovered.save(actual)
                self.assertEqual(len([v for v in self.gh.artifacts[7].values() if v["artifact"]["type"] == "plan"]), 2)

    def test_missing_origin_without_a_task_plan_creates_no_view(self):
        """Unavailable provenance is not an instruction to invent a plan."""
        self.cp.save(self.state)
        old = self.gh.repo("issues/7/artifacts", "POST", {"type": "plan", "name": PLAN_NAME, "body": "Old content"})
        self.gh.artifacts[7][old["number"]]["versions"][0]["version"] = 101
        before = deepcopy(self.gh.artifacts[7][old["number"]])
        cp, state = self.load()
        cp.save(state)
        self.assertEqual(len(self.gh.artifacts[7]), 2)
        self.assertEqual(self.gh.artifacts[7][old["number"]], before)
        self.assertIsNone(cp.plan)

    def test_bad_comment_encoding_and_cross_artifact_witnesses_fail(self):
        """Malformed machine data and receipts never supply recovery authority."""
        self.cp.save(self.state)
        body = self.cp.audit["body"]
        for malformed in (
            body.replace("\n-->\n", ""),
            body.replace('"stopped"', '"-->"'),
            body.replace('"version": 7', '"version": 6'),
        ):
            with self.subTest(body=malformed), self.assertRaises(CheckpointError):
                self.cp.decode({"body": malformed})
        pointer = self.cp.reference()
        for change in ({"id": 999}, {"number": 999}, {"revision": 999}, {"digest": "invalid"}):
            payload = legacy_state(body)
            payload["dashboard"] = {"confirmed": {"pointer": pointer | change, "display": "0" * 64}}
            with self.subTest(change=change), self.assertRaises(CheckpointError):
                self.cp.decode({"body": self.cp.render_audit(payload)})

    def test_overflow_envelope_retains_dashboard_receipts(self):
        """Carried witnesses survive immutable envelope storage as well as inline audits."""
        self.cp.save(self.state)
        self.cp.publish_dashboard(self.comment())
        state = self.state | {"large_metadata": "x" * 70000}
        self.cp.save(state)
        self.assertIn("audit_data", legacy_state(self.cp.audit["body"]))
        cp, recovered = self.load()
        self.assertEqual(recovered, state)
        self.assertEqual(cp.dashboard["confirmed"], self.cp.dashboard["confirmed"])

    def test_unverifiable_plan_without_audit_cannot_start_new_work(self):
        """Replacement requires authoritative task state; a missing audit is not a fresh issue."""
        old = self.gh.repo("issues/7/artifacts", "POST", {"type": "plan", "name": PLAN_NAME, "body": "Old content"})
        self.gh.artifacts[7][old["number"]]["versions"][0]["version"] = 101
        with self.assertRaises(CheckpointError) as caught:
            self.load()
        self.assertEqual(caught.exception.code, "audit_absent")
        self.assertEqual(len(self.gh.artifacts[7]), 1)

    def test_dashboard_metadata_requires_format_seven(self):
        """Fenced legacy payloads cannot opt into recovery semantics unknown to their readers."""
        self.cp.save(self.state)
        payload = legacy_state(self.cp.audit["body"])
        payload["state"]["version"] = 3
        payload["dashboard"] = {"confirmed": {"pointer": self.cp.reference(), "display": "0" * 64}}
        with self.assertRaises(CheckpointError) as caught:
            self.cp.decode({"body": self.cp.render_audit(payload, legacy=True)})
        self.assertEqual(caught.exception.code, "checkpoint_format")
