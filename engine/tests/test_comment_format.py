"""Comment format: format 1 renders byte for byte forever, format 2 is stable, and model text stays inert."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule, tearDownModule  # unittest module fixtures
import format_samples
from test_findings import FINDING, Harness, Reviewers, report
from test_pipeline import FakeAgents
import conversation
from controller import SUMMARY_LIMIT
import markup
from policy import Blocked, validate_pr_review, validate_qc
import reviews

GOLDENS = Path(__file__).parent / "goldens"
ENGINE_TAG = re.compile(r"<(/?(details|summary|sub)>|!-- concurro:)")


def outside_code(rendered):
    """The text GitHub parses as Markdown and HTML: everything outside fenced code and code spans."""
    lines, fence = [], None
    for line in rendered.split("\n"):
        match = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line[match.end():].strip():
                fence = None
            continue
        if match:
            fence = match[1]
            continue
        # A backslash-escaped character is literal; only then do backtick runs delimit code spans.
        line = re.sub(r"\\[!-/:-@\[-`{-~]", "", line)
        lines.append(re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", line))
    return "\n".join(lines), fence


def parser():
    """An installed CommonMark parser's HTML nodes for a document, or None without one."""
    if importlib.util.find_spec("markdown_it"):
        from markdown_it import MarkdownIt

        def html(document):
            tokens = MarkdownIt("commonmark").parse(document)
            nodes = [t for t in tokens if t.type == "html_block"]
            nodes += [c for t in tokens if t.children for c in t.children if c.type == "html_inline"]
            return [n.content for n in nodes]
        return html
    if importlib.util.find_spec("commonmark"):
        import commonmark

        def html(document):
            walker = commonmark.Parser().parse(document).walker()
            return [node.literal for node, entering in walker if entering and node.t in ("html_block", "html_inline")]
        return html
    return None


def assert_structure(test, rendered):
    """Only engine tags open HTML, every collapsed section closes, and no fence stays open."""
    parsed, fence = outside_code(rendered)
    test.assertIsNone(fence, "a code fence is left open")
    for match in re.finditer("<", parsed):
        test.assertRegex(parsed[match.start():], ENGINE_TAG, "model text opened HTML")
    test.assertEqual(parsed.count("<details>"), parsed.count("</details>"))


class LegacyFormatTests(unittest.TestCase):
    """Format 1 is the pre-change renderer; its goldens were generated from the committed code."""

    def test_format1_renders_byte_for_byte(self):
        expected = json.loads((GOLDENS / "format1.json").read_text())
        actual = {f"event/{k}": v for k, v in format_samples.event_cases(conversation, 1).items()}
        actual.update({f"pr/{k}": v for k, v in format_samples.publication_cases(reviews, 1).items()})
        self.assertEqual(actual, expected)


class Format2Tests(unittest.TestCase):
    """Format 2 is fixed once published: its goldens change only with a new format."""

    def cases(self):
        cases = {f"event/{k}": v for k, v in format_samples.event_cases(conversation, 2).items()}
        cases.update({f"pr/{k}": v for k, v in format_samples.publication_cases(reviews, 2).items()})
        return cases

    def test_format2_goldens(self):
        path = GOLDENS / "format2.json"
        if not path.exists():
            path.write_text(json.dumps(self.cases(), indent=2, ensure_ascii=False, sort_keys=True) + "\n")
            self.fail("format 2 goldens written; review and rerun")
        self.assertEqual(self.cases(), json.loads(path.read_text()))

    def test_model_text_cannot_change_the_structure(self):
        for name, rendered in self.cases().items():
            with self.subTest(name=name):
                assert_structure(self, rendered)

    def test_review_leads_with_its_outcome(self):
        review = self.cases()["pr/review"]
        lines = review.split("\n")
        self.assertEqual(lines[1], "### 🛠️ Concurro review · claude · Changes requested")
        self.assertEqual(lines[3], "Two defects remain; one earlier finding is fixed.")
        rows = [line for line in lines if line.startswith("| ") and "**F" in line]
        self.assertEqual([r.split(" | ")[0] for r in rows], ["| ❗ High", "| ℹ️ Low"], "ordered by severity")
        self.assertIn("[F1](https://github.com/o/r/pull/9#discussion_r101) ✅ Fixed", review)
        self.assertIn(markup.BLOCKING_NOTE, review)
        self.assertTrue(lines[-1].startswith("<sub>claude / m · head `ddddddd`"))

    def test_finding_titles_are_plain_text(self):
        review = self.cases()["pr/review"]
        self.assertIn(r"Missing \*changelog\* \[fragment\]\(x\)", review)
        self.assertIn("❗ **Concurro · High · F3 · Missing", reviews.finding_body(
            {**format_samples.FINDING, "id": "F3", "provider": "claude", "severity": "high",
             "title": "Missing *changelog* [fragment](x)", "format": 2}))


