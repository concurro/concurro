"""Recoverable fork publication and upstream PR rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

import candidates
import conversation
import markup
import protocol
from dashboard import dependency_summary
from github import Transient
from markup import block, link, text
from policy import Blocked, approval_valid

if TYPE_CHECKING:
    from collections.abc import Callable

    from records import Config, Document, Edit, GitHubClient, PendingPublication, PlanArtifact, TaskState, TreeEntry


PR_TITLE_LIMIT = 72
# Keep tree requests compact; larger contents are published as separate blob objects.
INLINE_BLOB_LIMIT = 64000


class Publication:
    """Publish an approved candidate using explicit fork and upstream clients."""

    def __init__(
        self,
        code: GitHubClient,
        pulls: GitHubClient,
        config: Config,
        state: TaskState,
        plan_artifact: PlanArtifact | None = None,
    ) -> None:
        """Bind publication to its task state and optional plan artifact."""
        self.code, self.pulls, self.config, self.state = code, pulls, config, state
        self.plan_artifact = plan_artifact

    def _branch_refs(self, branch: str) -> list[Document]:
        """Select only the exact branch from GitHub's prefix-matching endpoint."""
        return [
            ref for ref in self.code.repo("git/matching-refs/heads/" + branch) if ref["ref"] == "refs/heads/" + branch
        ]

    def reserve_development_branch(self, save: Callable[[], None]) -> None:
        """Reserve a fork branch and publish its issue link with recoverable intent."""
        state = self.state
        if state.get("head"):
            return
        branch, base = state["branch"], state["base"]
        refs = self._branch_refs(branch)
        reservations = state.setdefault("development_branches", {})
        if branch not in reservations:
            if refs:
                raise Blocked("implementation branch already exists without controller intent")
            reservations[branch] = {"base": base, "created": False}
            save()
        reservation = reservations[branch]
        if reservation["base"] != base:
            raise Blocked("development branch reservation base changed")
        if refs:
            if refs[0]["object"]["sha"] != base:
                raise Blocked("implementation branch moved outside the controller")
        else:
            if reservation["created"]:
                raise Blocked("implementation branch disappeared")
            self.code.repo("git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": base})
        if not reservation["created"]:
            reservation["created"] = True
            fork = self.config.get("fork", self.config["repository"])
            conversation.queue(
                state,
                "Implementation started",
                "Work branch: " + link(branch, f"https://github.com/{fork}/tree/{branch}"),
            )
            save()

    def build_commit(
        self,
        parent: str,
        edits: list[Edit],
        tree_reader: Callable[[str], dict[str, TreeEntry]],
        inline_blob_limit: int = INLINE_BLOB_LIMIT,
    ) -> Document:
        """Create blobs, a tree and a commit in order, without advancing the task checkpoint."""
        state = self.state
        # Large contents go through blob objects so the tree request contains only small entries.
        entries: list[TreeEntry]
        uploaded_blobs: list[str]
        entries, uploaded_blobs = [], []
        for edit in edits:
            entry: TreeEntry = {
                "path": edit["path"],
                "mode": tree_reader(parent).get(edit["path"], {}).get("mode", "100644"),
                "type": "blob",
            }
            content = edit["content"]
            if content is None:
                entry["sha"] = None
            elif len(content.encode()) > inline_blob_limit:
                entry["sha"] = self.code.repo("git/blobs", "POST", {"content": content, "encoding": "utf-8"})["sha"]
                uploaded_blobs.append(entry["sha"])
            else:
                entry["content"] = content
            entries.append(entry)
        parent_commit = self.code.repo("git/commits/" + parent)
        tree = self.code.create_tree(parent_commit["tree"]["sha"], entries, uploaded_blobs)
        commit = self.code.repo(
            "git/commits",
            "POST",
            {
                "message": f"fix(concurro): address issue #{state['issue']} (round {state['round']})",
                "tree": tree["sha"],
                "parents": [parent],
            },
        )
        return commit

    def publish(self, save: Callable[[], None]) -> None:
        """Reconcile interrupted publication without overwriting a foreign branch tip."""
        state, pending = self.state, self.state["pending"]
        candidate = state.get("candidate", {})
        if (
            not approval_valid(state, self.config)
            or not candidate.get("edits_digest")
            or any(
                candidate.get(key) != value
                for key, value in (
                    ("head", pending["head"]),
                    ("parent", pending["parent"]),
                    ("base", state["base"]),
                    ("plan", state["plan_id"]),
                )
            )
        ):
            raise Blocked("candidate does not match its recorded approval and publication intent")
        self._advance_branch(pending)
        pr = self._find_or_open_pr(pending)
        published_files = candidates.changed_paths(self.pulls.pages(f"pulls/{pr['number']}/files"))
        if set(published_files) != set(candidate["changed_files"]):
            raise Transient("unavailable", 30)
        if state["reviews"]:
            state.setdefault("review_history", []).append(state["reviews"])
        state.update(
            head=pending["head"],
            pr=pr["number"],
            reviews={},
            stage="review",
            input_revision=state.get("input_revision", 0) + 1,
        )
        state["changed_files"] = published_files
        del state["pending"]
        save()

    def _advance_branch(self, pending: PendingPublication) -> None:
        """Advance only the reserved branch or recognize its already-published head."""
        state = self.state
        refs = self._branch_refs(state["branch"])
        if refs:
            current = refs[0]["object"]["sha"]
            if current != pending["head"]:
                reservation = state.get("development_branches", {}).get(state["branch"])
                if current != pending["parent"] or (
                    pending["initial"] and (reservation != {"base": pending["parent"], "created": True})
                ):
                    raise Blocked("branch moved outside the controller")
                self.code.repo("git/refs/heads/" + state["branch"], "PATCH", {"sha": pending["head"], "force": False})
        else:
            raise Blocked("implementation branch disappeared")

    def _find_or_open_pr(self, pending: PendingPublication) -> Document:
        """Recover an existing matching PR before attempting a new draft PR write."""
        state = self.state
        fork = self.config.get("fork", self.config["repository"])
        owner, repository = fork.split("/")
        prs = self.pulls.pages(
            "pulls?state=all&head=" + quote(owner + ":" + state["branch"]) + "&base=" + self.config["base"]
        )
        if len(prs) > 1 or (prs and prs[0]["state"] != "open"):
            raise Blocked("PR was closed or duplicated; reconcile manually")
        if prs:
            pr = prs[0]
            if (
                pr.get("user", {}).get("login") != self.config["bot_login"]
                or state.get("pr", pr["number"]) != pr["number"]
                or pr["head"].get("repo", {}).get("full_name", "").lower() != fork.lower()
                or pr["head"]["ref"] != state["branch"]
                or pr["base"]["ref"] != self.config["base"]
            ):
                raise Blocked("PR does not match the controller's fork branch and author")
            if pr["head"]["sha"] != pending["head"]:
                raise Transient("unavailable", 30)
        else:
            if state.get("pr"):
                raise Blocked("recorded PR disappeared; reconcile manually")
            pr = self.pulls.repo(
                "pulls",
                "POST",
                {
                    "title": self.pr_title(),
                    "head": owner + ":" + state["branch"],
                    "head_repo": repository,
                    "base": self.config["base"],
                    "body": self.pr_body(),
                    "draft": True,
                    "maintainer_can_modify": False,
                },
            )
        if pr.get("user", {}).get("login") != self.config["bot_login"]:
            raise Blocked("PR is not authored by the controller App")
        return pr

    def pr_title(self) -> str:
        """The plan's headline, or its summary's first sentence, as plain text within 72 characters."""
        plan, suffix = self.state["plan"], f" (#{self.state['issue']})"
        title = (
            markup.one_line(plan.get("headline") or "")
            or markup.first_sentence(plan["summary"])
            or markup.one_line(plan["summary"])
        )
        title = title.rstrip(".")
        if not title:
            return f"Address issue #{self.state['issue']}"
        limit = PR_TITLE_LIMIT - len(suffix)
        if len(title) > limit:
            cut = title[: limit - 1]
            # Cut at a word boundary unless the first word alone is too long.
            title = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:-") + "…"
        return title + suffix

    def pr_body(self) -> str:
        """The plan's summary, its artifact, any dependency change, and the issue it closes."""
        plan = self.plan_artifact
        lines = [block(self.state["plan"]["summary"]), ""]
        if plan:
            lines += [
                link(
                    "Implementation plan",
                    protocol.artifact_url(self.config["repository"], self.state["issue"], plan["number"]),
                ),
                "",
            ]
        for path, delta in sorted((self.state.get("candidate", {}).get("dependencies") or {}).items()):
            lines += ["📦 " + text(dependency_summary(path, delta)), ""]
        issue = protocol.issue_url(self.config["repository"], self.state["issue"])
        closing = (
            f"Closes #{self.state['issue']}"
            if self.config.get("upstream", self.config["repository"]) == self.config["repository"]
            else f"Closes {issue}"
        )
        return "\n".join(lines + [closing])

    def pr_url(self, number: int) -> str:
        """The task's pull request on GitHub."""
        return f"https://github.com/{self.config.get('upstream', self.config['repository'])}/pull/{number}"
