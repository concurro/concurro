"""Shared report artifacts retain evidence, attribution and interrupted multipart layouts."""

import json
import re
import tempfile
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import classification
import conversation
import dashboard_runs
import markup
import report_artifact
import report_store
import report_view
import review_timing
from artifact_fixtures import fixed_parts
from checkpoints import Checkpoints
from controller import Controller
from design_audit import Snapshot, measure
from fakes import CONFIG, FakeAgents, FakeGitHub, decision
from pipeline_fixtures import PipelineFixture
from policy import approval_valid, digest
from report_store import Store as ReportStore

# These pin the phase-two encoding and its interrupted-write behavior.
Store = partial(ReportStore, write_version=2)


class SharedReportTests(unittest.TestCase):
    """Exercise checkpoint retention and publication against the same immutable records."""

    def setUp(self):
        """Isolate GitHub and the checkpoint/report store for every scenario."""
        self.gh = FakeGitHub(CONFIG)
        self.gh.comments = []
        self.cp = Checkpoints(self.gh, CONFIG, 7)
        self.cp.reports.write_version = 2
        self.state = {"version": 2, "issue": 7, "stage": "stopped", "calls": 1}
        self.report = {
            "decision": "approve",
            "rationale": "Evidence > --> \\u003e 😀. " * 2000,
            "concerns": [],
            "questions": [],
        }

    def queue(self, report, provider="claude"):
        """Queue a design assessment with its own publication identity."""
        self.state["qc"] = {"plan": deepcopy(report)}
        conversation.queue(
            self.state,
            None,
            source=("qc", "plan"),
            kind="qc",
            details={
                "provider": provider,
                "round": 1,
                "phase": "cooperative",
                "reviewer": provider,
                "resolved": [],
                "model": "test-model",
            },
        )
        return self.state["conversation"]["pending"][-1]

    def data(self):
        """Select evidence artifacts without audit or derived plan views."""
        return [x for x in self.gh.artifacts[7].values() if x["artifact"]["body"].startswith(report_store.MARKER)]

    def test_retention_and_publication_share_one_core_and_keep_annotations(self):
        """Ratings and controller identity are receipt snapshots, not extra wrapper artifacts."""
        retained = self.report | {"classification": {"complexity": "high"}}
        self.state["operation"] = {"response": retained, "status": "completed"}
        self.cp.save(self.state)
        first = self.data()[0]["artifact"]["number"]
        event = self.queue(retained | {"agent": {"profile": "reviewer"}, "sequence": 7})
        self.assertEqual(event["format"], 6)
        self.cp.save(self.state)
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        self.cp.save(self.state)
        self.assertEqual(len(self.data()), 1)
        self.assertEqual(len(self.gh.artifacts[7]), 2)
        entry = self.state["reports"][0]
        self.assertEqual(entry["artifact"], first)
        self.assertEqual(entry["event"], event["id"])
        self.assertEqual(
            entry["publication"], {k: event[k] for k in ("kind", "title", "source", "details", "sequence", "at")}
        )
        event["details"]["model"] = "changed later"
        self.assertEqual(entry["publication"]["details"]["model"], "test-model")
        self.assertEqual(self.cp.reports.restore_reference(entry["data"]), self.state["qc"]["plan"])
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), self.state)
        body = self.data()[0]["artifact"]["body"]
        self.assertIn("Submitted model output", body)
        self.assertIn("concurro:report-part:v2", body)
        self.assertNotIn("```json", body)

    def test_same_content_keeps_distinct_events_and_runs(self):
        """Reused content is not a reused vote or a prior invalidated run."""
        first = self.queue(self.report)
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        self.state["invalidations"] = [{"display_start": dashboard_runs.start(self.state)}]
        second = self.queue(self.report, "codex")
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        self.assertEqual(len(self.data()), 1)
        entries = self.state["reports"]
        self.assertEqual([x["event"] for x in entries], [first["id"], second["id"]])
        self.assertEqual(entries[0]["artifact"], entries[1]["artifact"])
        self.assertEqual(dashboard_runs.current_reports(self.state), [entries[1]])
        # Replay after receipt mutation must not append the same event a second time.
        self.state["conversation"]["pending"] = [deepcopy(second)]
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        self.assertEqual(len(self.state["reports"]), 2)

    def test_continuation_only_recovery_freezes_layout_when_chunk_setting_changes(self):
        """A lost continuation reply pins all boundaries before the entry part exists."""
        value = {"summary": "payload --> \\u003e " * 200}
        original = self.gh.repo
        lost = []

        def fail(path, method="GET", data=None):
            """Apply the first part creation and lose its response."""
            result = original(path, method, data)
            if method == "POST" and not lost:
                lost.append(result["number"])
                raise RuntimeError("lost reply")
            return result

        with fixed_parts(700), patch.object(self.gh, "repo", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.cp.reports.put(value)
        body = self.data()[0]["artifact"]["body"]
        self.assertNotIn(f"{digest(value)}:1:", body)
        saved = deepcopy(self.gh.artifacts)
        store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        with fixed_parts(10000):
            key = store.put(value)
        self.assertEqual(self.gh.artifacts[7][lost[0]], saved[7][lost[0]])
        self.assertGreater(len(store.numbers[key]), 2)
        self.assertEqual(store.numbers[key][0], max(self.gh.artifacts[7]))
        self.assertEqual(Store(self.gh, 7, CONFIG["bot_login"]).get(key, store.numbers[key]), value)

    def test_rejected_intake_uses_retained_value_without_copying_serialized_output(self):
        """A draft reconstructs its exact output string from the retained report plus key order."""
        value = {"rationale": "invalid " * 3000, "decision": "unknown", "extra": {"z": 1, "a": 2}}
        self.state["operation"] = {"response": value, "status": "completed"}
        self.cp.save(self.state)
        self.state["intake_draft"] = {
            "output": json.dumps(value, indent=2, ensure_ascii=True),
            "reason": "Invalid decision",
            "attempt": 1,
            "truncated": False,
        }
        conversation.queue(
            self.state,
            None,
            source=("intake_draft",),
            kind="intake_rejection",
            details={"provider": "claude", "round": "rejected output 1"},
        )
        self.cp.save(self.state)
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        self.cp.save(self.state)
        self.assertEqual(len(self.data()), 1)
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), self.state)
        self.assertEqual(
            self.cp.reports.restore_reference(self.state["reports"][0]["data"]), self.state["intake_draft"]
        )

    def test_audit_counts_distinct_events_sharing_one_artifact(self):
        """Offline audit measurements deduplicate replay by event, preserving independent attempts."""
        self.queue(self.report)
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        self.queue(self.report, "codex")
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        # Establish the audit at number 1 in a fresh issue, as the audit CLI expects.
        gh = FakeGitHub(CONFIG)
        cp = Checkpoints(gh, CONFIG, 7)
        cp.save({"version": 2, "issue": 7, "stage": "stopped"})
        state = {"version": 2, "issue": 7, "stage": "stopped", "calls": 2}
        for provider in ("claude", "codex"):
            state["qc"] = {"plan": self.report}
            conversation.queue(
                state,
                None,
                source=("qc", "plan"),
                kind="qc",
                details={
                    "provider": provider,
                    "round": 1,
                    "phase": "cooperative",
                    "reviewer": provider,
                    "resolved": [],
                    "model": "test-model",
                },
            )
            conversation.publish(state, gh, CONFIG, cp.reports)
        cp.save(state)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "snapshot.json").write_text(json.dumps({"repository": CONFIG["repository"], "issue": 7}))
            for n, response in gh.artifacts[7].items():
                (root / f"{n}.json").write_text(json.dumps(response))
            result = measure(Snapshot(root, CONFIG["repository"], 7), CONFIG["bot_login"])
        self.assertEqual(len(result["reports"]), 2)
        self.assertTrue(all(r["verification"] == "verified" for r in result["reports"]))

    def test_published_metadata_survives_pressure_and_later_mutation(self):
        """A published attempt retains its ratings and timing even when current annotations change."""
        rating = {"complexity": "high", "source": {"report": "attempt-1"}, "rationale": "Evidence"}
        self.report["classification"] = deepcopy(rating)
        event = self.queue(self.report)
        event["index"].update(assessment="attempt-1", timing={"created": 1, "finished": 3})
        conversation.publish(self.state, self.gh, CONFIG, self.cp.reports)
        entry = deepcopy(self.state["reports"][0])
        self.state["qc"] = {}
        classification.make_room(self.state, 1, lambda _: 100)
        review_timing.make_room(self.state, 1, lambda _: 100)
        self.assertEqual(self.state["reports"][0], entry)
        self.report["classification"]["complexity"] = "low"
        self.assertEqual(self.cp.reports.restore_reference(entry["data"])["classification"], rating)
        self.cp.save(self.state)
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), self.state)

    def test_every_lost_part_reply_recovers_without_new_artifacts(self):
        """Lose each continuation and the entry in turn, then finish the original layout."""
        value = {"summary": "evidence --> \\u003e " * 100}
        with fixed_parts(900):
            key = self.cp.reports.put(value)
        count = len(self.cp.reports.numbers[key])
        for failed in range(count):
            with self.subTest(write=failed):
                self.setUp()
                original, writes = self.gh.repo, []

                def write(path, method="GET", data=None):
                    """Apply and lose exactly the selected part creation reply."""
                    result = original(path, method, data)
                    if method == "POST":
                        writes.append(result["number"])
                        if len(writes) == failed + 1:
                            raise RuntimeError("lost reply")
                    return result

                with fixed_parts(900), patch.object(self.gh, "repo", side_effect=write):
                    with self.assertRaises(RuntimeError):
                        self.cp.reports.put(value)
                before = deepcopy(self.gh.artifacts[7])
                store = Store(self.gh, 7, CONFIG["bot_login"])
                with fixed_parts(20000):
                    store.put(value)
                self.assertEqual(len(self.gh.artifacts[7]), count)
                for number, item in before.items():
                    self.assertEqual(self.gh.artifacts[7][number], item)
                self.assertEqual(Store(self.gh, 7, CONFIG["bot_login"]).get(key, store.numbers[key]), value)

    def test_identical_duplicates_preserve_pinned_ids_and_conflicts_fail(self):
        """Discovery picks the oldest identical part; pinned copies remain authoritative."""
        value = {"summary": "same evidence"}
        key = self.cp.reports.put(value)
        original = self.cp.reports.numbers[key][0]
        data = self.gh.artifacts[7][original]["artifact"]
        duplicate = self.gh.repo("issues/7/artifacts", "POST", {k: data[k] for k in ("type", "name", "body")})
        store = Store(self.gh, 7, CONFIG["bot_login"])
        store.put(value)
        self.assertEqual(store.numbers[key], [original])
        self.assertEqual(Store(self.gh, 7, CONFIG["bot_login"]).get(key, [duplicate["number"]]), value)
        self.gh.artifacts[7][duplicate["number"]]["artifact"]["body"] += "changed"
        self.gh.artifacts[7][duplicate["number"]]["versions"][0]["body"] += "changed"
        with self.assertRaises(report_store.ReportDataError):
            Store(self.gh, 7, CONFIG["bot_login"]).put(value)

    def test_shared_parts_reject_missing_edited_foreign_and_reordered_data(self):
        """No partial or substituted multipart value can be used as retained evidence."""
        for failure in ("missing", "edited", "version", "foreign", "reordered", "layout"):
            with self.subTest(failure=failure):
                self.setUp()
                value = {"summary": "source " * 500}
                with fixed_parts(1000):
                    key = self.cp.reports.put(value)
                numbers = self.cp.reports.numbers[key][:]
                item = self.gh.artifacts[7][numbers[-1]]
                if failure == "missing":
                    del self.gh.artifacts[7][numbers[-1]]
                elif failure == "version":
                    item["versions"][0]["version"] = 2
                elif failure == "foreign":
                    item["artifact"]["creator"]["login"] = "human"
                elif failure == "reordered":
                    numbers[1], numbers[2] = numbers[2], numbers[1]
                else:
                    body = item["artifact"]["body"]
                    body = (
                        body.replace("source", "forged")
                        if failure == "edited"
                        else body.replace('"version": 2', '"version": 3')
                    )
                    item["artifact"]["body"] = item["versions"][0]["body"] = body
                with self.assertRaises(report_store.ReportDataError):
                    Store(self.gh, 7, CONFIG["bot_login"]).get(key, numbers)

    def test_malformed_descriptor_is_rejected_before_nested_reads(self):
        """Descriptor validation precedes continuation and nested-reference resolution."""
        value = {"summary": "value"}
        key = self.cp.reports.put(value)
        number = self.cp.reports.numbers[key][0]
        response = self.gh.artifacts[7][number]
        body = response["artifact"]["body"].replace('"ends": [', '"ends": [true, ')
        response["artifact"]["body"] = response["versions"][0]["body"] = body
        original = self.gh.repo
        reads = []

        def read(path, method="GET", data=None):
            """Record all detail reads without allowing a second artifact fetch."""
            reads.append(path)
            return original(path, method, data)

        with patch.object(self.gh, "repo", side_effect=read), self.assertRaises(report_store.ReportDataError):
            Store(self.gh, 7, CONFIG["bot_login"]).get(key, [number])
        self.assertEqual(len(reads), 1)

    def test_comment_payload_never_leaks_to_rendered_html(self):
        """All comment terminators and literal escapes round-trip without exposing machine data."""
        from markdown_it import MarkdownIt

        value = {"secret_data": "--> --!> <!--> <!---> <script> & \\u003e 😀"}
        key = self.cp.reports.put(value)
        body = self.data()[0]["artifact"]["body"]
        rendered = MarkdownIt("commonmark", {"html": False}).render(body)
        # With HTML disabled comments are escaped; the real HTML-enabled renderer drops them.
        self.assertIn("Stored task data", rendered)
        html = MarkdownIt("commonmark", {"html": True}).render(body)
        visible = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        self.assertNotIn("secret_data", visible)
        self.assertNotIn("<script>", visible)
        self.assertEqual(Store(self.gh, 7, CONFIG["bot_login"]).get(key, self.cp.reports.numbers[key]), value)

    def test_cold_reads_and_complete_reuse_never_render(self):
        """Stored view bytes, not today's renderer, verify a completed artifact."""
        for width in (100000, 900):
            with self.subTest(width=width):
                self.setUp()
                value = {"summary": "Evidence `code` <tag>. " * 100}
                with fixed_parts(width):
                    key = self.cp.reports.put(value)
                numbers = self.cp.reports.numbers[key]
                before = deepcopy(self.gh.artifacts)
                with patch("report_artifact.submitted", side_effect=AssertionError("reader invoked renderer")):
                    cold = Store(self.gh, 7, CONFIG["bot_login"])
                    self.assertEqual(cold.get(key, numbers), value)
                    self.assertEqual(Store(self.gh, 7, CONFIG["bot_login"]).put(value), key)
                self.assertEqual(self.gh.artifacts, before)

    def test_interrupted_write_ignores_live_renderer_and_markup_changes(self):
        """A continuation-only recovery reproduces the frozen entry after UI code changes."""
        value = {"summary": "Evidence `code` <tag>. " * 100}
        original, lost = self.gh.repo, []

        def write(path, method="GET", data=None):
            """Keep the first continuation, losing its creation reply."""
            result = original(path, method, data)
            if method == "POST" and not lost:
                lost.append(True)
                raise RuntimeError("lost continuation")
            return result

        with fixed_parts(900), patch.object(self.gh, "repo", side_effect=write):
            with self.assertRaises(RuntimeError):
                self.cp.reports.put(value)
        frozen = report_artifact.submitted(value)
        with (
            patch.object(report_view, "render", return_value="unrelated changed UI"),
            patch.object(markup, "inline", side_effect=AssertionError("live markup used")),
            patch.object(markup, "details", side_effect=AssertionError("live markup used")),
        ):
            self.assertEqual(report_artifact.submitted(value), frozen)
            store = Store(self.gh, 7, CONFIG["bot_login"])
            key = store.put(value)
            self.assertEqual(Store(self.gh, 7, CONFIG["bot_login"]).get(key, store.numbers[key]), value)
        self.assertIn(frozen, self.gh.artifacts[7][store.numbers[key][0]]["artifact"]["body"])

    def test_stored_view_and_continuation_links_are_verified_independently(self):
        """Removing presentation recomputation must not accept an altered view or link trailer."""
        for corruption in ("view", "link", "missing link", "extra text"):
            with self.subTest(corruption=corruption):
                self.setUp()
                with fixed_parts(900):
                    key = self.cp.reports.put({"summary": "Evidence. " * 200})
                numbers = self.cp.reports.numbers[key]
                item = self.gh.artifacts[7][numbers[0]]
                body = item["artifact"]["body"]
                if corruption == "view":
                    prefix, tail = body.split("\n-->\n", 1)
                    body = prefix + "\n-->\n" + tail.replace("Evidence", "Changed")
                elif corruption == "link":
                    body = body.replace(f"/artifacts/{numbers[1]})", "/artifacts/999999)")
                elif corruption == "missing link":
                    body = body[: body.rfind("\n- [Data part")] + "\n"
                else:
                    body += "unexpected trailing text\n"
                item["artifact"]["body"] = item["versions"][0]["body"] = body
                with self.assertRaises(report_store.ReportDataError):
                    Store(self.gh, 7, CONFIG["bot_login"]).get(key, numbers)

    def test_renderer_one_matches_frozen_golden(self):
        """Pin readable bytes independently of both the writer and live rendering helpers."""
        cases = json.loads((Path(__file__).parent / "goldens/shared-view-v1.json").read_text())
        for case in cases:
            with self.subTest(case=case["name"]):
                value = deepcopy(case["value"])
                if case.get("finding_copies"):
                    value["findings"] *= case["finding_copies"]
                self.assertEqual(report_artifact.submitted(value), case["view"])


