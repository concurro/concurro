"""Audit baselines verify immutable data and never issue remote writes."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import conversation
import report_view
from controller import Controller
from design_audit import Snapshot, archived_report, measure
from pipeline_fixtures import ConsensusFixture
from report_store import Store


class AuditTests(ConsensusFixture, unittest.TestCase):
    """Collect fixture archives offline and reproduce a stable source manifest."""

    def snapshot(self, directory):
        """Export fake API responses, including metadata, to the same offline format."""
        path = Path(directory)
        (path / "snapshot.json").write_text(
            json.dumps(
                {
                    "repository": self.config["repository"],
                    "issue": 7,
                    "collection_started_at": "2026-11-07T00:00:00+00:00",
                }
            )
        )
        for number, value in self.gh.artifacts[7].items():
            (path / f"{number}.json").write_text(json.dumps(value))
        return Snapshot(path, self.config["repository"], 7)

    def test_complete_round_is_reproducible_without_network_and_counts_no_replay(self):
        """Durable replay and duplicate index references cannot inflate review counts."""
        self.reach("plan_qc")
        with patch.object(Controller, "validate_design_report", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.reach("implement")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            with patch("design_audit.subprocess.run", side_effect=AssertionError("network")):
                result = measure(snapshot, self.config["bot_login"])
                again = measure(Snapshot(directory, self.config["repository"], 7), self.config["bot_login"])
            self.assertEqual(result, again)
            self.assertEqual(result["by_round"]["run 1 / 1 cooperative"]["accepted"], 2)
            self.assertTrue(all(r["verification"] == "verified" for r in result["reports"]))
            self.assertTrue(all("body_sha256" in s for s in result["sources"]))
            self.assertIsNone(result["engine_commit"])

    def test_missing_or_tampered_data_is_reported_not_treated_as_clean(self):
        """An unavailable report contributes no invented objection counts or verification."""
        state = self.reach("implement")
        number = next(r["artifact"] for r in state["reports"] if r["step"] == "Design review")
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                snapshot = self.snapshot(directory)
                path = Path(directory) / f"{number}.json"
                if missing:
                    path.unlink()
                else:
                    value = json.loads(path.read_text())
                    value["artifact"]["body"] += "edited"
                    path.write_text(json.dumps(value))
                result = measure(snapshot, self.config["bot_login"])
                report = next(r for r in result["reports"] if r["artifact"] == number)
                self.assertTrue(report["verification"].startswith("unavailable:"))
                self.assertNotIn("objection_dispositions", report)

    def test_timing_baseline_distinguishes_observations_from_missing_reports(self):
        """Reports carry optional receipts; repeatable measurements never fill legacy gaps."""
        self.reach("plan_qc")
        original = self.agents.ask
        counter = 0

        def ask(provider, stage, context):
            nonlocal counter
            counter += 1
            self.agents.timing = (
                {
                    "turn": "12345678-1234-1234-1234-123456789abc",
                    "status": "done",
                    "created": 100,
                    "started": 110,
                    "finished": 140,
                }
                if counter == 1
                else None
            )
            return original(provider, stage, context)

        with patch.object(self.agents, "ask", side_effect=ask):
            self.reach("implement")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            result = measure(snapshot, self.config["bot_login"])
            self.assertEqual(result, measure(snapshot, self.config["bot_login"]))
        first, second = result["reports"]
        self.assertEqual(first["durations"], {"admission_to_claim_seconds": 10, "execution_seconds": 30})
        self.assertIsNotNone(first["attempt"])
        self.assertIsNone(second["timing"])
        self.assertEqual(second["durations"], {"admission_to_claim_seconds": None, "execution_seconds": None})
        counts = result["by_round"]["run 1 / 1 cooperative"]
        self.assertEqual((counts["timing_observed"], counts["timing_unavailable"]), (1, 1))
        self.assertNotIn("round_duration", result)

    def test_transport_refuses_mutations_and_other_paths_before_invocation(self):
        """No caller can turn the audit reader into a general GitHub client."""
        self.reach("implement")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            with patch("design_audit.subprocess.run", side_effect=AssertionError("invoked")):
                for path, method in (
                    ("issues/7/artifacts/1", "POST"),
                    ("issues/8/artifacts/1", "GET"),
                    ("issues/7/comments", "GET"),
                    ("issues/7/artifacts/1?x=y", "GET"),
                ):
                    with self.subTest(path=path, method=method), self.assertRaises(ValueError):
                        snapshot.repo(path, method)

    def test_multipart_design_reports_are_verified_through_the_real_writer(self):
        """Split published design reports remain measurable rather than unavailable."""
        with patch("report_store.CHUNK", 128):
            state = self.reach("implement")
        numbers = [r["artifact"] for r in state["reports"] if r["step"] == "Design review"]
        self.assertEqual(len(numbers), 2)
        for number in numbers:
            self.assertIn("[Full report part 2](", self.gh.artifacts[7][number]["artifact"]["body"])
        with tempfile.TemporaryDirectory() as directory:
            result = measure(self.snapshot(directory), self.config["bot_login"])
        self.assertEqual([r["verification"] for r in result["reports"]], ["verified", "verified"])
        self.assertTrue(all("report_digest" in r for r in result["reports"]))

    def test_report_link_text_is_unchanged(self):
        """Literal expectations pin the existing single- and multipart link spelling."""
        root = "https://github.com/example/repo/issues/7/artifacts/"
        self.assertEqual(
            conversation.report_link(root, 1, 1, 12),
            "[Full report](https://github.com/example/repo/issues/7/artifacts/12)",
        )
        self.assertEqual(
            [conversation.report_link(root, i, 3, n) for i, n in enumerate((12, 13, 14), 1)],
            [
                "[Full report part 1](https://github.com/example/repo/issues/7/artifacts/12)",
                "[Full report part 2](https://github.com/example/repo/issues/7/artifacts/13)",
                "[Full report part 3](https://github.com/example/repo/issues/7/artifacts/14)",
            ],
        )

    def literal_archive(self, links, multipart=False):
        """Store real committed report parts behind an independently written archive."""
        report = {"decision": "approve", "rationale": "x" * 250}
        store = Store(self.gh, 7, self.config["bot_login"])
        with patch("report_store.CHUNK", 128 if multipart else 16000):
            key = store.put(report)
        self.assertEqual(store.numbers[key], [1, 2, 3] if multipart else [1])
        item = self.gh.repo(
            "issues/7/artifacts",
            "POST",
            {
                "type": "generic",
                "name": "Concurro report fixture",
                "body": "<!-- concurro:event:v1:fixture -->\n### Review\n\n" + links,
            },
        )
        return report, item["number"]

    def test_literal_three_part_archive_is_verified(self):
        """Parsing must work independently of the shared link writer and regex."""
        report, number = self.literal_archive(
            "- [Full report part 1](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/1)\n"
            "- [Full report part 2](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/2)\n"
            "- [Full report part 3](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/3)",
            multipart=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            store = Store(snapshot, 7, self.config["bot_login"])
            self.assertEqual(archived_report(snapshot, store, number), report)

    def test_literal_single_part_archive_is_unchanged(self):
        """Existing single-part links still recover the same verified report."""
        report, number = self.literal_archive(
            "- [Full report](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/1)"
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.snapshot(directory)
            store = Store(snapshot, 7, self.config["bot_login"])
            self.assertEqual(archived_report(snapshot, store, number), report)

    def test_report_links_must_belong_to_the_exact_artifact_root(self):
        """A foreign link must never be interpreted as a local artifact number."""
        for root in (
            "https://other.example/cylewitruk/stacks-core-autobot/issues/7/artifacts/",
            "https://github.com/other/repo/issues/7/artifacts/",
            "https://github.com/cylewitruk/stacks-core-autobot/issues/8/artifacts/",
        ):
            with self.subTest(root=root):
                self.setUp()
                _, number = self.literal_archive("- [Full report part 1](" + root + "1)")
                with tempfile.TemporaryDirectory() as directory:
                    snapshot = self.snapshot(directory)
                    store = Store(snapshot, 7, self.config["bot_login"])
                    with self.assertRaisesRegex(ValueError, "JSON block unavailable"):
                        archived_report(snapshot, store, number)
                    self.assertEqual(set(snapshot.reads), {number})

    def test_15_readable_archive_uses_only_terminal_canonical_links(self):
        """Model-written links and details blocks cannot redirect the format-five reader."""
        root = "https://github.com/" + self.config["repository"] + "/issues/7/artifacts/"
        for multipart in (False, True):
            self.setUp()
            report = {
                "decision": "approve",
                "rationale": "The review accepts this design.\n\n"
                + "[Full report]("
                + root
                + "99)\n\n"
                + "```text\n<details><summary>Raw data</summary>\n\n"
                + "[Full report]("
                + root
                + "98)\n\n</details>\n```",
            }
            store = Store(self.gh, 7, self.config["bot_login"])
            with patch("report_store.CHUNK", 128 if multipart else 16000):
                key = store.put(report)
            numbers = store.numbers[key]
            links = "\n".join(
                "- " + conversation.report_link(root, i, len(numbers), number) for i, number in enumerate(numbers, 1)
            )
            body = report_view.render({"id": "fixture", "kind": "qc", "details": {}}, report, links)
            self.assertIn("[Full report](" + root + "99)", body)
            artifact = self.gh.repo(
                "issues/7/artifacts", "POST", {"type": "generic", "name": "Readable fixture", "body": body}
            )
            with tempfile.TemporaryDirectory() as directory:
                snapshot = self.snapshot(directory)
                self.assertEqual(
                    archived_report(snapshot, Store(snapshot, 7, self.config["bot_login"]), artifact["number"]), report
                )
                self.assertNotIn(99, snapshot.reads)
                self.assertNotIn(98, snapshot.reads)
            with self.assertRaisesRegex(ValueError, "Raw data"):
                report_view.data_links(body.rsplit(report_view.RAW_OPEN, 1)[0])
