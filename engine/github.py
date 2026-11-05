"""Small GitHub REST/GraphQL adapter; credentials never reach model processes."""

import json
import subprocess
from urllib.parse import quote

STAGE_LABEL_PREFIX = "concurro:stage:"
ATTENTION_LABEL = "concurro:attention"
STAGES = {"intake", "clarify", "plan", "plan_qc", "planned", "implement", "publish", "review",
          "gates", "closed", "merging", "merged", "observed_merged", "blocked", "stopped", "rejected", "escalated"}
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

    TEXT = {"throttled": "GitHub rate limit reached; retry later.",
            "unavailable": "GitHub unavailable; retry later.",
            "response_lost": "GitHub response lost after the write applied; reconstruct on retry."}

    def __init__(self, code, retry_after=None):
        """Select a fixed text; unknown codes become `unavailable`."""
        self.code = code if code in self.TEXT else "unavailable"
        self.retry_after = retry_after
        super().__init__(self.TEXT[self.code])


class GitHub:
    """Use gh's authenticated transport, with bounded pagination and explicit methods."""

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
        result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            error = NotFound if "(HTTP 404)" in result.stderr else RuntimeError
            raise error("GitHub request failed: " + method + " " + path)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def repo(self, path, method="GET", data=None):
        """Address a repository endpoint."""
        return self.api(self.root + "/" + path, method, data)

    def dispatch(self, session):
        """Start a runner session on the target: a `repository_dispatch` that carries only its ID."""
        self.repo("dispatches", "POST", {"event_type": DISPATCH_EVENT, "client_payload": {"session": session}})

    def archive(self, sha):
        """Download a pinned source archive without extracting candidate files on the host."""
        result = subprocess.run(["gh", "api", self.root + "/tarball/" + sha],
                                capture_output=True, timeout=120)
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
            self.ensure_label(wanted, "8250df", "Concurro progress display; does not grant execution or merge approval.")
            self.repo(f"issues/{number}/labels", "POST", {"labels": [wanted]})
        for old in sorted(current - {wanted}):
            if old.startswith(STAGE_LABEL_PREFIX):
                self.repo(f"issues/{number}/labels/" + quote(old, safe=""), "DELETE")

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
            self.repo(f"issues/{number}/labels", "POST", {"labels": [ATTENTION_LABEL]})
        elif not failed and ATTENTION_LABEL in current:
            self.repo(f"issues/{number}/labels/" + quote(ATTENTION_LABEL, safe=""), "DELETE")

    def pages(self, path, key=None):
        """Read all pages or fail if the bounded traversal cannot finish."""
        result = []
        for page in range(1, 101):
            sep = "&" if "?" in path else "?"
            batch = self.repo(f"{path}{sep}per_page=100&page={page}")
            batch = batch[key] if key else batch
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise RuntimeError("pagination exceeded safety bound")

    def outcome_candidates(self, limit):
        """Retrieve at most ten recent opted-in issues and ten recent comments per issue."""
        issues = self.repo(f"issues?state=all&labels=concurro&sort=updated&direction=desc&per_page={limit}")
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
        owner, name = self.repository.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              nodes { id isResolved comments(first:1) { nodes { databaseId } } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        cursor, result = None, []
        for _ in range(100):
            response = self.api("graphql", "POST", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "cursor": cursor}})
            if response.get("errors"):
                raise RuntimeError("cannot establish review-thread state")
            page = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in page["nodes"]:
                first = node["comments"]["nodes"]
                result.append({"id": node["id"], "isResolved": node["isResolved"],
                               "comment": first[0]["databaseId"] if first else None})
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]
        raise RuntimeError("review-thread pagination exceeded safety bound")

    def mark_ready(self, node_id):
        """Mark the bound draft PR ready; callers verify the current head before and after."""
        result = self.api("graphql", "POST", {"query": MARK_READY,
                          "variables": {"input": {"pullRequestId": node_id}}})
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
        owner, name = self.repository.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              nodes { id isResolved isOutdated }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        cursor, result = None, []
        for _ in range(100):
            response = self.api("graphql", "POST", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "cursor": cursor}})
            if response.get("errors"):
                raise RuntimeError("cannot establish review-thread state")
            page = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            result.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]
        raise RuntimeError("review-thread pagination exceeded safety bound")
