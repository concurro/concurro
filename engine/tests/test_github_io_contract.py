"""Pin GitHub requests and failure order before sharing I/O helpers."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support  # noqa: F401 -- initializes the engine import path

from github import GitHub

THREAD_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              nodes { id isResolved isOutdated }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
REVIEW_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              nodes { id isResolved comments(first:1) { nodes { databaseId } } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
READY_QUERY = """mutation($input:MarkPullRequestReadyForReviewInput!) {
          markPullRequestReadyForReview(input:$input) { pullRequest { id isDraft headRefOid } }
        }"""
RESOLVE_QUERY = """mutation($input:ResolveReviewThreadInput!) {
          resolveReviewThread(input:$input) { thread { id isResolved } }
        }"""


def thread_page(nodes, more=False, cursor=None):
    """One literal GraphQL page shape, independent of the production paginator."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
                }
            }
        }
    }


class GitHubIOContractTests(unittest.TestCase):
    """Exact calls, boundary behaviour and distinct mutation confirmations."""

    def setUp(self):
        """Record adapter requests without invoking gh or GitHub."""
        self.gh = GitHub("owner/repo")
        self.gh.api = Mock()

    def test_outcome_window_uses_multiple_pages_above_one_hundred(self):
        """Operator retrieval budgets are honored beyond GitHub's page capacity."""

        def response(path, *args):
            """Return 150 issues and record each outcome-comment lookup."""
            if "/issues?" in path:
                first = 1 if path.endswith("page=1") else 101
                return [{"number": i} for i in range(first, min(first + 100, 151))]
            return []

        self.gh.api.side_effect = response
        issues = self.gh.outcome_candidates(125)
        self.assertEqual([number for number, _ in issues], list(range(1, 126)))
        pages = [c.args[0] for c in self.gh.api.call_args_list if "/issues?" in c.args[0]]
        self.assertEqual(len(pages), 2)
        self.assertTrue(all("per_page=100" in p for p in pages))

    def test_cli_request_bytes(self):
        """The gh input remains default JSON, including empty and Unicode bodies."""
        for data, body in [(None, None), ({}, "{}"), ([], "[]"), ({"é": "雪"}, '{"\\u00e9": "\\u96ea"}')]:
            with (
                self.subTest(data=data),
                patch(
                    "github.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")
                ) as run,
            ):
                GitHub("owner/repo").repo("issues/7", "PATCH", data)
                argv = ["gh", "api", "--method", "PATCH", "repos/owner/repo/issues/7"]
                if data is not None:
                    argv += ["--input", "-"]
                run.assert_called_once_with(argv, input=body, text=True, capture_output=True, timeout=90)

    def test_rest_pagination_edges(self):
        """Literal artifact suffix, existing queries and keyed responses retain their URLs."""
        for path, size, key in [
            ("issues/7/artifacts", 1, None),
            ("issues", 100, None),
            ("issues/7/artifacts?x=y", 100, None),
            ("actions/runs?x=y", 100, "runs"),
            ("issues", 100, ""),
        ]:
            with self.subTest(path=path, key=key):
                batches = [list(range(size)), ["last"], []]
                self.gh.api.reset_mock()
                self.gh.api.side_effect = [{key: b} if key else b for b in batches]
                self.assertEqual(self.gh.pages(path, key), batches[0] + batches[1])
                sep = "&" if "?" in path else "?"
                self.assertEqual(
                    self.gh.api.call_args_list,
                    [
                        ((f"repos/owner/repo/{path}{sep}per_page={size}&page={p}", "GET", None),)
                        for p in range(1, 4 if size == 1 else 3)
                    ],
                )

    def test_rest_pagination_past_old_bounds(self):
        """A complete listing traverses beyond the old 10,000-entry ceiling."""
        for path, size, pages in [("issues/7/artifacts", 1, 10001), ("issues", 100, 101)]:
            self.gh.api.reset_mock()
            self.gh.api.side_effect = [[0] * size for _ in range(pages)] + [["last"], []]
            result = self.gh.pages(path)
            self.assertEqual(len(result), size * pages + 1)
            self.assertEqual(result[-1], "last")
            self.assertEqual(self.gh.api.call_count, pages + (2 if size == 1 else 1))

    def test_thread_requests_and_mapping(self):
        """Both documents and cursor bodies stay exact; only review_threads maps comments."""
        cases = [
            (
                "threads",
                THREAD_QUERY,
                [{"id": "one", "isResolved": False, "isOutdated": True}],
                [{"id": "one", "isResolved": False, "isOutdated": True}],
            ),
            (
                "review_threads",
                REVIEW_QUERY,
                [
                    {"id": "one", "isResolved": False, "comments": {"nodes": [{"databaseId": 12}]}},
                    {"id": "two", "isResolved": True, "comments": {"nodes": []}},
                ],
                [{"id": "one", "isResolved": False, "comment": 12}, {"id": "two", "isResolved": True, "comment": None}],
            ),
        ]
        for name, query, nodes, expected in cases:
            with self.subTest(name=name):
                self.gh.api.reset_mock()
                self.gh.api.side_effect = [thread_page(nodes, True, "next"), thread_page([])]
                self.assertEqual(getattr(self.gh, name)(7), expected)
                self.assertEqual(
                    self.gh.api.call_args_list,
                    [
                        (
                            (
                                "graphql",
                                "POST",
                                {
                                    "query": query,
                                    "variables": {"owner": "owner", "name": "repo", "number": 7, "cursor": cursor},
                                },
                            ),
                        )
                        for cursor in (None, "next")
                    ],
                )

    def test_thread_mapping_precedes_page_info_and_next_fetch(self):
        """A broken first node fails before even reading a broken pageInfo."""
        page = thread_page([{"id": "broken"}], True, "next")
        page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = None
        self.gh.api.return_value = page
        with self.assertRaises(KeyError) as caught:
            self.gh.review_threads(7)
        self.assertEqual(caught.exception.args, ("comments",))
        self.assertEqual(self.gh.api.call_count, 1)

    def test_thread_errors_and_long_traversal(self):
        """Errors retain their meaning; valid cursors continue past 100 pages."""
        for name in ("review_threads", "threads"):
            self.gh.api.reset_mock(side_effect=True)
            self.gh.api.return_value = {"errors": ["bad"], "data": True}
            with self.assertRaisesRegex(RuntimeError, "^cannot establish review-thread state$"):
                getattr(self.gh, name)(7)
            self.gh.api.reset_mock()
            self.gh.api.side_effect = [thread_page([], True, str(i)) for i in range(101)] + [
                thread_page([], False, None)
            ]
            self.assertEqual(getattr(self.gh, name)(7), [])
            self.assertEqual(self.gh.api.call_count, 102)

    def test_graphql_confirmation_table(self):
        """Readiness raises on nonconfirmation; resolution returns False; their error order differs."""
        ready_error = "GitHub did not confirm PR readiness"
        resolve_error = "review thread resolution failed; reconcile on the next tick"
        for name, query, input_key, wrapper, item_key, flag in [
            ("mark_ready", READY_QUERY, "pullRequestId", "markPullRequestReadyForReview", "pullRequest", "isDraft"),
            ("resolve_thread", RESOLVE_QUERY, "threadId", "resolveReviewThread", "thread", "isResolved"),
        ]:
            ready = name == "mark_ready"
            cases = [
                ({}, RuntimeError if ready else False),
                ({"data": None}, RuntimeError if ready else False),
                ({"data": True, "errors": ["bad"]}, AttributeError if ready else RuntimeError),
            ]
            for ident in ("requested", "wrong"):
                for value in (False, True, 0, 1, None, "true"):
                    item = {"id": ident, flag: value}
                    result = {"data": {wrapper: {item_key: item}}, "errors": []}
                    expected = (
                        (item if value is False else RuntimeError)
                        if ready
                        else (ident == "requested" and value is True)
                    )
                    cases.append((result, expected))
            for response, expected in cases:
                with self.subTest(name=name, response=response):
                    self.gh.api.reset_mock()
                    self.gh.api.return_value = response
                    if isinstance(expected, type):
                        with self.assertRaises(expected) as caught:
                            getattr(self.gh, name)("requested")
                        if expected is RuntimeError:
                            self.assertEqual(str(caught.exception), ready_error if ready else resolve_error)
                    else:
                        self.assertEqual(getattr(self.gh, name)("requested"), expected)
                    self.gh.api.assert_called_once_with(
                        "graphql", "POST", {"query": query, "variables": {"input": {input_key: "requested"}}}
                    )

    def test_label_requests(self):
        """Ensure/create/add/remove order and encoded label names stay exact."""
        root = "repos/owner/repo/"
        get_labels = ((root + "issues/7/labels?per_page=100&page=1", "GET", None),)
        stage = "concurro:stage:review"
        attention = "concurro:attention"
        for kind, current, argument, expected in [
            ("stage", [stage], "review", []),
            (
                "stage",
                ["human", "concurro:stage:z /", "concurro:stage:a"],
                "review",
                [
                    ((root + "labels?per_page=100&page=1", "GET", None),),
                    (
                        (
                            root + "labels",
                            "POST",
                            {
                                "name": stage,
                                "color": "8250df",
                                "description": "Concurro progress display; does not grant execution or merge approval.",
                            },
                        ),
                    ),
                    ((root + "issues/7/labels", "POST", {"labels": [stage]}),),
                    ((root + "issues/7/labels/concurro%3Astage%3Aa", "DELETE", None),),
                    ((root + "issues/7/labels/concurro%3Astage%3Az%20%2F", "DELETE", None),),
                ],
            ),
            (
                "attention",
                [],
                True,
                [
                    ((root + "labels?per_page=100&page=1", "GET", None),),
                    (
                        (
                            root + "labels",
                            "POST",
                            {
                                "name": attention,
                                "color": "d4c5f9",
                                "description": "Concurro checkpoint needs attention; display only.",
                            },
                        ),
                    ),
                    ((root + "issues/7/labels", "POST", {"labels": [attention]}),),
                ],
            ),
            (
                "attention",
                [attention],
                False,
                [
                    ((root + "issues/7/labels/concurro%3Aattention", "DELETE", None),),
                ],
            ),
            ("attention", [attention], True, []),
            ("attention", ["human"], False, []),
        ]:
            with self.subTest(kind=kind, current=current, argument=argument):
                self.gh.known_labels = None
                self.gh.api.reset_mock()
                self.gh.api.side_effect = [[{"name": n} for n in current]] + [[]] * len(expected)
                getattr(self.gh, "sync_" + kind)(7, argument)
                self.assertEqual(self.gh.api.call_args_list, [get_labels] + expected)

    def test_label_creation_cache_and_existing_definition(self):
        """An existing definition is not created, and a cached definition avoids another lookup."""
        self.gh.api.side_effect = [[], [{"name": "concurro:attention"}], None, [], None]
        self.gh.sync_attention(7, True)
        self.gh.sync_attention(7, True)
        self.assertEqual(
            [c.args for c in self.gh.api.call_args_list],
            [
                ("repos/owner/repo/issues/7/labels?per_page=100&page=1", "GET", None),
                ("repos/owner/repo/labels?per_page=100&page=1", "GET", None),
                ("repos/owner/repo/issues/7/labels", "POST", {"labels": ["concurro:attention"]}),
                ("repos/owner/repo/issues/7/labels?per_page=100&page=1", "GET", None),
                ("repos/owner/repo/issues/7/labels", "POST", {"labels": ["concurro:attention"]}),
            ],
        )
