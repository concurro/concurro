"""Observe each task's inputs and enqueue work only when they change."""

import logging
import threading
import time

from policy import digest
from readiness import ci_refs

ENROLLMENT = "concurro"
PAGE_LIMIT = 100
# A cached comment and label-event scan is reused while the issue's `updated_at` is unchanged,
# and refreshed at least this often regardless.
HINT_SECONDS = 3600

log = logging.getLogger("concurro.service")


def pages(reader, path, key=None):
    """Read every page of a collection, or fail if the bounded traversal cannot finish."""
    result = []
    for page in range(1, PAGE_LIMIT + 1):
        separator = "&" if "?" in path else "?"
        batch = reader.request("GET", f"{path}{separator}per_page=100&page={page}")
        batch = batch[key] if key else batch
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError("pagination exceeded safety bound")


class Reconciler:
    """Snapshot task inputs through read tokens; compare with what the last completed tick acknowledged."""

    def __init__(self, registry, reader, repositories, identities, clock=time.time, cleanup=None):
        """`reader(repository)` returns that repository's read transport; `identities` are Concurro logins."""
        self.registry, self.reader, self.clock = registry, reader, clock
        self.repositories = tuple(repositories)
        self.identities = frozenset(identities)
        self.guard = threading.Lock()
        self.hints = {}
        self.cleanup = cleanup

    def ours(self, login):
        """Whether a login is a Concurro identity, whose writes are never task input."""
        return login in self.identities

    def snapshot(self, key):
        """The task's inputs only: Concurro's own comments, label events and the issue's `updated_at` are excluded."""
        reader, root = self.reader(key[0]), f"repos/{key[0]}/"
        issue = reader.request("GET", root + f"issues/{key[1]}")
        hint = issue.get("updated_at")
        with self.guard:
            cached = self.hints.get(key)
        if hint and cached and cached[0] == hint and cached[1] > self.clock():
            comments, enrollment = cached[2], cached[3]
        else:
            comments = sorted([c["id"], digest(c["body"])] for c in pages(reader, root + f"issues/{key[1]}/comments")
                              if not self.ours(c["user"]["login"]))
            enrollment = [[e["id"], e["event"], e["actor"]["login"]]
                          for e in pages(reader, root + f"issues/{key[1]}/events")
                          if e["event"] in ("labeled", "unlabeled") and e.get("label", {}).get("name") == ENROLLMENT
                          and e.get("actor") and not self.ours(e["actor"]["login"])]
            with self.guard:
                self.hints[key] = (hint, self.clock() + HINT_SECONDS, comments, enrollment)
        return {"issue": {"state": issue["state"], "requirements": digest([issue["title"], issue["body"]])},
                "comments": comments, "enrollment": enrollment, "pull": self.pull(reader, root, key)}

    def pull(self, reader, root, key):
        """The linked pull request's head, merge state, reviews and check runs, or None."""
        pull = self.registry.pull(key)
        if pull is None:
            return None
        reader, root = self.reader(pull.repository), f"repos/{pull.repository}/"
        number = pull.number
        pr = reader.request("GET", root + f"pulls/{number}")
        head = pr["head"]["sha"]
        reviews = sorted([r["id"], r["state"]] for r in pages(reader, root + f"pulls/{number}/reviews")
                         if not self.ours((r.get("user") or {}).get("login")))
        checks, statuses, suites, runs = [], [], [], []
        for sha in ci_refs(pr):
            checks.extend([sha, c.get("id"), (c.get("check_suite") or {}).get("id"), c["name"], c["status"], c.get("conclusion")]
                          for c in pages(reader, root + f"commits/{sha}/check-runs?filter=all", "check_runs"))
            statuses.extend([sha, c["context"], c["state"]]
                            for c in pages(reader, root + f"commits/{sha}/status", "statuses"))
            suites.extend([sha, c["id"], c["status"], c.get("conclusion")]
                          for c in pages(reader, root + f"commits/{sha}/check-suites", "check_suites"))
            runs.extend([sha, r["id"], r["status"], r.get("conclusion")]
                        for r in pages(reader, root + f"actions/runs?head_sha={sha}", "workflow_runs"))
        return {"number": number, "head": head, "merge_head": pr.get("merge_commit_sha"),
                "state": pr.get("state"), "draft": pr.get("draft"), "merged": bool(pr.get("merged")),
                "reviews": reviews, "checks": sorted(checks), "statuses": sorted(statuses),
                "suites": sorted(suites), "runs": sorted(runs)}

    def candidates(self, repository):
        """Open enrolled issues, plus every task the registry still holds active (its label may be gone)."""
        enrolled = pages(self.reader(repository), f"repos/{repository}/issues?state=open&labels={ENROLLMENT}")
        keys = {(repository, issue["number"]) for issue in enrolled if "pull_request" not in issue}
        return sorted(keys | set(self.registry.active(repository)))

    def poll(self, repositories=None):
        """Enqueue every candidate whose inputs changed; reads only, so no model call without a change."""
        if self.cleanup is not None:
            self.cleanup()
        enqueued = []
        for repository in repositories or self.repositories:
            try:
                keys = self.candidates(repository)
            except Exception as error:
                log.warning("poll of %s failed: %s", repository, type(error).__name__)
                continue
            for key in keys:
                try:
                    snapshot = self.snapshot(key)
                except Exception as error:
                    log.warning("snapshot of %s#%s failed: %s", *key, type(error).__name__)
                    continue
                if self.registry.observe(key, snapshot):
                    enqueued.append(key)
        return enqueued
