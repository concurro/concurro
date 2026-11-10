"""Publication bytes and ordering pinned before the 09b helper extraction."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import CONFIG

import conversation
import format_samples
import report_store
import reviews
from fakes import FakeGitHub
from policy import Blocked, digest
from readiness import ci_reasons

GOLDEN = Path(__file__).parent / "goldens/rendering-helpers.json"


def samples():
    """Small complete sources for every current archived conversation kind."""
    cases = {}
    for kind, sample in (
        ("plan_summary", format_samples.plan_state()),
        ("qc_design", format_samples.qc_state()),
        ("review", format_samples.review_state()),
    ):
        state, source, details = sample
        cases[kind] = (state, source, format_samples.v2_details(details))
    state, source, details = deepcopy(cases["qc_design"])
    state["qc"]["plan_qc"].update(decision="proceed", concerns=[], objection_updates=[])
    details.update(phase="intake", plan="intake", disposition_ids=[])
    cases["qc_intake"] = (state, source, details)
    state, source, details = deepcopy(cases["qc_design"])
    details["diagnostic"] = {"code": "review_shape", "detail": "Missing `sources` <field>."}
    cases["review_rejection"] = (state, source, details)
    cases["implementation"] = (
        {"issue": 7, "result": {"summary": "Updated `read()`.", "validation": "Two tests passed."}},
        ("result",),
        {"round": 1, "provider": "claude"},
    )
    return cases


class TracedGitHub(FakeGitHub):
    """Record requests and the receipts visible immediately before each request."""

    def __init__(self, state):
        super().__init__(CONFIG)
        self.state, self.trace = state, []

    def repo(self, path, method="GET", data=None):
        self.trace.append(
            [path, method, deepcopy(data), deepcopy(self.state.get("conversation", {}).get("receipts", {}))]
        )
        return super().repo(path, method, data)


def conversation_case(name, sample, version, multipart=False, lost_reply=False):
    """Publish actual report artifacts, retaining the exact wire bodies and receipts."""
    state, source, details = deepcopy(sample)
    kind = "qc" if name.startswith("qc_") else name
    value = conversation.source_value(state, source)
    event = {
        "id": name + "-" + str(version),
        "title": name,
        "sequence": 3,
        "at": "2026-11-08T12:00:00+00:00",
        "source": list(source),
        "kind": kind,
        "details": details,
        "format": version,
        "index": {"step": name, "provider": "claude", "round": "1"},
    }
    if version == 4:
        event["content_digest"] = digest(value)
    elif version == 3:
        event["content_digest"] = digest(conversation.archive(event, state))
    else:
        event["content_digest"] = digest(
            conversation.content_v2(event, *conversation.render_v2(state, kind, source, details))
        )
    state["conversation"] = {"pending": [event]}
    gh = TracedGitHub(state)
    store = report_store.Store(gh, 7, CONFIG["bot_login"], CONFIG["repository"])
    chunk = 100 if multipart else 16000
    with patch.object(report_store, "CHUNK", chunk):
        if lost_reply:
            original = gh.repo
            failed = False

            def lose(path, method="GET", data=None):
                """Lose the first successful archive response, then permit recovery."""
                nonlocal failed
                result = original(path, method, data)
                if method == "POST" and not failed:
                    failed = True
                    raise RuntimeError("lost reply")
                return result

            with patch.object(gh, "repo", side_effect=lose):
                try:
                    conversation.publish(state, gh, CONFIG, store)
                except conversation.PublicationError:
                    pass
            # A fresh invocation must discover the previous write.
            store = report_store.Store(gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        conversation.publish(state, gh, CONFIG, store)
    return {"trace": gh.trace, "journal": state["conversation"], "reports": state.get("reports", [])}


def review_case():
    """Publish an inline finding, file-level finding, general finding and disposition."""
    state, _, _ = format_samples.review_state()
    report = state["reviews"]["claude"]
    file_finding = {**format_samples.FINDING, "line": 99, "start_line": None, "detail": "Check the file boundary."}
    report["findings"].append(file_finding)
    report["finding_ids"].append("F4")
    state["findings"].append({**file_finding, "id": "F4", "provider": "claude", "status": "open"})
    for finding in state["findings"]:
        finding["text_digest"] = digest(finding["detail"])
        if finding["id"] != "F1":
            finding["format"] = 3
            finding["opening_digest"] = digest(reviews.finding_body(finding))
    gh = TracedGitHub(state)
    gh.pr = {"head": {"sha": "d" * 40}}
    old = state["findings"][0]
    opening = {
        "id": 101,
        "path": old["path"],
        "body": reviews.finding_body(old),
        "user": {"login": CONFIG["bot_login"]},
    }
    gh.review_comments.append(opening)
    gh.thread(opening)
    with patch.object(reviews, "FORMAT", 3):
        reviews.queue_review(state, "claude", report)

    def checkpoint():
        """Pin the state visible at every durable publication boundary."""
        gh.trace.append(["checkpoint", deepcopy(state)])

    with patch.object(
        gh, "pull_files", return_value=[{"filename": "docs/guide.md", "patch": "@@ -1,4 +1,4 @@\n a\n b\n c\n d"}]
    ):
        reviews.publish(state, gh, CONFIG, checkpoint)
    publisher = reviews.Publisher(state, gh, CONFIG, checkpoint)
    try:
        publisher.comment(
            [{"user": {"login": CONFIG["bot_login"]}, "body": "marker old"}], "marker", "unused", "marker new", {}
        )
    except Blocked as error:
        conflict = str(error)
    return {"trace": gh.trace, "comments": gh.review_comments, "reviews": gh.app_reviews, "conflict": conflict}


def snapshots():
    """Collect fixed-input publication and compact-rendering outputs."""
    output = {}
    for name, sample in samples().items():
        for version in (2, 3, 4):
            output[f"{name}/v{version}"] = conversation_case(name, sample, version)
        output[f"{name}/multipart"] = conversation_case(name, sample, 4, multipart=True)
        state, source, details = deepcopy(sample)
        kind = "qc" if name.startswith("qc_") else name
        with patch.object(conversation, "EVENT_LIMIT", 1000):
            output[f"{name}/compact"] = conversation.render_v2(state, kind, source, details)
    output["pr"] = review_case()
    return output


class RenderingContractTests(unittest.TestCase):
    """Goldens plus independent assertions protect text and publication semantics."""

    def test_publication_snapshots(self):
        self.assertEqual(json.dumps(snapshots(), ensure_ascii=False, indent=2) + "\n", GOLDEN.read_text())

    def test_archive_parts_and_recovery(self):
        for name, sample in samples().items():
            with self.subTest(kind=name):
                normal = conversation_case(name, sample, 4, multipart=True)
                recovered = conversation_case(name, sample, 4, multipart=True, lost_reply=True)
                self.assertEqual(recovered["journal"], normal["journal"])
                self.assertEqual(recovered["reports"], normal["reports"])
                writes = [call for call in recovered["trace"] if call[1] == "POST"]
                self.assertEqual(writes, [call for call in normal["trace"] if call[1] == "POST"])
                self.assertGreaterEqual(len(writes), 3)
                self.assertIn("[Full report part 1]", writes[-1][2]["body"])
                self.assertIn("[Full report part 2]", writes[-1][2]["body"])
                self.assertTrue(all(not call[3] for call in writes), "receipt follows all writes")
                receipt = next(iter(normal["journal"]["receipts"].values()))
                self.assertEqual(list(receipt), ["content_digest", "source_digest", "data", "artifact"])
                self.assertGreater(len(receipt["data"]["artifacts"]), 1)

    def test_compact_and_pr_boundaries(self):
        data = snapshots()
        for name in ("plan_summary", "qc_design", "qc_intake", "review"):
            self.assertIn(conversation.OMITTED, data[f"{name}/compact"][1])
        trace = data["pr"]["trace"]
        self.assertEqual(
            [call[0] for call in trace if call[0] == "checkpoint" or call[1] == "POST"],
            [
                "pulls/9/reviews",
                "pulls/9/comments",
                "checkpoint",
                "pulls/9/comments/101/replies",
                "checkpoint",
                "checkpoint",
                "checkpoint",
            ],
        )
        self.assertEqual(
            data["pr"]["conflict"], "PR review publication differs from its recorded content; reconcile manually"
        )
        self.assertIn("<!-- concurro:finding:v1:F4 -->", data["pr"]["comments"][2]["body"])
        self.assertIn("<!-- concurro:review:v1:", data["pr"]["reviews"][0]["body"])

    def test_objection_ids_and_labels(self):
        state, source, details = format_samples.qc_state()
        report = conversation.source_value(state, source)
        report["objection_updates"].append(
            {
                "id": "",
                "status": "open",
                "detail": "New",
                "evidence": "Observed",
                "counterexample": "Edge",
                "closure": "Test",
            }
        )
        report["concerns"].append("New")
        report["questions"] = ["Which boundary?"]
        details["disposition_ids"].append("D2-bbbbbbbb-cooperative-c2")
        self.assertEqual(
            conversation.objections(report, details),
            (
                [],
                [("O2-bbbbbbbb", "Specify the `<limit>` boundary."), ("D2-bbbbbbbb-cooperative-c2", "New")],
                [("D2-bbbbbbbb-cooperative-q1", "Which boundary?")],
            ),
        )
        body = conversation.render(state, "qc", source, details)
        for label in ("O1-bbbbbbbb", "O2-bbbbbbbb", "D2-bbbbbbbb-cooperative-c2"):
            self.assertIn(f"**{label}**", body)
        details.pop("disposition_ids")
        self.assertIn("**New finding**", conversation.render(state, "qc", source, details))
        report.update(decision="approve", concerns=[], questions=[])
        details["resolved"] = ["closed"]
        self.assertEqual(conversation.objections(report, details), (["closed"], [], []))

    def test_mixed_publication_receipt_order(self):
        state, source, details = samples()["qc_design"]
        state["result"] = {"summary": "Implemented.", "validation": "Passed."}
        with patch.object(conversation, "datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-11-08T12:00:00+00:00"
            conversation.queue(
                state, None, source=["result"], kind="implementation", details={"round": 1, "provider": "claude"}
            )
            conversation.queue(state, "Notice", "First comment.")
            conversation.queue(state, None, source=source, kind="qc", details=details)
            conversation.queue(state, "Notice", "Last comment.")
        for event, version in zip(state["conversation"]["pending"], [3, None, 4, None]):
            if version is not None:
                event["format"] = version
                if version == 3:
                    event["content_digest"] = digest(conversation.archive(event, state))
        self.assertEqual([e.get("format") for e in state["conversation"]["pending"]], [3, None, 4, None])
        gh = TracedGitHub(state)
        conversation.publish(state, gh, CONFIG)
        writes = [call for call in gh.trace if call[1] == "POST"]
        self.assertEqual(
            [call[0] for call in writes],
            [
                "issues/7/artifacts",
                "issues/7/comments",
                "issues/7/artifacts",
                "issues/7/artifacts",
                "issues/7/comments",
            ],
        )
        self.assertEqual(
            [list(call[3]) for call in writes], [[], ["result"], ["result"], ["result"], ["result", "qc/plan_qc"]]
        )
        self.assertEqual(list(state["conversation"]["receipts"]), ["result", "qc/plan_qc"])
        self.assertEqual(state["conversation"]["pending"], [])


class CIContractTests(unittest.TestCase):
    """Pin latest-wins precedence, superseded checks and ordered diagnostic output."""

    def test_ci_table(self):
        good = {"id": 2, "name": "test", "head_sha": "a", "status": "completed", "conclusion": "success"}
        failed = {**good, "id": 1, "conclusion": "failure"}
        external = {
            "id": 10,
            "app": {"slug": "external"},
            "head_sha": "a",
            "status": "queued",
            "conclusion": None,
            "latest_check_runs_count": 0,
        }
        run = {
            "id": 2,
            "workflow_id": 1,
            "event": "push",
            "head_branch": "main",
            "name": "Build",
            "head_sha": "a",
            "status": "completed",
            "conclusion": "success",
            "check_suite_id": 12,
        }
        old = {**run, "id": 1, "check_suite_id": 11, "status": "queued"}
        cases = [
            ([], ["CI has no successful result yet"]),
            ([("a", [good, failed], [])], []),
            (
                [("a", [failed, {**failed, "conclusion": "success"}], [])],
                ["CI unsuccessful: test", "CI has no successful result yet"],
            ),
            (
                [("a", [], [{"context": "x", "state": "pending"}, {"context": "x", "state": "success"}])],
                ["CI pending: x", "CI has no successful result yet"],
            ),
            (
                [
                    (
                        "a",
                        [],
                        [{"id": 2, "context": "x", "state": "success"}, {"id": 1, "context": "x", "state": "failure"}],
                    )
                ],
                [],
            ),
            ([("a", [], [], [external], [])], ["CI has no successful result yet"]),
            ([("a", [good], [], [external], [])], []),
            (
                [
                    (
                        "a",
                        [{**failed, "check_suite": {"id": 11}}, {**good, "check_suite": {"id": 12}}],
                        [],
                        [{"id": 11}],
                        [run, old],
                    )
                ],
                [],
            ),
            (
                [("a", [good], [], [{"id": 3}], [{**run, "status": "queued"}])],
                ["CI suite pending: 3", "CI workflow pending: Build"],
            ),
            (
                [
                    (
                        "a",
                        [{**good, "status": "queued"}, {**failed, "name": "lint"}],
                        [{"context": "x", "state": "error"}],
                        [{"id": 3, "head_sha": "a", "status": "completed", "conclusion": "failure"}],
                        [{**run, "conclusion": "failure"}],
                    )
                ],
                [
                    "CI suite unsuccessful: 3",
                    "CI workflow unsuccessful: Build",
                    "CI pending: test",
                    "CI unsuccessful: lint",
                    "CI error: x",
                    "CI has no successful result yet",
                ],
            ),
            ([("a", [failed], []), ("a", [failed], [])], ["CI unsuccessful: test", "CI has no successful result yet"]),
            ([("a", [{**good, "conclusion": "neutral"}], [], [], [run])], ["CI has no successful result yet"]),
        ]
        for observations, expected in cases:
            with self.subTest(observations=observations):
                self.assertEqual(ci_reasons(observations), expected)
        with self.assertRaises(KeyError):
            ci_reasons([("a", [], [], [], [{"workflow_id": 1}])])
