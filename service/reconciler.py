"""Observe each task's inputs and enqueue work only when they change."""

import logging
import threading
import time

from github import Transient, paginate
from policy import digest
from readiness import ci_refs

from .failures import backoff
from .observations import satisfied

ENROLLMENT = "concurro"
# A cached comment and label-event scan is reused while the issue's `updated_at` is unchanged,
# and refreshed at least this often regardless.
HINT_SECONDS = 3600

log = logging.getLogger("concurro.service")


def pages(reader, path, key=None):
    """Read every page of a collection."""
    return paginate(lambda url: reader.request("GET", url), path, key=key, page_size=100)


class Reconciler:
    """Snapshot task inputs through read tokens; compare with what the last completed tick acknowledged."""

    def __init__(self, registry, reader, repositories, identities, clock=time.time, cleanup=None, command_comment=None):
        """`reader(repository)` returns that repository's read transport; `identities` are Concurro logins."""
        self.registry, self.reader, self.clock = registry, reader, clock
        self.repositories = tuple(repositories)
        self.identities = frozenset(identities)
        self.guard = threading.Lock()
        self.hints = {}
        self.cleanup = cleanup
        self.command_comment = command_comment

    def ours(self, login):
        """Whether a login belongs to Concurro; only marked commands become comment input."""
        return login in self.identities

    def comment_input(self, comments):
        """The snapshot form of issue comments: everyone's but Concurro's, plus marked commands."""
        return sorted(
            [c["id"], digest(c["body"])]
            for c in comments
            if not self.ours(c["user"]["login"]) or self.command_comment and self.command_comment(c["body"] or "")
        )

    def snapshot(self, key, cached=True, comments_read=None):
        """Task inputs, including marked commands but excluding other App-authored changes."""
        reader, root = self.reader(key[0]), f"repos/{key[0]}/"
        issue = reader.request("GET", root + f"issues/{key[1]}")
        hint = issue.get("updated_at")
        with self.guard:
            entry = self.hints.get(key) if cached else None
        if hint and entry and entry[0] == hint and entry[1] > self.clock():
            comments, enrollment = entry[2], entry[3]
        else:
            all_comments = pages(reader, root + f"issues/{key[1]}/comments")
            if comments_read is not None:
                comments_read.extend(all_comments)
            comments = self.comment_input(all_comments)
            enrollment = [
                [e["id"], e["event"], e["actor"]["login"]]
                for e in pages(reader, root + f"issues/{key[1]}/events")
                if e["event"] in ("labeled", "unlabeled")
                and e.get("label", {}).get("name") == ENROLLMENT
                and e.get("actor")
                and not self.ours(e["actor"]["login"])
            ]
            if cached:
                with self.guard:
                    self.hints[key] = (hint, self.clock() + HINT_SECONDS, comments, enrollment)
        return {
            "issue": {"state": issue["state"], "requirements": digest([issue["title"], issue["body"]])},
            "comments": comments,
            "enrollment": enrollment,
            "pull": self.pull(reader, root, key),
        }

    def pull(self, reader, root, key):
        """The linked pull request's head, merge state, reviews and check runs, or None."""
        pull = self.registry.pull(key)
        if pull is None:
            return None
        reader, root = self.reader(pull.repository), f"repos/{pull.repository}/"
        number = pull.number
        pr = reader.request("GET", root + f"pulls/{number}")
        head = pr["head"]["sha"]
        reviews = sorted(
            [r["id"], r["state"]]
            for r in pages(reader, root + f"pulls/{number}/reviews")
            if not self.ours((r.get("user") or {}).get("login"))
        )
        checks, statuses, suites, runs = [], [], [], []
        for sha in ci_refs(pr):
            checks.extend(
                [sha, c.get("id"), (c.get("check_suite") or {}).get("id"), c["name"], c["status"], c.get("conclusion")]
                for c in pages(reader, root + f"commits/{sha}/check-runs?filter=all", "check_runs")
            )
            statuses.extend(
                [sha, c["context"], c["state"]] for c in pages(reader, root + f"commits/{sha}/status", "statuses")
            )
            suites.extend(
                [sha, c["id"], c["status"], c.get("conclusion")]
                for c in pages(reader, root + f"commits/{sha}/check-suites", "check_suites")
            )
            runs.extend(
                [sha, r["id"], r["status"], r.get("conclusion")]
                for r in pages(reader, root + f"actions/runs?head_sha={sha}", "workflow_runs")
            )
        return {
            "number": number,
            "head": head,
            "merge_head": pr.get("merge_commit_sha"),
            "base_ref": (pr.get("base") or {}).get("ref"),
            "base": (pr.get("base") or {}).get("sha"),
            "state": pr.get("state"),
            "draft": pr.get("draft"),
            "merged": bool(pr.get("merged")),
            "reviews": reviews,
            "checks": sorted(checks),
            "statuses": sorted(statuses),
            "suites": sorted(suites),
            "runs": sorted(runs),
        }

    def candidates(self, repository):
        """Open enrolled issues, plus every task the registry still holds active (its label may be gone)."""
        enrolled = pages(self.reader(repository), f"repos/{repository}/issues?state=open&labels={ENROLLMENT}")
        keys = {(repository, issue["number"]) for issue in enrolled if "pull_request" not in issue}
        return sorted(keys | set(self.registry.active(repository)))

    def poll(self, repositories=None, *, strict=False):
        """Enqueue every candidate whose inputs changed; reads only, so no model call without a change."""
        if self.cleanup is not None:
            self.cleanup()
        enqueued = []
        for repository in repositories or self.repositories:
            try:
                keys = self.candidates(repository)
            except Exception as error:
                if strict:
                    raise
                log.warning("poll of %s failed: %s", repository, type(error).__name__)
                continue
            for key in keys:
                try:
                    snapshot = self.snapshot(key)
                except Exception as error:
                    if strict:
                        raise
                    log.warning("snapshot of %s#%s failed: %s", *key, type(error).__name__)
                    continue
                if self.registry.observe(key, snapshot):
                    enqueued.append(key)
        return enqueued

    def observe(self, key, cached=True):
        """Observe one task; failures propagate so durable callers can retry."""
        return self.registry.observe(key, self.snapshot(key, cached=cached))

    def observe_pending(self):
        """Drain durable observations without bypassing engine retry deadlines."""
        for key, version, attempts, expectations in self.registry.observations():
            try:
                comments = []
                snapshot = self.snapshot(key, cached=False, comments_read=comments)
                self.registry.observe(key, snapshot)
                if not satisfied(expectations, comments):
                    raise Transient("comment observation incomplete")
            except Exception as error:
                delay = (
                    error.retry_after
                    if isinstance(error, Transient) and error.retry_after is not None
                    else backoff(attempts)
                )
                self.registry.finish_observation(key, version, retry_after=delay)
                log.warning("observation of %s#%s failed: %s", *key, type(error).__name__)
            else:
                self.registry.finish_observation(key, version)
