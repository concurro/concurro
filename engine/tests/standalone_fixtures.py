"""An existing contributor pull request and its verified review requests, for standalone review tests."""

from copy import deepcopy

from support import CONFIG

from fakes import FakeAgents, FakeGitHub
from standalone import ReviewController

PR = 9
BASE, HEAD = "a" * 40, "b" * 40
APP = {"login": CONFIG["bot_login"], "id": 1001, "node": "BOT_app"}
REVIEWER = {"login": "ConcurroBot", "id": 4242, "node": "U_reviewer"}
REQUESTER = {"login": "writer", "id": 77}


def request(delivery="d1", action="requested", actor=None, at="2026-11-15T10:00:00Z"):
    """One verified review-request event as the service records it."""
    return {"delivery": delivery, "action": action, "actor": dict(actor or REQUESTER), "at": at}


class FakePull(FakeGitHub):
    """Upstream PR #9 from a contributor; reviews are authored by the reviewer account."""

    def __init__(self, config):
        super().__init__(config)
        self.issue.update(number=PR, labels=[], user={"login": "contributor"}, pull_request={"url": "pulls/9"})
        self.comments = []
        self.events = []
        self.commits[HEAD] = {"tree": {"sha": HEAD}}
        self.trees[HEAD] = {**self.trees[BASE], "docs/guide.md": "Original guide\nA new term.\n"}
        self.pr = {
            "id": 900,
            "node_id": "PR_9",
            "number": PR,
            "user": {"login": "contributor", "id": 55},
            "title": "Explain the term",
            "body": "Adds the missing explanation.",
            "state": "open",
            "draft": False,
            "merged": False,
            "head": {"sha": HEAD, "ref": "explain", "repo": {"full_name": "contributor/fork"}},
            "base": {"ref": "main", "sha": BASE},
        }
        self.check_runs = []
        self.fail_checks = False

    def review_user(self):
        return {"login": REVIEWER["login"], "id": REVIEWER["id"]}

    def review_author(self):
        return {"login": REVIEWER["login"], "id": REVIEWER["node"]}

    def push(self, sha, content):
        """Move the PR head to a new commit."""
        self.commits[sha] = {"tree": {"sha": sha}}
        self.trees[sha] = {**self.trees[BASE], "docs/guide.md": content}
        self.pr["head"]["sha"] = sha

    def bot_comments(self):
        """Conversation comments the App posted."""
        return [c for c in self.comments if c["user"]["login"] == self.config["bot_login"]]

    def repo(self, path, method="GET", data=None):
        if path == f"issues/{PR}":
            return deepcopy(self.issue)
        if path == f"issues/{PR}/comments" and method == "POST":
            self.comment(data["body"], self.config["bot_login"])
            return deepcopy(self.comments[-1])
        if path.startswith("compare/"):
            base, head = path[len("compare/") :].split("...")
            assert base == self.pr["base"]["sha"] or base == BASE
            before, after = self.trees[BASE], self.trees[head]
            files = [
                {
                    "filename": name,
                    "patch": "@@ -1 +1,2 @@\n Original guide\n+" + (after.get(name) or "").split("\n")[1],
                }
                for name in sorted(set(before) | set(after))
                if before.get(name) != after.get(name)
            ]
            return {"merge_base_commit": {"sha": BASE}, "files": files}
        if path == "check-runs" and method == "POST":
            if self.fail_checks:
                raise RuntimeError("checks unavailable")
            self.counter += 1
            self.check_runs.append({"id": self.counter, "app": {"slug": "concurro"}, **deepcopy(data)})
            if getattr(self, "lose_check_reply", False):
                self.lose_check_reply = False
                raise RuntimeError("lost response after check creation")
            return deepcopy(self.check_runs[-1])
        if path.startswith("check-runs/") and method == "GET":
            return deepcopy(next(r for r in self.check_runs if r["id"] == int(path.split("/")[1])))
        if path.startswith("check-runs/") and method == "PATCH":
            if self.fail_checks:
                raise RuntimeError("checks unavailable")
            run = next(r for r in self.check_runs if r["id"] == int(path.split("/")[1]))
            run.update(deepcopy(data))
            return deepcopy(run)
        return super().repo(path, method, data)

    def pages(self, path, key=None):
        if path == f"issues/{PR}/comments":
            return deepcopy(self.comments)
        if path == f"issues/{PR}/events":
            return deepcopy(self.events)
        if path.startswith("commits/") and "/check-runs" in path:
            sha = path.split("/")[1]
            return [deepcopy(r) for r in self.check_runs if r["head_sha"] == sha]
        return super().pages(path, key)


class Harness:
    """The standalone controller over the fake PR, ticked as the service does."""

    def setUp(self):
        self.config = {**deepcopy(CONFIG), "actors": {"app": dict(APP), "reviewer": dict(REVIEWER)}}
        self.gh = FakePull(self.config)
        self.requests = [request()]
        self.agents = FakeAgents()

    def controller(self, **options):
        return ReviewController(self.gh, self.agents, self.config, requests=lambda: list(self.requests), **options)

    def tick(self, **options):
        """One tick; returns the controller and its scheduling outcome."""
        controller = self.controller(**options)
        return controller, controller.advance(PR)

    def run_to(self, stage, ticks=20):
        """Tick until the task reaches `stage`; return the last controller."""
        for _ in range(ticks):
            controller, _ = self.tick()
            if controller.state and controller.state["stage"] == stage:
                return controller
        self.fail("never reached " + stage)

    def state(self):
        """The task's checkpoint as a fresh controller loads it."""
        controller = self.controller()
        controller.issue = self.gh.repo(f"issues/{PR}")
        controller.load(self.gh.pages(f"issues/{PR}/comments"))
        return controller.state
