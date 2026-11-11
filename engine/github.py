"""Small GitHub REST/GraphQL adapter; credentials never reach model processes."""

import json
import subprocess
from itertools import count
from urllib.parse import quote

import protocol
from vocab import Stage

STAGE_LABEL_PREFIX = "concurro:stage:"
ATTENTION_LABEL = "concurro:attention"
STAGES = {stage.value for stage in Stage}
# The engine's only GraphQL mutations; the service gateway admits exactly these documents.
RESOLVE_THREAD = """mutation($input:ResolveReviewThreadInput!) {
          resolveReviewThread(input:$input) { thread { id isResolved } }
        }"""

MARK_READY = """mutation($input:MarkPullRequestReadyForReviewInput!) {
          markPullRequestReadyForReview(input:$input) { pullRequest { id isDraft headRefOid } }
        }"""


class NotFound(RuntimeError):
    """The resource does not exist or is not visible to this token."""


DISPATCH_EVENT = "concurro-agent"


class Transient(RuntimeError):
    """GitHub did not apply a request, or its result was not read; retry later.

    `retry_after` is the delay GitHub requested, in seconds, or None. The text is
    fixed per code, so it is safe to log and publish.
    """

    TEXT = {
        "throttled": "GitHub rate limit reached; retry later.",
        "unavailable": "GitHub unavailable; retry later.",
        "response_lost": "GitHub response lost after the write applied; reconstruct on retry.",
    }

    def __init__(self, code, retry_after=None):
        """Select a fixed text; unknown codes become `unavailable`."""
        self.code = code if code in self.TEXT else "unavailable"
        self.retry_after = retry_after
        super().__init__(self.TEXT[self.code])


def paginate(fetch, path, *, key=None, page_size, limit=None):
    """Collect a REST listing, optionally stopping at an operator-selected item count."""
    result = []
    for page in count(1):
        sep = "&" if "?" in path else "?"
        batch = fetch(f"{path}{sep}per_page={page_size}&page={page}")
        batch = batch[key] if key else batch
        result.extend(batch)
        if limit is not None and len(result) >= limit:
            return result[:limit]
        if len(batch) < page_size:
            return result


