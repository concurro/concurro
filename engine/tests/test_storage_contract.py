"""Literal storage formats, provenance and graft order pinned before helper extraction."""

import io
import json
import unittest
from unittest.mock import Mock, patch

import support  # noqa: F401 -- initializes the engine import path

import checkpoint_health
import conversation
import markup
import report_store
from checkpoints import CheckpointError, Checkpoints
from fakes import CONFIG, FakeGitHub
from policy import digest
from report_store import ReportDataError, Store

AUDIT = "<!-- concurro:audit:v1 -->"
PLAN_HEADER = (
    "<!-- concurro:plan:v1 -->\n# Concurro implementation plan\n\n"
    "Generated view; edits are overwritten. Request changes in issue comments.\n\n"
    "Approval is recorded in the audit state; editing this document does not approve work.\n\n"
)


class StorageContractTests(unittest.TestCase):
    """Use the existing API fake while pinning expected formats independently of the writer."""

    def setUp(self):
        """Create isolated issue storage without using any host directory or live API."""
        self.config = {**CONFIG, "repository": "owner/repo"}
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.cp = Checkpoints(self.gh, self.config, 7)
        self.bot = self.config["bot_login"]

    def store(self):
        """A fresh store has no previously verified artifacts or discovery cache."""
        return Store(self.gh, 7, self.bot)

    def create(self, body, name="Report", kind="generic"):
        """Seed an App-authored artifact using the real fake's version history."""
        return self.gh.repo("issues/7/artifacts", "POST", {"type": kind, "name": name, "body": body})

    def human_edit(self, number):
        """Add a consistent human version, leaving the original bot version intact."""
        item = self.gh.artifacts[7][number]
        item["artifact"]["body"] = "Human replacement"
        item["artifact"]["updated_by_actor"] = {"login": "writer"}
        item["versions"].insert(
            0,
            {
                "version": item["versions"][0]["version"] + 1,
                "name": item["artifact"]["name"],
                "body": "Human replacement",
                "actor": {"login": "writer"},
            },
        )

    def payload(self, state, refs):
        """Encode a hand-built checkpoint envelope without calling its writer."""
        return {
            "body": AUDIT
            + "\n```json\n"
            + json.dumps({"repository": "owner/repo", "state": state, "report_data": refs})
            + "\n```\n"
        }

    def reference(self, path, **changes):
        """One allowed report reference, with individual invalid cases supplied by tests."""
        return {"path": path, "digest": "a" * 64, "artifacts": [1], "select": [], "annotations": {}, **changes}

    def test_checkpoint_literal_round_trip(self):
        """The audit envelope and wire-version conversion stay byte-identical."""
        state = {"version": 2, "issue": 7, "stage": "blocked", "calls": 3, "reason": "é"}
        expected = (
            '<!-- concurro:audit:v1 -->\n```json\n{"repository": "owner/repo", "state": '
            '{"calls": 3, "issue": 7, "reason": "\\u00e9", "stage": "blocked", "version": 3}}\n```\n'
        )
        with patch.object(self.gh, "repo", wraps=self.gh.repo) as repo:
            self.cp.save(state)
        writes = [c.args for c in repo.call_args_list if len(c.args) > 1 and c.args[1] == "POST"]
        self.assertEqual(
            writes,
            [("issues/7/artifacts", "POST", {"type": "generic", "name": "Concurro audit state", "body": expected})],
        )
        self.assertEqual(self.cp.audit["body"], expected)
        self.assertEqual(Checkpoints(self.gh, self.config, 7).load([]), state)

    def test_report_literal_bodies_and_multipart_round_trip(self):
        """Single and multi-part bodies retain JSON whitespace, escapes, names and fences."""
        report = {"summary": "é```", "verdict": "approve"}
        record = '{\n  "references": [],\n  "value": {\n    "summary": "\\u00e9```",\n    "verdict": "approve"\n  }\n}'
        key = digest(report)
        for chunk, parts, fences in [(16000, [record], ["````"]), (60, [record[:60], record[60:]], ["````", "```"])]:
            with self.subTest(chunk=chunk):
                self.setUp()
                store = self.store()
                with patch("report_store.CHUNK", chunk):
                    self.assertEqual(store.put(report), key)
                numbers = store.numbers[key]
                expected = [
                    f"<!-- concurro:report-data:v1:{key}:{i}:{len(parts)} -->\n"
                    f"### Report data · part {i} of {len(parts)}\n\n{fence}"
                    + ("json" if len(parts) == 1 else "text")
                    + f"\n{part}\n{fence}\n"
                    for i, (part, fence) in enumerate(zip(parts, fences), 1)
                ]
                self.assertEqual([self.gh.artifacts[7][n]["artifact"]["body"] for n in numbers], expected)
                self.assertEqual(
                    [self.gh.artifacts[7][n]["artifact"]["name"] for n in numbers],
                    [f"Concurro review data {key[:32]} part {i}" for i in range(1, len(parts) + 1)],
                )
                self.assertEqual(self.store().get(key, numbers), report)

    def test_annotation_partition_and_nested_round_trip(self):
        """All seven annotation fields detach, while unknown report fields remain evidence."""
        annotations = {
            "classification": {"complexity": "high"},
            "agent": {"profile": "a"},
            "head": "h",
            "model": "m",
            "input_revision": 2,
            "sequence": 3,
            "finding_ids": ["F1"],
        }
        core = {"decision": "approve", "rationale": "e" * 13000, "risk": "low", "extra": "evidence"}
        report = {**core, **annotations}
        store = self.store()
        record = store.encode(report)
        self.assertEqual(record, {"report": {"digest": digest(core), "artifacts": [1]}, "annotations": annotations})
        self.assertEqual(store.get(digest(core)), core)
        value = {"qc": {"reviewer": report}, "calls": 9}
        compact, refs = report_store.packed(value)
        self.assertEqual(compact, {"qc": {"reviewer": None}, "calls": 9})
        self.assertEqual(
            refs,
            [
                {
                    "path": ["qc", "reviewer"],
                    "digest": digest(core),
                    "value": core,
                    "select": [],
                    "annotations": annotations,
                }
            ],
        )
        key = store.put(value)
        self.assertEqual(self.store().get(key, store.numbers[key]), value)
        self.assertEqual(report, {**core, **annotations})

    def test_artifact_kind_fallback(self):
        """All non-generic kinds retain plan metadata, including unhashable incoming values."""
        for kind in ("generic", "plan", "future", None, []):
            with self.subTest(kind=kind):
                self.setUp()
                name, marker = (
                    ("Concurro audit state", AUDIT)
                    if kind == "generic"
                    else ("Concurro implementation plan", "<!-- concurro:plan:v1 -->")
                )
                item = self.cp.write(None, kind, marker + "\nbody")
                self.assertEqual((item["name"], item["type"]), (name, kind))
                self.assertEqual(self.cp.read(item["number"], kind)["body"], marker + "\nbody")
                # The marker alone must discover it even when its listing name is different.
                listing = self.gh.pages("issues/7/artifacts")
                listing[0]["name"] = "Different listing name"
                self.assertEqual(self.cp.discover(listing, kind)["number"], item["number"])

    def test_comments_use_user_and_duplicate_authoritative_comments_fail(self):
        """Creator and actor cannot impersonate a comment's user; duplicates never select a winner."""
        body = '<!-- concurro:v1 -->\n```json\n{"issue": 7, "version": 2}\n```'
        ours = {"id": 1, "user": {"login": self.bot}, "creator": {"login": "foreign"}, "body": body}
        foreign = {
            "id": 2,
            "user": {"login": "foreign"},
            "creator": {"login": self.bot},
            "actor": {"login": self.bot},
            "body": body,
        }
        self.assertEqual(self.cp.load([foreign, ours]), {"issue": 7, "version": 2})
        self.assertEqual(self.cp.comment, ours)
        with patch.object(self.gh, "pages") as pages, self.assertRaises(CheckpointError) as caught:
            Checkpoints(self.gh, self.config, 7).load([ours, {**ours, "id": 3}])
        self.assertEqual(caught.exception.code, "duplicate_comments")
        pages.assert_not_called()

    def test_artifact_duplicates_fail_and_foreign_creator_is_ignored(self):
        """Artifact discovery uses creator; two authoritative matches are an error."""
        body = AUDIT + '\n```json\n{"repository":"owner/repo","state":{"version":3,"issue":7}}\n```\n'
        first = self.create(body, "Concurro audit state")
        foreign = self.create(body, "Concurro audit state")
        listing = [first, {**foreign, "creator": {"login": "foreign"}, "user": {"login": self.bot}}]
        self.assertEqual(self.cp.discover(listing, "generic")["number"], first["number"])
        with self.assertRaises(CheckpointError) as caught:
            self.cp.discover([first, foreign], "generic")
        self.assertEqual(caught.exception.code, "duplicate_artifacts")
        store = self.store()
        store.items = [first, {**foreign, "creator": {"login": "foreign"}, "user": {"login": self.bot}}]
        self.assertEqual(store.matches(AUDIT), [first])

    def test_first_archive_digest_wins_and_invalid_first_does_not_fall_through(self):
        """Collection order, not artifact number or a later valid copy, selects reused archives."""
        report = {"verdict": "approve", "summary": "ok", "classification": {"complexity": "low"}}
        body = "<!-- concurro:event:v1:old -->\n```json\n" + json.dumps(report) + "\n```"
        first, second = self.create(body, "First"), self.create(body, "Second")
        core = {"verdict": "approve", "summary": "ok"}
        for value in (report, core):
            store = self.store()
            store.items = [second, first]
            key = store.put(value)
            self.assertEqual(store.numbers[key], [second["number"]])
            self.assertEqual(self.store().get(key, store.numbers[key]), value)
        self.gh.artifacts[7][second["number"]]["versions"][0]["actor"] = {"login": "writer"}
        store = self.store()
        store.items = [second, first]
        with self.assertRaisesRegex(ReportDataError, "^Stored review data changed; restore it before continuing.$"):
            store.put(core)
        self.assertEqual(len(self.gh.artifacts[7]), 2)

    def test_human_plan_repaired_but_audit_and_report_edits_refused(self):
        """Generated views may be repaired without granting human-authored evidence authority."""
        state = {
            "version": 2,
            "issue": 7,
            "plan_id": "p",
            "base": "base",
            "plan": {"summary": "Summary", "questions": [], "files": [], "steps": [], "tests": []},
        }
        self.cp.save(state)
        plan_number = self.cp.plan["number"]
        original = self.cp.plan["body"]
        self.human_edit(plan_number)
        fresh = Checkpoints(self.gh, self.config, 7)
        self.assertEqual(fresh.load([]), state)
        fresh.save(state)
        self.assertEqual(fresh.plan["body"], original)
        self.assertEqual(self.gh.artifacts[7][plan_number]["versions"][1]["actor"], {"login": "writer"})
        self.human_edit(self.cp.audit["number"])
        with self.assertRaises(CheckpointError) as caught:
            Checkpoints(self.gh, self.config, 7).load([])
        self.assertEqual(caught.exception.code, "artifact_identity")
        store = self.store()
        key = store.put({"summary": "evidence"})
        self.human_edit(store.numbers[key][0])
        with self.assertRaisesRegex(ReportDataError, "^Malformed or incomplete review data.$"):
            self.store().get(key, store.numbers[key])

    def test_health_notice_user_filter_and_duplicate_rejection(self):
        """Health display ignores creator impersonation and never overwrites duplicate bot notices."""
        body = "<!-- concurro:checkpoint-health:v1 -->\nold"
        foreign = {"id": 1, "user": {"login": "foreign"}, "creator": {"login": self.bot}, "body": body}
        ours = {"id": 2, "user": {"login": self.bot}, "creator": {"login": "foreign"}, "body": body}
        gh = Mock()
        gh.pages.return_value = [foreign, ours]
        checkpoint_health.sync_checkpoint_health(gh, self.config, 7, False)
        gh.repo.assert_called_once_with(
            "issues/comments/2",
            "PATCH",
            {
                "body": (
                    "<!-- concurro:checkpoint-health:v1 -->\n## Concurro checkpoint recovered\n\n"
                    "The controller verified checkpoint access and integrity and completed its invocation.\n"
                )
            },
        )
        gh.repo.reset_mock()
        gh.pages.return_value = [ours, {**ours, "id": 3}]
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            checkpoint_health.sync_checkpoint_health(gh, self.config, 7, False)
        gh.repo.assert_not_called()
        self.assertEqual(err.getvalue(), "Concurro checkpoint health display could not be fully synchronized.\n")

    def test_checkpoint_graft_errors_and_fetch_order(self):
        """Occupied and malformed slots fail before fetch; storage errors retain their public code."""
        for qc, path in [
            ({"nested": ["occupied"]}, ["qc", "nested", 0]),
            ({}, ["qc", "missing"]),
            ({"nested": []}, ["qc", "nested", 0]),
        ]:
            with self.subTest(qc=qc):
                state = {"version": 3, "issue": 7, "qc": qc}
                with patch.object(self.cp.reports, "get") as get, self.assertRaises(CheckpointError) as caught:
                    self.cp.decode(self.payload(state, [self.reference(path)]))
                self.assertEqual(caught.exception.code, "checkpoint_format")
                get.assert_not_called()
        state = {"version": 3, "issue": 7, "qc": {"nested": [None]}}
        ref = self.reference(["qc", "nested", 0])
        with patch.object(self.cp.reports, "get", side_effect=ReportDataError("missing")) as get:
            with self.assertRaises(CheckpointError) as caught:
                self.cp.decode(self.payload(state, [ref]))
            self.assertEqual(caught.exception.code, "report_data")
            get.assert_called_once_with("a" * 64, [1])
        with patch.object(self.cp.reports, "get", return_value={"value": "evidence"}) as get:
            result = self.cp.decode(self.payload(state, [{**ref, "select": ["value"]}]))
        self.assertEqual(result, {"version": 2, "issue": 7, "qc": {"nested": ["evidence"]}})

    def test_checkpoint_specific_overlap_and_annotation_checks(self):
        """Path overlap is checked before a second read, even if the first graft restored None."""
        state = {"version": 3, "issue": 7, "qc": {"nested": None}}
        ref = self.reference(["qc", "nested"])
        with patch.object(self.cp.reports, "get", return_value=None) as get:
            with self.assertRaises(CheckpointError) as caught:
                self.cp.decode(self.payload(state, [ref, ref]))
            self.assertEqual(caught.exception.code, "checkpoint_format")
            get.assert_called_once_with("a" * 64, [1])
        for bad_ref in [self.reference(["calls"]), {**ref, "annotations": {"verdict": "approve"}}]:
            with self.subTest(ref=bad_ref), patch.object(self.cp.reports, "get") as get:
                with self.assertRaises(CheckpointError) as caught:
                    self.cp.decode(self.payload(state, [bad_ref]))
                self.assertEqual(caught.exception.code, "checkpoint_format")
                get.assert_not_called()
        with patch.object(self.cp.reports, "get", return_value={"classification": {}}) as get:
            with self.assertRaises(CheckpointError) as caught:
                self.cp.decode(self.payload(state, [{**ref, "annotations": {"classification": {}}}]))
            self.assertEqual(caught.exception.code, "checkpoint_format")
            get.assert_called_once()

    def test_report_graft_checks_before_nested_fetch(self):
        """Store.load keeps its own overlap message and performs no nested read for occupied slots."""
        ref = {"path": ["nested", 0], "digest": "b" * 64, "artifacts": [99]}
        for slot, expected in [("occupied", "Overlapping report references."), (None, None)]:
            with self.subTest(slot=slot):
                restored = {"nested": ["restored"]}
                key = digest(restored)
                record = {"value": {"nested": [slot]}, "references": [ref]}
                body = (
                    f"<!-- concurro:report-data:v1:{key}:1:1 -->\n### Report data · part 1 of 1\n\n"
                    "```json\n" + json.dumps(record) + "\n```\n"
                )
                item = self.create(body)
                store = self.store()
                with patch.object(store, "get", return_value="restored") as get:
                    if expected:
                        with self.assertRaises(ReportDataError) as caught:
                            store.load(key, [item["number"]])
                        self.assertEqual(str(caught.exception), expected)
                        get.assert_not_called()
                    else:
                        self.assertEqual(store.load(key, [item["number"]]), restored)
                        get.assert_called_once_with("b" * 64, [99])

    def test_rejected_output_fence_selection_literal_bodies(self):
        """Shortest fence wins for rejected output; equal lengths choose backticks."""
        for output, fence in [
            ("plain", "```"),
            ("``", "```"),
            ("```", "~~~"),
            ("`````", "~~~"),
            ("~~", "```"),
            ("~~~", "```"),
            ("~~~~~", "```"),
            ("```~~", "~~~"),
            ("`````~~~", "~~~~"),
            ("````````~~~~~", "~~~~~~"),
            ("```~~~", "````"),
            ("`````~~~~~", "``````"),
        ]:
            with self.subTest(output=output):
                draft = {"output": output, "reason": "Invalid shape", "truncated": False}
                self.assertEqual(
                    self.cp.plan_body({"plan_draft": draft}),
                    PLAN_HEADER
                    + (
                        "## Rejected draft — not approved\n\nInvalid shape\n\n"
                        "The planner is asked to correct this output; it does not authorize implementation.\n\n"
                        f"{fence}json\n{output}\n{fence}\n"
                    ),
                )
                event = {
                    "id": "event",
                    "sequence": 1,
                    "kind": "intake_rejection",
                    "source": ["draft"],
                    "details": {"round": "attempt 1"},
                }
                self.assertEqual(
                    conversation.archive(event, {"draft": draft}),
                    (
                        "<!-- concurro:event:v1:event -->\n### ⚠️ Intake output rejected\n\nInvalid shape\n\n"
                        "Rejected output — not an accepted intake decision.\n\n"
                        f"<sub>Report 1 · attempt 1</sub>\n\n{fence}json\n{output}\n{fence}"
                    ),
                )

    def test_report_archive_uses_backticks_and_inline_code_stays_distinct(self):
        """Accepted report archives never switch to tildes, and inline spans have no three-character minimum."""
        for text, fence, inline in [
            ("plain", "```", "`plain`"),
            ("``", "```", "``` `` ```"),
            ("```", "````", "```` ``` ````"),
            ("`````", "``````", "`````` ````` ``````"),
        ]:
            with self.subTest(text=text):
                event = {"id": "event", "sequence": 2, "kind": "review", "source": ["report"], "details": {}}
                with patch("conversation.render_v2", return_value=("Report", "", ())):
                    body = conversation.archive(event, {"report": {"evidence": text}})
                self.assertEqual(
                    body,
                    "<!-- concurro:event:v1:event -->\n### Report\n\n"
                    f'<sub>Report 2</sub>\n\n{fence}json\n{{\n  "evidence": "{text}"\n}}\n{fence}',
                )
                self.assertEqual(markup.code(text), inline)