class MarkupTests(unittest.TestCase):
    """Serializers: titles and headlines are plain text, cells cannot break tables, blocks cannot open HTML."""

    def test_plain_text(self):
        cases = {"Vec<u8> length is not validated": "Vec&lt;u8&gt; length is not validated",
                 "![status](https://e.com/b.png)": r"\!\[status\]\(https://e.com/b.png\)",
                 "*em* _u_ `code` ~s~ a|b": r"\*em\* \_u\_ \`code\` \~s\~ a\|b",
                 "# Heading": r"\# Heading", "- item": r"\- item", "+ item": r"\+ item", "---": r"\---",
                 "> quote": "&gt; quote", "12. step": r"12\. step", "line\nbreak\t here": "line break here"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(markup.text(raw), expected)
        self.assertEqual(markup.text("x" * 200, markup.TITLE_LIMIT), "x" * 99 + "…")

    def test_cells_and_links(self):
        self.assertEqual(markup.code("src/a|b`c.rs", cell=True), r"``src/a\|b`c.rs``")
        self.assertEqual(markup.code("`x"), "`` `x ``")
        table = markup.table(["Where"], [[markup.code("a|b", cell=True)]])
        self.assertEqual(table.split("\n")[2], r"| `a\|b` |")
        self.assertEqual(markup.link("PR #9", "https://github.com/o/r/pull/9)x"),
                         "[PR #9](https://github.com/o/r/pull/9%29x)")

    def test_blocks_are_inert(self):
        hostile = ["</details>", "<summary>x</summary>", "<!-- open", "<?php", "<div>\nraw", "<script>x</script>",
                   "```\nunclosed", "````rust\nlet x = 1;\n```\nstill code", "~~~\ntilde", "~~~~\n~~~\nnested",
                   "# Heading\n\n---\n\n* list", "text `Vec<u8>` and <b>bold</b>",
                   "\\`</details><details><summary>Injected section</summary>`", "a \\\\`<b>` b \\`<i>"]
        for raw in hostile:
            with self.subTest(raw=raw):
                parsed, fence = outside_code(markup.block(raw))
                self.assertNotIn("<", parsed, "model text opened HTML")
                self.assertIsNone(fence)
                rendered = markup.details("Full review", markup.block(raw)) + "\n\n" + markup.sub("meta")
                assert_structure(self, rendered)
                self.assertTrue(rendered.endswith("<sub>meta</sub>"))
        self.assertIn("`Vec<u8>`", markup.block("text `Vec<u8>` here"), "code spans are untouched")
        self.assertIn("let x: Vec<u8>;", markup.block("```rust\nlet x: Vec<u8>;\n```"), "fenced code is untouched")

    @unittest.skipUnless(parser(), "no CommonMark parser installed (markdown-it-py or commonmark)")
    def test_parser_sees_only_engine_html(self):
        """With a real parser: every HTML node in a rendered comment is one the engine wrote."""
        html = parser()
        hostile = ["\\`</details><details><summary>Injected section</summary>`", "<!-- open", "<?php x", "<div>\nraw",
                   "```\nunclosed", "~~~~\n~~~\nnested", "text `Vec<u8>` and <b>bold</b>", "a \\\\`<b>` b \\`<i>"]
        documents = [markup.details("Full review", markup.block(raw)) + "\n\n" + markup.sub("meta") for raw in hostile]
        documents += list(Format2Tests().cases().values())
        for document in documents:
            with self.subTest(document=document[:80]):
                for node in html(document):
                    self.assertRegex(node.strip(), r"^(<(/?(details|summary|sub)>)|<!-- concurro:)", node)

    def test_headline_fallback(self):
        cases = {"The whole plan holds firm. Step 2 is fine.": "The whole plan holds firm.",
                 "Too short. The rest of it follows here.": "Too short. The rest of it follows here.",
                 "Dockerfile:12. Next it builds.": "Dockerfile:12. Next it builds.",
                 "no sentence break here": None, "x" * 300 + ". More.": None,
                 "# Heading text that is long enough. Rest.": r"\# Heading text that is long enough."}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(markup.headline({"rationale": raw}, "rationale"), expected)
        self.assertEqual(markup.headline({"headline": "Use <this>\nline.", "rationale": "x"}, "rationale"),
                         "Use &lt;this&gt; line.")


class ContractTests(unittest.TestCase):
    """Presentation fields are optional to validators and never a reason to reject."""

    def test_presentation_fields(self):
        finding = {**FINDING, "severity": "high", "title": "Vec<u8> | `bad` # title\nsecond line"}
        validate_pr_review(report("changes_requested", [finding]) | {"headline": "<b>x</b>"}, [], [])
        validate_pr_review(report("changes_requested", [FINDING]), [], [])
        with self.assertRaisesRegex(Blocked, "severity"):
            validate_pr_review(report("changes_requested", [{**finding, "severity": "blocker"}]), [], [])
        with self.assertRaisesRegex(Blocked, "presentation"):
            validate_pr_review(report("changes_requested", [FINDING]) | {"headline": 1}, [], [])
        qc = {"decision": "proceed", "rationale": "Fine.", "risk": "low", "questions": [], "concerns": [],
              "scope": {"goal_ids": ["G-USABILITY"], "fit": "Fits.", "conflicts": []}}
        validate_qc(qc | {"headline": "Proceed."}, "intake")
        validate_qc(qc, "intake")


class ArchiveNameTests(unittest.TestCase):
    """Archive names follow GitHub's artifact name rules, which rejected the first live archive."""

    RULE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9])?")

    def test_names_follow_githubs_rules(self):
        for index in ({"step": "Design review", "provider": "claude", "round": "1 cooperative"},
                      {"step": "Code review", "provider": "c·d<x>", "round": "2"},
                      {"step": "Intake", "provider": "codex", "round": "1" + "9" * 300}):
            with self.subTest(index=index):
                name = conversation.archive_name({"sequence": 12, "index": index})
                self.assertRegex(name, self.RULE)
                self.assertLessEqual(len(name), conversation.ARTIFACT_NAME_LIMIT)


