"""Recoverable fork publication and upstream PR rendering."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from urllib.parse import quote

import change_attention
import conversation
import file_inventory
import markup
import pr_description
import pr_template
import protocol
import reason_guidance
from dashboard import dependency_summary
from github import Transient
from markup import block, link, text
from policy import Blocked, approval_valid
from repository_reader import RepositoryReader

if TYPE_CHECKING:
    from collections.abc import Callable

    from records import Config, Document, Edit, GitHubClient, PendingPublication, PlanArtifact, TaskState, TreeEntry


# Publication title capacity; full model wording stays in the plan artifact.
PR_TITLE_LIMIT = 256
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
        reader: RepositoryReader | None = None,
    ) -> None:
        """Bind publication to its task state and optional plan artifact."""
        self.code, self.pulls, self.config, self.state = code, pulls, config, state
        self.plan_artifact = plan_artifact
        self.reader = reader or RepositoryReader(code, pulls)

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
                raise Blocked(reason_guidance.CHANGED_IMPLEMENTATION_BRANCH)
        else:
            if reservation["created"]:
                raise Blocked(reason_guidance.MISSING_IMPLEMENTATION_BRANCH)
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
        metadata: Document | None = None,
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
                "message": pr_description.commit_message(state, self.config, metadata),
                "tree": tree["sha"],
                "parents": [parent],
            },
        )
        return commit

    def publish_description(self, save: Callable[[], None]) -> None:
        """Refresh only PR presentation, then ask outstanding reviewers to resolve their findings."""
        state, update = self.state, self.state["description_update"]
        if not approval_valid(state, self.config) or update["head"] != state.get("head"):
            raise Blocked("description update does not match its approved candidate")
        pr = self.pulls.repo(f"pulls/{state['pr']}")
        if pr["state"] != "open" or pr.get("merged") or pr["head"]["sha"] != update["head"]:
            raise Blocked("PR closed or head changed before description publication")
        pr_description.refresh(state, self.pulls, pr, update["body"], save, self.pr_body(legacy=True))
        owners = {f["provider"] for f in state.get("findings", []) if f["status"] in ("open", "withdrawn")}
        retry = {p for p, r in state["reviews"].items() if r["verdict"] != "approve"} | owners
        if retry:
            state.setdefault("review_history", []).append(deepcopy(state["reviews"]))
            state["reviews"] = {p: r for p, r in state["reviews"].items() if p not in retry}
        state.update(stage="review", input_revision=state.get("input_revision", 0) + 1)
        del state["description_update"]
        save()

    def publish(self, save: Callable[[], None], preflight: Callable[[list[TaskState]], None]) -> None:
        """Reconcile interrupted publication without overwriting a foreign branch tip."""
        state, pending, reader = self.state, self.state["pending"], self.reader
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
        try:
            expected_files = file_inventory.candidate_paths(state, reader)
        except ValueError:
            raise Blocked("candidate file inventory cannot be verified") from None
        # Account for the largest durable publication state before advancing a ref or writing a PR.
        projected = deepcopy(state)
        if projected["reviews"]:
            projected.setdefault("review_history", []).append(projected["reviews"])
        projected.update(
            head=pending["head"],
            pr=state.get("pr", 2**63 - 1),
            reviews={},
            stage="review",
            input_revision=state.get("input_revision", 0) + 1,
            changed_files=expected_files,
        )
        projected["pr_description"] = {"last": "f" * 64, "intended": "f" * 64, "human_edited": True}
        # Keep pending as well: the saved intent and all bounded receipts must fit together.
        preflight([state, projected])
        self._advance_branch(pending)
        pr = self._find_or_open_pr(pending, save)
        published_files = reader.pr_paths(state, pr["number"], pending["head"])
        if set(published_files) != set(expected_files):
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
                    raise Blocked(reason_guidance.CHANGED_BRANCH)
                self.code.repo("git/refs/heads/" + state["branch"], "PATCH", {"sha": pending["head"], "force": False})
        else:
            raise Blocked(reason_guidance.MISSING_IMPLEMENTATION_BRANCH)

    def _find_or_open_pr(self, pending: PendingPublication, save: Callable[[], None]) -> Document:
        """Recover an existing matching PR before attempting a new draft PR write."""
        state = self.state
        fork = self.config.get("fork", self.config["repository"])
        owner, repository = fork.split("/")
        prs = self.pulls.pages(
            "pulls?state=all&head=" + quote(owner + ":" + state["branch"]) + "&base=" + self.config["base"]
        )
        if len(prs) > 1 or (prs and prs[0]["state"] != "open"):
            raise Blocked(reason_guidance.CLOSED_OR_DUPLICATE_PR)
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
                raise Blocked(reason_guidance.MISSING_PR)
            if state.get("candidate", {}).get("description"):
                pr_description.prepare_create(state, self.pr_body(), save)
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
            raise Blocked(reason_guidance.FOREIGN_PR_AUTHOR)
        if state.get("candidate", {}).get("description"):
            pr_description.refresh(state, self.pulls, pr, self.pr_body(), save, self.pr_body(legacy=True))
        return pr

    def pr_title(self) -> str:
        """The plan's headline, or its summary's first sentence, as plain text within the publication title capacity."""
        plan, suffix = self.state["plan"], ""
        title = (
            markup.one_line(plan.get("headline") or "")
            or markup.first_sentence(plan["summary"])
            or markup.one_line(plan["summary"])
        )
        title = title.rstrip(".")
        if not title:
            return f"Address issue #{self.state['issue']}"
        return (
            markup.short_title(pr_description.inert(title, preserve_code=False), PR_TITLE_LIMIT - len(suffix)) + suffix
        )

    def pr_body(self, legacy: bool = False) -> str:
        """The plan's summary, its artifact, any dependency change, and the issue it closes."""
        plan = self.plan_artifact
        candidate = {} if legacy else self.state.get("candidate", {})
        head = self.state.get("pending", {}).get("head", self.state.get("head"))
        parts = pr_description.sections(
            candidate,
            head,
            candidate.get("description", {}).get(
                "review_focus", self.state["plan"].get("publication", {}).get("review_focus", [])
            ),
            self.config.get("fork", self.config["repository"]),
        )
        attention = ""
        if candidate:
            attention = change_attention.notice(
                file_inventory.candidate_paths(self.state, self.reader), self.config, self.state.get("base"), head
            )
        if parts and attention:
            parts["review_focus"] = "\n\n".join(filter(None, (parts["review_focus"], attention)))
        lines = (
            [pr_template.render(parts, self.state.get("pr_template"), self.state.get("base")), "", "---", ""]
            if parts
            else [
                block(self.state["plan"]["summary"] if legacy else pr_description.inert(self.state["plan"]["summary"])),
                "",
            ]
        )
        if attention and not parts:
            lines += ["### Review focus", "", attention, ""]
        if plan:
            lines += [
                link(
                    "Implementation plan",
                    protocol.artifact_url(self.config["repository"], self.state["issue"], plan["number"]),
                ),
                "",
            ]
        dependencies = [
            "📦 " + text(dependency_summary(path, delta))
            for path, delta in sorted((self.state.get("candidate", {}).get("dependencies") or {}).items())
        ]
        for notice in dependencies:
            lines += [notice, ""]
        issue = protocol.issue_url(self.config["repository"], self.state["issue"])
        closing = (
            f"Closes #{self.state['issue']}"
            if self.config.get("upstream", self.config["repository"]) == self.config["repository"]
            else f"Closes {issue}"
        )
        body = "\n".join(lines + [closing])
        if len(body.encode()) <= protocol.PUBLICATION_BODY_BYTES:
            return body
        note = "Full implementation evidence is retained in the issue's report archives."
        if plan:
            note += " " + link(
                "Implementation plan",
                protocol.artifact_url(self.config["repository"], self.state["issue"], plan["number"]),
            )
        lead = candidate.get("description", {}).get("lead") or self.state["plan"]["summary"]
        notices = (["### Review focus\n\n" + attention] if attention else []) + dependencies
        suffix = "\n\n" + "\n\n".join([*notices, note, closing])
        return (
            markup.excerpt(
                pr_description.inert(lead),
                protocol.PUBLICATION_BODY_BYTES - len(suffix.encode()),
                "Complete text in the report archives.",
            )
            + suffix
        )

    def pr_url(self, number: int) -> str:
        """The task's pull request on GitHub."""
        return f"https://github.com/{self.config.get('upstream', self.config['repository'])}/pull/{number}"
