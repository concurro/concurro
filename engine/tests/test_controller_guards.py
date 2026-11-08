"""Characterize authorization reads, command consumption and save ordering before extraction."""

from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
from pipeline_fixtures import ConsensusFixture
from policy import Blocked, digest
from github import Transient
from fakes import APPROVE, named_config
import implementation_bundle


class GuardContractTests(ConsensusFixture, unittest.TestCase):
    """Use a real approved task; observe the guards without executing publication writes."""

    def setUp(self):
        """Reach a published head, then isolate the controller call under examination."""
        super().setUp()
        self.reach("review")
        self.command = {"id": 10000, "user": {"login": "writer"}, "body": "/concurro retry"}

    def prepared(self, kind):
        """Prepare only the precondition needed to reach each existing guard."""
        controller = self.loaded()
        if kind == "revision":
            controller.state.update(
                stage="escalated",
                retry_stage="plan",
                revision_wait={"stage": "plan"},
                plan_revisions=self.config["max_plan_revisions"],
            )
        return controller

    def invoke(self, controller, kind):
        """Exercise existing public paths, with the bundle proof separately controlled."""
        if kind == "revision":
            return controller.extend_plan_revisions(self.command, 1, [])
        if kind == "invalidate":
            return controller.invalidate_task(self.command, [])
        if kind == "bundle":
            with (
                patch("controller.implementation_bundle.current", return_value=False),
                patch(
                    "controller.implementation_bundle.original_skill",
                    return_value=controller.bundle.hashes[implementation_bundle.SKILL],
                ),
            ):
                return controller.retry_implementation_bundle(self.command, [])
        return controller.reopen_design(
            {"kind": "requirements", "explanation": "Changed requirements.", "evidence": ["Writer request."]},
            "requirements",
        )

    @contextmanager
    def trace(self, controller, files=None, read_error=None, authority_pr=None):
        """Record ordered reads and each save's consumed-command value."""
        events = []
        original_repo, original_pages = controller.pulls.repo, controller.pulls.pages

        def repo(path, method="GET", data=None):
            """Observe exact PR reads, including errors before any response is returned."""
            events.append(("repo", path, method, data))
            if read_error:
                raise read_error
            if (
                authority_pr is not None
                and path == "pulls/9"
                and sum(e[:2] == ("repo", "pulls/9") for e in events) == 2
            ):
                return deepcopy(authority_pr)
            return original_repo(path, method, data)

        def pages(path, key=None):
            """Supply file evidence without introducing another transport request."""
            events.append(("pages", path, key))
            return deepcopy(files) if files is not None else original_pages(path, key)

        def save(*args, **kwargs):
            """Capture consumption at the save boundary without unrelated artifact writes."""
            events.append(("save", controller.state.get("command_id"), args, kwargs))

        with (
            patch.object(controller.pulls, "repo", side_effect=repo),
            patch.object(controller.pulls, "pages", side_effect=pages),
            patch.object(controller, "save", side_effect=save),
        ):
            yield events

    def test_each_pr_guard_refuses_changed_closed_or_merged_pr(self):
        """Each strict guard reads once, consumes the command and preserves its save policy."""
        messages = {
            "revision": "PR changed outside the controller; cannot resume its plan revision.",
            "bundle": "PR changed outside the controller; cannot retry its implementation.",
            "invalidate": "PR changed outside the controller; cannot invalidate its recorded design",
        }
        original_pr = deepcopy(self.gh.pr)
        for kind, message in messages.items():
            for failure in ("head", "closed", "merged"):
                with self.subTest(kind=kind, failure=failure):
                    controller = self.prepared(kind)
                    before = controller.state["command_id"]
                    self.assertLess(before, self.command["id"])
                    self.gh.pr = deepcopy(original_pr)
                    if failure == "head":
                        self.gh.pr["head"]["sha"] = "f" * 40
                    elif failure == "closed":
                        self.gh.pr["state"] = "closed"
                    else:
                        self.gh.pr["merged"] = True
                    with self.trace(controller) as events, self.assertRaises(Blocked) as caught:
                        self.invoke(controller, kind)
                    self.assertIs(type(caught.exception), Blocked)
                    self.assertEqual(str(caught.exception), message)
                    self.assertEqual(controller.state["command_id"], self.command["id"])
                    expected = [("repo", "pulls/9", "GET", None)]
                    if kind != "invalidate":
                        expected.append(("save", self.command["id"], (), {}))
                    self.assertEqual(events, expected)

    def test_pending_and_configuration_refusals_happen_before_pr_reads(self):
        """Refusal consumption applies to the entire existing validation region."""
        messages = {
            "revision": (
                "Publication is pending; reconcile it before granting revision capacity.",
                "Configuration or requirements changed; reassess them before granting revision capacity.",
            ),
            "bundle": (
                "Publication is pending; finish the recorded publication before adopting instructions.",
                "Configuration, requirements or approval changed; cannot preserve the approved plan.",
            ),
            "invalidate": (
                "Publication is pending. Use /concurro retry to finish the recorded publication, "
                "then /concurro invalidate to reassess it.",
                "Controller configuration changed; start a new task with the new configuration.",
            ),
        }
        for kind, reasons in messages.items():
            for failure, message in zip(("pending", "config"), reasons, strict=True):
                with self.subTest(kind=kind, failure=failure):
                    controller = self.prepared(kind)
                    before = controller.state["command_id"]
                    controller.state["pending" if failure == "pending" else "config_digest"] = "changed"
                    with self.trace(controller) as events, self.assertRaises(Blocked) as caught:
                        self.invoke(controller, kind)
                    self.assertIs(type(caught.exception), Blocked)
                    self.assertEqual(str(caught.exception), message)
                    self.assertGreater(controller.state["command_id"], before)
                    self.assertEqual(controller.state["command_id"], self.command["id"])
                    self.assertEqual(events, [] if kind == "invalidate" else [("save", self.command["id"], (), {})])

    def test_transport_failure_does_not_consume_command_or_save(self):
        """Only Blocked refusals are consumed; transport errors remain recoverable reads."""
        for kind in ("revision", "bundle", "invalidate"):
            with self.subTest(kind=kind):
                controller = self.prepared(kind)
                before = controller.state["command_id"]
                error = Transient("unavailable")
                with self.trace(controller, read_error=error) as events, self.assertRaises(Transient) as caught:
                    self.invoke(controller, kind)
                self.assertIs(caught.exception, error)
                self.assertEqual(controller.state["command_id"], before)
                self.assertEqual(events, [("repo", "pulls/9", "GET", None)])

    def test_reconciliation_refusals_preserve_read_order_and_consumption(self):
        """Empty, disallowed and renamed-from-protected paths refuse at both callers."""
        for kind in ("invalidate", "reopen"):
            for files in (
                [],
                [{"filename": ".github/workflows/ci.yml"}],
                [{"filename": "docs/guide.md", "previous_filename": ".github/workflows/ci.yml", "status": "renamed"}],
            ):
                with self.subTest(kind=kind, files=files):
                    controller = self.prepared(kind)
                    before = controller.state["command_id"]
                    with self.trace(controller, files=files) as events, self.assertRaises(Blocked) as caught:
                        self.invoke(controller, kind)
                    self.assertIs(type(caught.exception), Blocked)
                    self.assertEqual(str(caught.exception), "cannot reconcile accumulated PR changes")
                    expected = [("repo", "pulls/9", "GET", None)] if kind == "invalidate" else []
                    self.assertEqual(events, expected + [("pages", "pulls/9/files", None)])
                    self.assertEqual(
                        controller.state["command_id"], self.command["id"] if kind == "invalidate" else before
                    )

    def test_success_paths_preserve_reads_and_save_boundaries(self):
        """Allowed work consumes and saves only at the original caller-owned boundary."""
        files = [
            {"filename": "docs/z.md"},
            {"filename": "docs/guide.md", "previous_filename": "docs/a.md", "status": "renamed"},
        ]
        for kind in ("revision", "bundle", "invalidate", "reopen"):
            with self.subTest(kind=kind):
                controller = self.prepared(kind)
                before = controller.state["command_id"]
                with self.trace(controller, files=files) as events:
                    self.invoke(controller, kind)
                consumed = self.command["id"] if kind in ("revision", "invalidate") else before
                expected = [] if kind == "reopen" else [("repo", "pulls/9", "GET", None)]
                if kind in ("invalidate", "reopen"):
                    expected.append(("pages", "pulls/9/files", None))
                    self.assertEqual(controller.state["changed_files"], ["docs/a.md", "docs/guide.md", "docs/z.md"])
                if kind != "bundle":
                    expected.append(("save", consumed, (), {}))
                self.assertEqual(events, expected)
                self.assertEqual(controller.state["command_id"], consumed)

    def test_reviewed_retry_refusals_remain_unconsumed(self):
        """The reviewed-repair path deliberately does not use consume-on-refusal."""
        for failure, message in (
            ("size", "repair feedback exceeds 4000 bytes; provide a shorter writer comment"),
            ("rounds", "writer-requested repair allowance exhausted"),
        ):
            with self.subTest(failure=failure):
                controller = self.loaded()
                before = controller.state["command_id"]
                controller.permissions["writer"] = True
                if failure == "rounds":
                    controller.state["round"] = self.config["max_fix_rounds"]
                comments = [
                    {
                        "id": self.command["id"] - 1,
                        "user": {"login": "writer"},
                        "body": "x" * 4001 if failure == "size" else "Please clarify this wording.",
                    }
                ]
                with self.trace(controller) as events, self.assertRaises(Blocked) as caught:
                    controller.retry_reviewed(self.command, comments)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(controller.state["command_id"], before)
                self.assertEqual(events, [])

    def test_step_authority_check_keeps_its_no_merged_variant(self):
        """After observation, the authority read checks head/open but not the merged flag."""
        for failure in ("head", "closed", "merged", "unchanged"):
            with self.subTest(failure=failure):
                controller = self.loaded()
                before = controller.state["command_id"]
                pr = deepcopy(self.gh.pr)
                if failure == "head":
                    pr["head"]["sha"] = "f" * 40
                elif failure == "closed":
                    pr["state"] = "closed"
                elif failure == "merged":
                    pr["merged"] = True
                with (
                    self.trace(controller, authority_pr=pr) as events,
                    patch.object(controller, "load"),
                    patch.object(controller, "sync_stage"),
                    patch.object(controller, "commands", return_value=False),
                    patch.object(controller, "refresh_autonomous", return_value=True),
                    patch.object(controller, "review", side_effect=lambda: events.append(("review",))),
                ):
                    if failure in ("head", "closed"):
                        with self.assertRaises(Blocked) as caught:
                            controller.step(7)
                        self.assertIs(type(caught.exception), Blocked)
                        self.assertEqual(str(caught.exception), "PR closed or head changed outside the controller")
                    else:
                        self.assertTrue(controller.step(7))
                expected = [
                    ("repo", "issues/7", "GET", None),
                    ("pages", "issues/7/comments", None),
                    ("repo", "pulls/9", "GET", None),
                    ("repo", "pulls/9", "GET", None),
                ]
                if failure in ("merged", "unchanged"):
                    expected.append(("review",))
                self.assertEqual(events, expected)
                self.assertEqual(controller.state["command_id"], before)

    def test_review_agent_record_is_optional_and_keeps_field_order(self):
        """Legacy reports stay unchanged; profiles attach identity before review metadata."""
        for named in (False, True):
            with self.subTest(named=named):
                controller = self.loaded()
                if named:
                    controller.config = named_config()
                provider = "sol-high" if named else "codex"
                report = {**deepcopy(APPROVE), "dispositions": [], "design_issue": None}
                original_keys = list(report)
                controller.record_pr_review(controller.state, provider, report)
                self.assertEqual(
                    list(report),
                    original_keys
                    + (["agent"] if named else [])
                    + ["head", "model", "input_revision", "sequence", "finding_ids"],
                )
                if named:
                    self.assertEqual(
                        report["agent"],
                        {
                            "profile": "sol-high",
                            "provider": "openai",
                            "harness": "codex",
                            "model": "gpt-6.1-sol",
                            "effort": "high",
                        },
                    )
                else:
                    self.assertNotIn("agent", report)

    def test_issue_identity_uses_only_title_and_body_in_that_order(self):
        """Metadata changes do not change identity; either text field still triggers reassessment."""
        for field in ("title", "body", "labels"):
            with self.subTest(field=field):
                controller = self.loaded()
                before = controller.state["command_id"]
                controller.issue = deepcopy(controller.issue)
                if field == "labels":
                    controller.issue["labels"].append({"name": "display-only"})
                else:
                    controller.issue[field] += " Updated."
                with self.trace(controller) as events, patch.object(controller, "resume_with_head") as resumed:
                    controller.retry_reviewed(self.command, [])
                self.assertEqual(controller.state["command_id"], self.command["id"])
                self.assertGreater(self.command["id"], before)
                if field == "labels":
                    resumed.assert_not_called()
                    self.assertEqual(events, [("save", self.command["id"], (), {})])
                    self.assertEqual(
                        controller.state["issue_digest"], digest([controller.issue["title"], controller.issue["body"]])
                    )
                else:
                    resumed.assert_called_once_with(
                        {"title": controller.issue["title"], "body": controller.issue["body"], "discussion": []}
                    )
                    self.assertEqual(events, [])