class GitHub:
    """Use gh's authenticated transport, with complete pagination and explicit methods."""

    def __init__(self, repository):
        """Select the sole repository this client may mutate."""
        self.repository = repository
        self.root = "repos/" + repository
        self.known_labels = None

    def api(self, path, method="GET", data=None):
        """Make a request without placing payloads or credentials in shell text."""
        command = ["gh", "api", "--method", method, path]
        if data is not None:
            command += ["--input", "-"]
        result = subprocess.run(
            command, input=json.dumps(data) if data is not None else None, text=True, capture_output=True, timeout=90
        )
        if result.returncode:
            error = NotFound if "(HTTP 404)" in result.stderr else RuntimeError
            raise error("GitHub request failed: " + method + " " + path)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def repo(self, path, method="GET", data=None):
        """Address a repository endpoint."""
        return self.api(self.root + "/" + path, method, data)

    def create_tree(self, base_tree, entries, uploaded_blobs=()):
        """Create a tree; service clients may retry visibility lag for this publication's new blobs."""
        return self.repo("git/trees", "POST", {"base_tree": base_tree, "tree": entries})

    def dispatch(self, session):
        """Start a runner session on the target: a `repository_dispatch` that carries only its ID."""
        self.repo("dispatches", "POST", {"event_type": DISPATCH_EVENT, "client_payload": {"session": session}})

    def archive(self, sha):
        """Download a pinned source archive without extracting candidate files on the host."""
        result = subprocess.run(["gh", "api", self.root + "/tarball/" + sha], capture_output=True, timeout=120)
        if result.returncode or len(result.stdout) > 100_000_000:
            raise RuntimeError("source archive unavailable or too large")
        return result.stdout

    def sync_stage(self, number, stage):
        """Maintain one namespaced progress label while preserving enrollment and human labels."""
        if stage not in STAGES:
            raise ValueError("unknown stage label")
        wanted = STAGE_LABEL_PREFIX + stage.replace("_", "-")
        current = {label["name"] for label in self.pages(f"issues/{number}/labels")}
        if wanted not in current:
            self.ensure_label(
                wanted, "8250df", "Concurro progress display; does not grant execution or merge approval."
            )
            self._add_label(number, wanted)
        for old in sorted(current - {wanted}):
            if old.startswith(STAGE_LABEL_PREFIX):
                self._remove_label(number, old)

    def _add_label(self, number, name):
        """Attach one existing label to the issue."""
        self.repo(f"issues/{number}/labels", "POST", {"labels": [name]})

    def _remove_label(self, number, name):
        """Detach one label using its encoded name."""
        self.repo(f"issues/{number}/labels/" + quote(name, safe=""), "DELETE")

    def ensure_label(self, name, color, description):
        """Create a display label only when it is absent from the repository."""
        if self.known_labels is None:
            self.known_labels = {label["name"] for label in self.pages("labels")}
        if name not in self.known_labels:
            self.repo("labels", "POST", {"name": name, "color": color, "description": description})
            self.known_labels.add(name)

    def sync_attention(self, number, failed):
        """Set checkpoint health independently of durable stage and enrollment labels."""
        current = {label["name"] for label in self.pages(f"issues/{number}/labels")}
        if failed and ATTENTION_LABEL not in current:
            self.ensure_label(ATTENTION_LABEL, "d4c5f9", "Concurro checkpoint needs attention; display only.")
            self._add_label(number, ATTENTION_LABEL)
        elif not failed and ATTENTION_LABEL in current:
            self._remove_label(number, ATTENTION_LABEL)

    def pages(self, path, key=None):
        """Read every page of the requested collection."""
        # Artifact reads deliberately use smaller pages; reconciler scans always use 100.
        page_size = protocol.ARTIFACT_PAGE_SIZE if path.endswith("/artifacts") else 100
        return paginate(
            self.repo,
            path,
            key=key,
            page_size=page_size,
        )

    def outcome_candidates(self, limit):
        """Retrieve the configured recent issue window and a small comment discovery window."""
        issues = paginate(
            self.repo,
            "issues?state=all&labels=concurro&sort=updated&direction=desc",
            page_size=min(limit, 100),
            limit=limit,
        )
        result = []
        for issue in issues[:limit]:
            if "pull_request" in issue:
                continue
            # A small fixed window is intentional: discovery must not scan arbitrary history.
            comments = self.repo(f"issues/{issue['number']}/comments?per_page=10&page=1")
            result.append((issue["number"], comments[:10]))
        return result

    def permitted(self, login):
        """Require current repository write access, not author association alone."""
        result = self.repo("collaborators/" + quote(login, safe="") + "/permission")
        return result["permission"] in ("admin", "maintain", "write")

    def review_threads(self, number):
        """Every review thread as `{id, isResolved, comment}`, where `comment` is its first comment's REST ID."""
        result = []
        for node in self._review_thread_nodes(number, "id isResolved comments(first:1) { nodes { databaseId } }"):
            first = node["comments"]["nodes"]
            result.append(
                {
                    "id": node["id"],
                    "isResolved": node["isResolved"],
                    "comment": first[0]["databaseId"] if first else None,
                }
            )
        return result

    def mark_ready(self, node_id):
        """Mark the bound draft PR ready; callers verify the current head before and after."""
        result = self.api("graphql", "POST", {"query": MARK_READY, "variables": {"input": {"pullRequestId": node_id}}})
        pr = ((result.get("data") or {}).get("markPullRequestReadyForReview") or {}).get("pullRequest")
        if result.get("errors") or not pr or pr.get("isDraft") is not False:
            raise RuntimeError("GitHub did not confirm PR readiness")
        return pr

    def resolve_thread(self, thread_id):
        """Resolve one review thread; return whether GitHub reports it resolved."""
        result = self.api("graphql", "POST", {"query": RESOLVE_THREAD, "variables": {"input": {"threadId": thread_id}}})
        if result.get("errors"):
            raise RuntimeError("review thread resolution failed; reconcile on the next tick")
        thread = ((result.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {}
        return thread.get("id") == thread_id and thread.get("isResolved") is True

    def threads(self, number):
        """Read every review thread; an outdated unresolved thread still blocks."""
        return list(self._review_thread_nodes(number, "id isResolved isOutdated"))

    def _review_thread_nodes(self, number, fields):
        """Yield nodes before fetching the next page so caller validation keeps its request order."""
        owner, name = self.repository.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              nodes { FIELDS }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }""".replace("FIELDS", fields)
        cursor = None
        while True:
            response = self.api(
                "graphql",
                "POST",
                {"query": query, "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor}},
            )
            if response.get("errors"):
                raise RuntimeError("cannot establish review-thread state")
            page = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]
