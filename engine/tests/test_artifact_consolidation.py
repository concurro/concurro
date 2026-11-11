"""Larger immutable artifacts and compact audit envelopes retain exact recovery semantics."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import markup
import protocol
import report_store
from checkpoints import AUDIT_MARKER, STATE_PREFIX, CheckpointError, Checkpoints, legacy_state
from fakes import CONFIG, PLAN, FakeGitHub
from policy import digest
from storage_traffic import measure


class ConsolidationTests(unittest.TestCase):
    """Exercise real saves, cold reads and interrupted writes without a live repository."""

    def setUp(self):
        """Use independent fake artifacts for each case."""
        self.gh = FakeGitHub(CONFIG)
        self.cp = Checkpoints(self.gh, CONFIG, 7)

    def fresh_store(self):
        """Simulate process loss without sharing a trusted cache."""
        return Checkpoints(self.gh, CONFIG, 7).reports

    def test_large_report_is_one_artifact_and_comment_allowance_is_unchanged(self):
        """A 96 KiB report no longer needs six data artifacts."""
        value = {"summary": "evidence " * 12000}
        key = self.cp.reports.put(value)
        numbers = self.cp.reports.numbers[key]
        self.assertEqual(len(numbers), 1)
        body = self.gh.artifacts[7][numbers[0]]["artifact"]["body"]
        self.assertGreater(len(body.encode()), 60000)
        self.assertLessEqual(len(body.encode()), 256 * 1024)
        self.assertIn("\n```text\n", body)
        self.assertEqual(protocol.PUBLICATION_BODY_BYTES, 60000)
        self.assertEqual(self.fresh_store().get(key, numbers), value)

    def test_unicode_and_long_fences_fit_complete_body_budget(self):
        """The byte allowance includes escaping, headings, fences and references."""
        value = {"summary": "😀é" * 60000 + "`" * 150000}
        key = self.cp.reports.put(value)
        numbers = self.cp.reports.numbers[key]
        self.assertGreater(len(numbers), 1)
        for number in numbers:
            self.assertLessEqual(len(self.gh.artifacts[7][number]["artifact"]["body"].encode()), 256 * 1024)
        self.assertEqual(self.fresh_store().get(key, numbers), value)

    def test_legacy_layout_reused_after_complete_or_interrupted_write(self):
        """Literal old 16,000-character parts retain their bodies, IDs and count."""
        value = {"summary": "older evidence " * 8000}
        key = digest(value)
        encoded = json.dumps({"value": value, "references": []}, sort_keys=True, indent=2)
        parts = [encoded[i : i + 16000] for i in range(0, len(encoded), 16000)]
        for complete in (False, True):
            with self.subTest(complete=complete):
                self.setUp()
                for index, part in enumerate(parts if complete else parts[:2], 1):
                    fence = markup.fence(part)
                    body = (
                        f"{report_store.MARKER}{key}:{index}:{len(parts)} -->\n"
                        f"### Report data · part {index} of {len(parts)}\n\n"
                        f"{fence}text\n{part}\n{fence}\n"
                    )
                    self.gh.repo(
                        "issues/7/artifacts",
                        "POST",
                        {
                            "type": "generic",
                            "name": f"Concurro review data {key[:32]} part {index}",
                            "body": body,
                        },
                    )
                before = deepcopy(self.gh.artifacts[7])
                store = self.fresh_store()
                store.put(value)
                self.assertEqual(len(store.numbers[key]), len(parts))
                for number, artifact in before.items():
                    self.assertEqual(self.gh.artifacts[7][number], artifact)
                self.assertEqual(self.fresh_store().get(key, store.numbers[key]), value)

    def overflow_state(self):
        """Keep non-report metadata large while retaining budgets and a completed response."""
        return {
            "version": 2,
            "issue": 7,
            "stage": "blocked",
            "calls": 48,
            "sessions": {str(i): {"transcript": "receipt " * 500} for i in range(32)},
            "operation": {"status": "completed", "response": {"summary": "retained " * 15000}},
            "findings": [{"id": "F1", "status": "open", "provider": "claude"}],
        }

    def test_overflow_metadata_spills_without_discarding_state(self):
        """The mutable audit contains a verified pointer, not an ever-growing metadata copy."""
        state = self.overflow_state()
        expected = deepcopy(state)
        self.cp.preflight([state])
        self.cp.save(state)
        envelope = legacy_state(self.cp.audit["body"])
        self.assertIn("audit_data", envelope)
        self.assertEqual(envelope["state"], {"version": 7, "issue": 7})
        self.assertLess(len(self.cp.audit["body"].encode()), 2000)
        self.assertEqual(state, expected)
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), expected)
        before = deepcopy(self.gh.artifacts[7])
        self.cp.save(state)
        self.assertEqual(self.gh.artifacts[7], before)

    def test_overflow_write_lost_reply_reuses_immutable_parts(self):
        """Loss before or after the mutable audit write never loses the completed response."""
        for marker in (report_store.MARKER, AUDIT_MARKER):
            with self.subTest(marker=marker):
                self.setUp()
                state = self.overflow_state()
                original = self.gh.repo
                lost = []

                def write(path, method="GET", data=None):
                    """Lose a committed write reply once."""
                    result = original(path, method, data)
                    if method == "POST" and (data or {}).get("body", "").startswith(marker) and not lost:
                        lost.append(result)
                        raise RuntimeError("lost reply")
                    return result

                with (
                    patch.object(self.gh, "repo", side_effect=write),
                    self.assertRaises((RuntimeError, CheckpointError)),
                ):
                    self.cp.save(state)
                recovered = Checkpoints(self.gh, CONFIG, 7)
                recovered.load([])
                recovered.save(state)
                self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state)
                bodies = [item["artifact"]["body"] for item in self.gh.artifacts[7].values()]
                self.assertEqual(len(bodies), len(set(bodies)))

    def test_overflow_identity_checked_before_fetch_and_missing_data_refused(self):
        """An envelope cannot fetch another task's metadata or silently omit missing data."""
        self.cp.save(self.overflow_state())
        envelope = legacy_state(self.cp.audit["body"])
        for change in ({"repository": "another/repo"}, {"state": {"version": 6, "issue": 7}}):
            invalid = envelope | change
            body = AUDIT_MARKER + STATE_PREFIX + json.dumps(invalid) + "\n```\n"
            with patch.object(self.cp.reports, "get", side_effect=AssertionError("fetched")):
                with self.assertRaises(CheckpointError):
                    self.cp.decode({"body": body})
        del self.gh.artifacts[7][envelope["audit_data"]["artifacts"][0]]
        with self.assertRaises(CheckpointError) as caught:
            Checkpoints(self.gh, CONFIG, 7).load([])
        self.assertEqual(caught.exception.code, "report_data")

    def test_large_report_traffic_reduces_artifacts_and_reads(self):
        """Compare identical full histories with the old and new part sizes."""
        old = measure(16, report_characters=98304, chunk_characters=16000)
        new = measure(16, report_characters=98304)
        self.assertLess(new["data_artifacts"], old["data_artifacts"] / 2)
        self.assertLess(new["data_gets"], old["data_gets"] / 2)
        self.assertLess(new["data_body_bytes"], old["data_body_bytes"])

    def test_plan_binding_survives_overflow_and_second_audit_write(self):
        """The final immutable envelope includes the newly published plan identity."""
        state = self.overflow_state() | {"plan": deepcopy(PLAN), "plan_id": "p" * 64, "base": "b" * 40}
        self.cp.save(state)
        envelope = legacy_state(self.cp.audit["body"])
        ref = envelope["audit_data"]
        complete = self.fresh_store().get(ref["digest"], ref["artifacts"])
        self.assertEqual(complete["plan_artifact"], self.cp.plan_binding)
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state)
        bodies = [item["artifact"]["body"] for item in self.gh.artifacts[7].values()]
        self.assertEqual(sum(body.count("retained retained") > 0 for body in bodies), 1)

    def test_preflight_reserves_space_by_detaching_metadata(self):
        """A near-target snapshot can reserve publication receipts without refusing the work."""
        state = {"version": 2, "issue": 7, "sessions": {"receipt": "x" * 58000}}
        self.cp.preflight([state])
        self.cp.save(state | {"receipt": "x" * 4000})
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state | {"receipt": "x" * 4000})

    def test_tampered_overflow_and_nested_or_mismatched_envelopes_are_refused(self):
        """Only a verified, task-bound, non-recursive metadata envelope is accepted."""
        state = self.overflow_state()
        self.cp.save(state)
        body = self.cp.audit["body"]
        envelope = legacy_state(body)
        original = self.fresh_store().get(envelope["audit_data"]["digest"], envelope["audit_data"]["artifacts"])
        for altered in (
            original | {"audit_data": {}},
            original | {"repository": "another/repo"},
            original | {"state": None},
            original | {"state": original["state"] | {"issue": 8}},
        ):
            key = self.cp.reports.put(altered)
            invalid = envelope | {"audit_data": {"digest": key, "artifacts": self.cp.reports.numbers[key]}}
            with self.assertRaises(CheckpointError) as caught:
                self.cp.decode({"body": AUDIT_MARKER + STATE_PREFIX + json.dumps(invalid) + "\n```\n"})
            self.assertEqual(caught.exception.code, "checkpoint_format")
        number = envelope["audit_data"]["artifacts"][0]
        item = self.gh.artifacts[7][number]
        item["artifact"]["body"] = item["artifact"]["body"].replace("receipt ", "altered ", 1)
        item["versions"][0]["body"] = item["artifact"]["body"]
        with self.assertRaises(CheckpointError) as caught:
            Checkpoints(self.gh, CONFIG, 7).load([])
        self.assertEqual(caught.exception.code, "report_data")

    def test_appends_keep_partial_blocks_and_directory_in_mutable_audit(self):
        """Metadata appends create one immutable history artifact per completed eight entries."""
        state = {"version": 2, "issue": 7, "reports": []}
        for index in range(48):
            state["reports"].append({"artifact": index + 100, "note": str(index) + "x" * 1000})
            self.cp.save(state)
            if index + 1 >= 16:
                data = [
                    v for v in self.gh.artifacts[7].values() if v["artifact"]["body"].startswith(report_store.MARKER)
                ]
                self.assertEqual(len(data), (index + 1) // 8)
                self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state)
        before = len(self.gh.artifacts[7])
        state["reports"][2]["note"] += "late timing"
        self.cp.save(state)
        self.assertEqual(len(self.gh.artifacts[7]), before + 1)
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state)

    def test_history_directory_and_tail_integrity(self):
        """Invalid layouts fail before reads; reordered blocks and changed tails fail the digest."""
        state = {
            "version": 2,
            "issue": 7,
            "reports": [{"artifact": i + 100, "note": str(i) + "x" * 1000} for i in range(25)],
        }
        self.cp.save(state)
        ref = legacy_state(self.cp.audit["body"])["report_data"][0]
        record = ref["history"]
        invalid = [
            record | {"length": 23},
            record | {"size": True},
            record | {"blocks": []},
            record | {"tail": {"value": [None], "references": [{"path": [8], "digest": "a" * 64, "artifacts": [1]}]}},
        ]
        for broken in invalid:
            with self.subTest(broken=broken), patch.object(self.cp.reports, "get", side_effect=AssertionError("fetch")):
                with self.assertRaises(report_store.ReportDataError):
                    self.cp.reports.restore_history(ref["digest"], broken)
        for change in ("blocks", "tail"):
            broken = deepcopy(record)
            if change == "blocks":
                broken["blocks"].reverse()
            else:
                broken["tail"]["value"][0]["note"] += "edited"
            with self.assertRaises(report_store.ReportDataError):
                self.fresh_store().restore_history(ref["digest"], broken)

    def test_inline_tail_reuses_canonical_reports_and_does_not_change_on_decode(self):
        """Tail metadata stays inline while its report bodies are referenced only once."""
        state = {
            "version": 2,
            "issue": 7,
            "reports": [
                {
                    "note": str(i) + "x" * 1000,
                    "report": {"verdict": "approve", "summary": f"Evidence {i}: " + "z" * 4000},
                }
                for i in range(25)
            ],
        }
        self.cp.save(state)
        ref = legacy_state(self.cp.audit["body"])["report_data"][0]
        original = deepcopy(ref["history"])
        self.assertEqual(len(original["tail"]["references"]), 1)
        self.assertIsNone(original["tail"]["value"][0]["report"])
        self.assertEqual(self.fresh_store().restore_history(ref["digest"], ref["history"]), state["reports"])
        self.assertEqual(ref["history"], original)
        bodies = [v["artifact"]["body"] for v in self.gh.artifacts[7].values()]
        self.assertEqual(sum(body.count("Evidence 24: ") for body in bodies), 1)