class SizeTests(unittest.TestCase):
    """Rendered comments stay within their bounds."""

    def v2(self, state, kind, source, details):
        """A format 2 event as a pending pre-change event renders it."""
        return conversation.content_v2({"id": "e" * 64, "sequence": 3}, *conversation.render_v2(state, kind, source, details))

    def test_oversized_format2_event_drops_collapsed_sections(self):
        state, source, details = format_samples.qc_state()
        state["qc"]["plan_qc"]["objection_updates"] = [
            {"id": f"O{i}", "status": "open", "detail": "d" * 600, "evidence": "e_" * 250,
             "counterexample": "x*" * 150, "closure": "y[" * 200} for i in range(24)]
        body = self.v2(state, "qc", source, format_samples.v2_details(details, disposition_ids=[f"O{i}" for i in range(24)]))
        self.assertLessEqual(len(body.encode()), conversation.EVENT_LIMIT)
        self.assertIn(conversation.OMITTED, body)
        self.assertIn("| `O23` | Objection | 📌 Open |", body)

    def test_long_expanded_format2_text_is_dropped(self):
        """Text shown without a headline goes too: an approval's summary, a review's, a design issue's."""
        status = {"attempt": 0, "local_commit": "c" * 40, "checks": [],
                  "review": {"verdict": "approve", "summary": "<" * 12700, "findings": [], "design_issue": None}}
        body = self.v2({"issue": 7, "local_status": status}, "local", ("local_status",), {"reviewer": "claude"})
        self.assertLessEqual(len(body.encode()), conversation.EVENT_LIMIT)
        self.assertIn(conversation.OMITTED, body)
        self.assertIn("**✅ Local review · claude · Approve**", body)
        state, source, details = format_samples.review_state()
        state["reviews"]["claude"].update(headline="", summary="<" * 20000)
        body = self.v2(state, "review", source, format_samples.v2_details(details))
        self.assertLessEqual(len(body.encode()), conversation.EVENT_LIMIT)
        self.assertIn("[Review on PR #9]", body)

    def test_archive_keeps_the_report_verbatim(self):
        """An archive holds the whole report as JSON, in a fence no report text can close."""
        state, source, details = format_samples.review_state()
        state["reviews"]["claude"]["summary"] = "<" * 20000 + "\n````\n</details>"
        conversation.queue(state, None, source=source, kind="review", details=format_samples.v2_details(details))
        from test_pipeline import FakeGitHub
        from report_store import Store
        gh = FakeGitHub(CONFIG)
        conversation.publish(state, gh, CONFIG)
        receipt = state['conversation']['receipts']['/'.join(source)]
        data = receipt['data']
        restored = Store(gh, 7, CONFIG['bot_login']).get(data['digest'], data['artifacts'])
        self.assertEqual(restored, state['reviews']['claude'])
        for item in gh.artifacts[7].values():
            body = item['artifact']['body']
            self.assertLessEqual(len(body.encode()), conversation.ARCHIVE_LIMIT)
            assert_structure(self, body)

    def test_largest_review_fits_a_github_review(self):
        findings = [{**FINDING, "severity": "low", "title": "t" * 100, "detail": "*_[" * 360} for _ in range(20)]
        large = report("changes_requested", findings) | {"summary": "<" * 1000}
        self.assertLessEqual(len(json.dumps(large)), 24000 * 2)
        state = {"issue": 7, "pr": 9, "findings": [{"id": f"F{i + 1}", "provider": "claude", **f, "status": "open",
                                                    "format": 2} for i, f in enumerate(findings)]}
        item = {"provider": "claude", "sequence": 1, "head": "d" * 40, "format": 2}
        for summary in ("<" * 1000, "<" * 23000):
            with self.subTest(summary=len(summary)):
                body = reviews.review_body(state, {"repository": "o/r"}, item, large | {"model": "m", "summary": summary},
                                           state["findings"], {f["id"]: "body" for f in state["findings"]})
                self.assertLessEqual(len(body), reviews.REVIEW_BODY_LIMIT)
                self.assertEqual(reviews.OMITTED in body, len(summary) > 1000)
                self.assertIn("| ℹ️ Low | **F20**", body, "the findings table always stays")