class SharedPipelineTests(PipelineFixture, unittest.TestCase):
    """Run current publication through full controller recovery, review and repair."""

    def setUp(self):
        """Use autonomous fixture stages and fresh transports."""
        self.config = deepcopy(CONFIG)
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []

    def test_pipeline_preserves_votes_and_native_reviews_without_view_artifacts(self):
        """Shared content never replaces per-reviewer votes, approvals or native PR reviews."""
        controller = self.reach("gates")
        state = controller.state
        self.assertTrue(approval_valid(state, self.config))
        self.assertEqual(state["calls"], 7)
        self.assertEqual(len(self.agents.calls), 7)
        self.assertEqual(len(self.gh.app_reviews), 2)
        self.assertEqual(set(state["reviews"]), {"claude", "codex"})
        entries = state["reports"]
        self.assertTrue(entries)
        self.assertTrue(all(e.get("event") and e.get("data") for e in entries))
        self.assertEqual(len({e["event"] for e in entries}), len(entries))
        self.assertFalse(
            any(a["artifact"]["name"].startswith("Concurro report ") for a in self.gh.artifacts[7].values())
        )
        for entry in entries:
            self.assertEqual(
                digest(controller.checkpoints.reports.restore_reference(entry["data"])), entry["source_digest"]
            )
        before = len(self.agents.calls)
        self.assertEqual(self.tick().state["stage"], "gates")
        self.assertEqual(len(self.agents.calls), before)

    def test_completed_review_recovery_does_not_charge_another_call(self):
        """Crash after response retention replays publication and applies the result exactly once."""
        self.reach("plan_qc")
        with patch.object(Controller, "validate_design_report", side_effect=RuntimeError("crash after retention")):
            with self.assertRaises(RuntimeError):
                self.tick()
        before = len(self.agents.calls)
        self.reach("implement")
        self.assertEqual(len(self.agents.calls), before + 1)  # The other reviewer still runs once.
        self.assertEqual(
            sum(stage == "plan_qc" and provider == "claude" for provider, stage, _ in self.agents.calls), 1
        )
        state = self.loaded().state
        self.assertTrue(approval_valid(state, self.config))
        events = [e["event"] for e in state["reports"]]
        self.assertEqual(len(events), len(set(events)))

    def test_rejection_and_correction_retain_diagnostics_and_independent_events(self):
        """A rejected intake output stays exact while its accepted correction proceeds normally."""
        bad = decision("invalid")
        self.agents.qc_reports = [bad, decision("proceed")]
        first = self.tick()
        rejected = next(e for e in first.state["reports"] if e["outcome"] == "invalid")
        retained = first.checkpoints.reports.restore_reference(rejected["data"])
        self.assertEqual(json.loads(retained["output"]), bad)
        self.assertTrue(retained["reason"])
        controller = self.reach("gates")
        self.assertIn(rejected, controller.state["reports"])
        self.assertEqual(controller.checkpoints.reports.restore_reference(rejected["data"]), retained)

    def test_lost_shared_publication_does_not_repeat_intake(self):
        """Lost derived publication may leave an unused copy, but does not repeat retained intake."""
        original, lost = self.gh.repo, []

        def write(path, method="GET", data=None):
            """Store the first shared report but lose its creation response."""
            result = original(path, method, data)
            if method == "POST" and (data or {}).get("body", "").startswith(report_store.MARKER) and not lost:
                lost.append(result["number"])
                raise RuntimeError("lost report reply")
            return result

        with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(conversation.PublicationError):
            self.tick()
        self.assertEqual(len(self.agents.calls), 1)
        first = deepcopy(self.gh.artifacts[7][lost[0]])
        controller = self.tick()
        self.assertEqual(len(self.agents.calls), 2)  # Planning, not another intake invocation.
        self.assertEqual([stage for _, stage, _ in self.agents.calls], ["intake", "plan"])
        self.assertEqual(controller.state["calls"], 2)
        self.assertEqual(self.gh.artifacts[7][lost[0]], first)
        self.assertNotEqual(controller.state["reports"][0]["artifact"], lost[0])
        self.assertEqual(
            sum(a["artifact"]["body"] == first["artifact"]["body"] for a in self.gh.artifacts[7].values()), 1
        )
