"""Dashboard rendering contracts, based on the pre-extraction snapshots."""

import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import markup
from actions import BUNDLE_REASON
from controller import Controller
from fakes import ClassifiedAgents
from pipeline_fixtures import ISSUE, PipelineFixture
from policy import approval_binding, design_roster, digest

GOLDENS = Path(__file__).parent / "goldens/dashboard"


class DashboardSnapshotTests(PipelineFixture, unittest.TestCase):
    """Real lifecycle states plus display-only variants of transient and paused work."""

    def setUp(self):
        """Use manual approval and classified reports to exercise both dashboard tables."""
        super().setUp()
        self.agents = ClassifiedAgents()

    def render(self, controller):
        """Freeze only the hidden audit digest, which includes random attempts and timestamps."""
        before = deepcopy(controller.state)
        reference = {**controller.checkpoints.reference(), "digest": "0" * 64}
        with patch.object(controller.checkpoints, "reference", return_value=reference):
            result = controller.summary_body()
        self.assertEqual(controller.state, before, "rendering must not mutate durable state")
        self.assertEqual(
            markup.BLOCKING_NOTE in result,
            any(f["status"] in ("open", "withdrawn") for f in controller.state.get("findings", [])),
        )
        return result

    def scenarios(self):
        """Capture stage transitions; restore display-only variants before advancing again."""
        bodies = {}
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        controller.initialize(self.gh.comments, "manual")
        bodies["intake"] = self.render(controller)
        for stage in ("plan", "plan_qc"):
            controller = self.reach(stage)
            bodies[stage] = self.render(controller)
        initial = deepcopy(controller.state)
        members = design_roster(self.config)
        controller.state["review_repair"] = {
            "binding": digest([approval_binding(controller.state, self.config), members[0]]),
            "attempts": 1,
        }
        bodies["serial_correction"] = self.render(controller)
        controller.state["review_batch"] = {
            "members": {
                members[0]["id"]: {"identity": members[0], "status": "running"},
                members[1]["id"]: {"identity": members[1], "status": "retrying", "review_repair": {"attempts": 1}},
            }
        }
        controller.state["reports"].append(
            {
                "step": "Design review",
                "round": "1 cooperative",
                "provider": members[1]["provider"],
                "outcome": "invalid",
                "artifact": 90,
                "diagnostic": {"status": "correcting", "attempt": 1, "limit": 4, "detail": "Quote does not match."},
                "timing": {
                    "turn": "12345678-1234-1234-1234-123456789abc",
                    "status": "done",
                    "created": 100,
                    "started": 110,
                    "finished": 150,
                },
            }
        )
        bodies["mixed_review"] = self.render(controller)
        for status in ("queued", "running", "retrying", "complete", "done", "unknown"):
            for record in controller.state["review_batch"]["members"].values():
                record.pop("review_repair", None)
                record["status"] = status
            bodies["batch_" + status] = self.render(controller)
        controller.state = initial
        for stage in ("clarify", "blocked", "escalated", "rejected", "stopped"):
            controller.state = deepcopy(initial)
            controller.state.update(
                stage=stage,
                retry_stage="plan_qc",
                reason=BUNDLE_REASON if stage == "blocked" else "A decision is needed.",
            )
            if stage == "escalated":
                controller.state["revision_wait"] = {"stage": "plan"}
                controller.state["reason"] = "Plan revision budget exhausted: Design reviewers require a revision."
            bodies[stage] = self.render(controller)
        controller.state = initial
        controller = self.reach("planned")
        bodies["planned"] = self.render(controller)
        self.gh.comment("/concurro approve " + controller.state["plan_id"])
        controller.commands(self.gh.comments)
        self.assertEqual(controller.state["stage"], "implement")
        bodies["implement"] = self.render(controller)
        for stage in ("publish", "review", "gates"):
            controller = self.reach(stage)
            bodies[stage] = self.render(controller)
        controller = self.tick()
        bodies["handoff"] = self.render(controller)
        initial = deepcopy(controller.state)
        controller.state["candidate"]["dependencies"] = {
            "Cargo.lock": {
                "counts": {"added": 1, "removed": 2, "changed": 4},
                "changed": [{"name": name, "change": "updated"} for name in ("one", "two", "three", "four")],
            }
        }
        bodies["dependencies"] = self.render(controller)
        controller.state = deepcopy(initial)
        controller.state["reports"] = [
            {
                "step": "Code review",
                "round": "x" * 2000 + str(i),
                "provider": "claude",
                "outcome": "approve",
                "artifact": i,
            }
            for i in range(40)
        ]
        bodies["compact"] = self.render(controller)
        for stage in ("merged", "observed_merged", "closed"):
            controller.state = deepcopy(initial)
            controller.state["stage"] = stage
            bodies[stage] = self.render(controller)
        self.gh.comment("/concurro invalidate")
        controller = self.tick()
        bodies["previous_run"] = self.render(controller)
        self.assertEqual(controller.state["issue"], ISSUE)
        return bodies

    def test_dashboard_bodies_match_snapshots(self):
        """Keep every byte, including links, alert severity, table order and recovery metadata."""
        bodies = self.scenarios()
        self.assertEqual(set(bodies), {p.stem for p in GOLDENS.glob("*.md")})
        for name, body in bodies.items():
            with self.subTest(name=name):
                self.assertEqual(body, (GOLDENS / (name + ".md")).read_text())

    def test_snapshot_semantics_are_independently_checked(self):
        """Guard the important meanings so regenerating goldens cannot silently bless a regression."""
        bodies = self.scenarios()
        self.assertIn("> [!WARNING]", bodies["planned"])
        self.assertIn("Awaiting writer approval", bodies["planned"])
        self.assertIn("`claude`: reviewing; `codex`: correcting its output", bodies["mixed_review"])
        self.assertIn("claude: Reviewing<br>codex: Correcting output", bodies["mixed_review"])
        self.assertIn("10.0 s | 40.0 s", bodies["mixed_review"])
        self.assertIn("`/concurro invalidate`", bodies["blocked"])
        self.assertIn("`/concurro retry --plan-revisions 1`", bodies["escalated"])
        self.assertIn("Ready for human review", bodies["handoff"])
        for stage in ("merged", "observed_merged"):
            self.assertIn("has been merged", bodies[stage])
            self.assertIn("| Handoff | ✅ Merged |", bodies[stage])
        self.assertIn("| Handoff | ⏹️ Closed without merge |", bodies["closed"])
        self.assertNotIn("Handoff ✅", bodies["closed"])
        self.assertNotIn("↳ Round", bodies["compact"])
        self.assertLess(len(bodies["compact"].encode()), 60000)
        main, diagnostics = bodies["previous_run"].split("<details><summary>Diagnostics</summary>", 1)
        self.assertNotIn("↳ Round", main)
        self.assertIn("Not assessed yet", main)
        self.assertIn("Run 1 — invalidated", diagnostics)
        self.assertIn("Earlier approvals no longer apply", diagnostics)

    def test_22_blocking_note_only_appears_for_unfinished_findings(self):
        """Open and withdrawn findings explain the merge gate; completed findings do not."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        controller.initialize(self.gh.comments, "manual")
        for status in ("open", "withdrawn", "fixed", "obsolete", "resolved"):
            with self.subTest(status=status):
                controller.state["findings"] = [{"status": status}]
                body = self.render(controller)
                self.assertEqual(markup.BLOCKING_NOTE in body, status in ("open", "withdrawn"))