class StatusSizeTests(Harness, unittest.TestCase):
    """The status comment keeps its status when its narrative would exceed GitHub's limit."""

    def test_status_comment_stays_small_with_huge_reports(self):
        """Reports live in their archives; the status comment carries at most a shortened reason."""
        controller = self.run_to(Reviewers(set()), "gates")
        for review in controller.state["reviews"].values():
            review["summary"] = "<" * 17000
        controller.state["reason"] = "autonomous merge is disabled; " + "<" * 20000
        body = controller.summary_body()
        self.assertLess(len(body.encode()), 8000)
        self.assertNotIn("<" * 400, body)
        self.assertIn("| Merge |", body)

    def test_oversized_status_comment_drops_historical_rows(self):
        controller = self.run_to(Reviewers(set()), "gates")
        controller.state["reports"] = [{"step": "Code review", "round": "x" * 2000 + str(i), "provider": "claude",
                                        "outcome": "approve", "artifact": i} for i in range(40)]
        body = controller.summary_body()
        self.assertLessEqual(len(body.encode()), SUMMARY_LIMIT)
        self.assertNotIn("↳ Round", body)
        self.assertIn("| Merge |", body)


class UpgradeTests(Harness, unittest.TestCase):
    """A review queued in format 1 before the upgrade publishes in format 1, once; later ones use format 2."""

    def test_format1_item_recovers_after_upgrade(self):
        agents = Reviewers({"claude"}, findings={"claude": [{**FINDING, "line": 1}]})
        # Before the upgrade: the review is recorded and queued in format 1, but not yet published.
        with patch.object(reviews, "FORMAT", 1), patch.object(reviews, "publish", return_value=False):
            controller = self.run_to(agents, "review", until=lambda state: state.get("pr_outbox"))
        # A checkpoint written before the upgrade has no format fields at all.
        checkpoints, legacy = self.load()
        for entry in legacy["pr_outbox"] + legacy["findings"]:
            entry.pop("format")
            entry.pop("opening_digest", None)
        checkpoints.save(legacy)
        # After it: published by the new code, with its first response lost.
        original, lost = self.gh.repo, []

        def lose(path, method="GET", data=None):
            result = original(path, method, data)
            if method == "POST" and path.endswith("/reviews") and not lost:
                lost.append(path)
                raise RuntimeError("lost response")
            return result

        with patch.object(self.gh, "repo", side_effect=lose):
            state = self.run_to(agents, "gates").state
        self.assertTrue(lost)
        first = [r for r in self.gh.app_reviews if r["body"].startswith(f"{reviews.REVIEW_MARKER}{CONFIG['repository']}#7:1 -->")]
        self.assertEqual(len(first), 1)
        self.assertIn("### claude / ", first[0]["body"], "published in format 1")
        [opening] = [c for c in self.gh.review_comments if c["body"].startswith(f"{reviews.FINDING_MARKER}F1 -->")]
        self.assertTrue(opening["body"].startswith(reviews.finding_prefix(state["findings"][0])))
        [reply] = [c for c in self.gh.review_comments if c["body"].startswith(f"{reviews.DISPOSITION_MARKER}F1:")]
        self.assertIn("✅ **Fixed** in `", reply["body"], "the format 2 review answers a format 1 finding")
        self.assertEqual(state["findings"][0]["resolution"], "done")


if __name__ == "__main__":
    unittest.main()
