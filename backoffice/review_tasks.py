"""Standalone PR review tasks: their list, live status and full reports, read from verified PR artifacts.

GitHub shows no artifacts on a pull request, so this is where complete reports are read.
Nothing here is linked from public GitHub output.
"""

import hashlib
import json

import report_store
from checkpoints import CheckpointError
from github import NotFound
from service.github_reads import TASK_BRANCH
from vocab import STANDALONE_REVIEW

# Evidence a review task's view renders; everything else in its checkpoint stays unread.
FIELDS = frozenset({"reviews", "review_history", "findings", "reports", "reason"})
ACTIVE = ("queued", "review", "publishing")


def listed(tasks, target):
    """The upstream's reviewed pull requests the service lists: every held one off a task branch.

    Without the service's list there is nothing to discover them from, so none are listed.
    """
    from .commands import IndexUnavailable
    from .tasks import ci

    upstream = target.upstream
    if tasks.holds(upstream, "pulls") is None:
        return []
    try:
        rows = tasks.listing(upstream, "pulls", lambda: [], rows=lambda: tasks.lists.list_rows(upstream, "pulls"))
    except (IndexUnavailable, NotFound):
        return []
    owner, repo = upstream.split("/")
    return sorted(
        (
            {
                "owner": owner,
                "repo": repo,
                "pr": pull["number"],
                "title": pull["title"],
                "open": pull["open"],
                "draft": pull["draft"],
                "branch": pull["ref"],
                "headSha": pull["sha"][:7],
                "ci": ci(pull["runs"]) if pull.get("runs") is not None else "none",
                "updatedAt": pull["updated_at"],
            }
            for pull in rows
            if not TASK_BRANCH.fullmatch(pull["ref"])
        ),
        key=lambda review: review["updatedAt"],
        reverse=True,
    )


def load(tasks, repository, number, fields=FIELDS):
    """`(target, fork, config, checkpoints, state)` of one verified standalone review, or `TaskNotFound`."""
    from .tasks import REPORTS, ReadCheckpoints, TaskNotFound

    target, fork = tasks.find_target(repository)
    if repository.lower() != target.upstream.lower():
        raise TaskNotFound(f"{repository}#{number} is not a reviewed pull request")
    config = tasks.task_config(target, target.upstream)
    checkpoints = ReadCheckpoints(
        tasks.access.engine(target.upstream), config, number, report_cache=REPORTS, fields=fields
    )
    # Review tasks have no checkpoint comment: the audit artifact is discovered and verified directly.
    state = checkpoints.load([])
    if state is None or state.get("kind") != STANDALONE_REVIEW:
        raise TaskNotFound(f"{repository}#{number} is not a reviewed pull request")
    return target, fork, config, checkpoints, state


def entries(state):
    """The addressable report index: each archived report with its stable publication ID."""
    return [entry for entry in state.get("reports") or [] if entry.get("event") and entry.get("data")]


def report_summary(entry):
    """One report as lists show it; its ID is the controller's publication receipt, never a position."""
    publication = entry.get("publication") or {}
    details = publication.get("details") or {}
    return {
        "id": entry["event"],
        "step": entry.get("step") or "",
        "reviewer": entry.get("provider") or "",
        "model": details.get("model") or "",
        "round": details.get("round"),
        "head": details.get("head") or "",
        "outcome": entry.get("outcome") or "",
        "at": publication.get("at") or "",
    }


def rounds(state):
    """Each round's snapshot, request and per-reviewer progress, oldest first."""
    reports = [report_summary(entry) for entry in entries(state)]
    current = state.get("round")
    result = []
    for record in state.get("rounds") or []:
        number = record["round"]
        done = {r["reviewer"]: r for r in reports if r["round"] == number}
        result.append(
            {
                "round": number,
                "head": record["head"],
                "base": record["base"],
                "baseRef": record["base_ref"],
                "baseTip": record["base_tip"],
                "requestedBy": (record.get("requested_by") or {}).get("login", ""),
                "requestedAt": record.get("requested_at") or "",
                "opened": record.get("opened") or "",
                "started": record.get("started") or "",
                "completed": record.get("completed") or "",
                "outcome": record.get("outcome") or ("active" if number == current else ""),
                "cancelled": bool(record.get("cancelled")),
                "reviewers": [
                    {
                        "reviewer": name,
                        "verdict": done[name]["outcome"] if name in done else "pending",
                        "reportId": done[name]["id"] if name in done else None,
                        "model": done[name]["model"] if name in done else "",
                    }
                    for name in record["panel"]
                ],
            }
        )
    return result


def view(tasks, repository, number):
    """One review task: phase, blockers, reviewed snapshot beside the PR's current state, rounds and findings."""
    from .tasks import TaskNotFound, alert, findings

    target, fork, config, checkpoints, state = load(tasks, repository, number)
    try:
        pull = tasks.access.get(target.upstream, f"pulls/{number}")
    except NotFound:
        raise TaskNotFound(f"{repository}#{number} doesn't exist or isn't visible to you") from None
    owner, repo = target.upstream.split("/")
    stage = state["stage"]
    observed = {
        "head": pull["head"]["sha"],
        "baseRef": pull["base"]["ref"],
        "baseTip": pull["base"]["sha"],
        "title": pull["title"],
        "draft": bool(pull.get("draft")),
        "open": pull["state"] == "open",
        "merged": bool(pull.get("merged")),
    }
    check = state.get("check") or {}
    task = {
        "kind": STANDALONE_REVIEW,
        "owner": owner,
        "repo": repo,
        "pr": number,
        "title": pull["title"],
        "author": (pull.get("user") or {}).get("login", ""),
        "stage": stage,
        "active": stage in ACTIVE,
        "alert": alert(stage, state)
        if stage not in ("complete", *ACTIVE)
        else {
            "tone": "note",
            "title": "Review complete" if stage == "complete" else "Reviewing",
            "body": "Request the reviewer again for a new round."
            if stage == "complete"
            else "Reviewers are assessing the frozen snapshot.",
        },
        "round": state.get("round", 0),
        "snapshot": {
            "head": state.get("head") or "",
            "base": state.get("base") or "",
            "baseRef": state.get("base_ref") or "",
            "baseTip": state.get("base_tip") or "",
            "title": (state.get("requirements") or {}).get("title", ""),
        },
        "observed": observed,
        # The assessment belongs to its snapshot; a moved PR is shown, never re-labelled as reviewed.
        "changedSinceReview": observed["head"] != state.get("head") or observed["baseTip"] != state.get("base_tip"),
        "rounds": rounds(state),
        "findings": findings(state),
        "reports": [report_summary(entry) for entry in entries(state)],
        "budgets": {"modelCalls": {"used": state.get("calls", 0), "max": config.get("max_model_calls", 48)}},
        "check": {
            "round": check.get("round"),
            "attempt": check.get("attempt"),
            "published": check.get("id") is not None,
            "pending": bool(check.get("pending")),
            "completed": bool(check.get("completed")),
        },
        "enrolledAt": ((state.get("rounds") or [{}])[0]).get("opened") or pull["created_at"],
        "updatedAt": pull["updated_at"],
        "sponsorships": tasks.sponsorship_view(target.upstream, number),
        # From the verified audit, so progress shows before any review exists.
        "revision": hashlib.sha256(json.dumps([state.get("_audit"), pull["updated_at"]]).encode()).hexdigest(),
    }
    return {
        "task": task,
        "issueWriter": tasks.access.writable(target.upstream, tasks.user.login, tasks.user.id),
        "target": tasks.target_record(target, fork),
    }


def report(tasks, repository, number, identifier):
    """One complete report by its stable ID, with every referenced data part read and verified.

    Works for any task of a visible target. Model text is returned as data for the
    frontend's safe Markdown rendering; no stored HTML is served.
    """
    from .tasks import REPORTS, ReadCheckpoints, TaskNotFound

    target, _ = tasks.find_target(repository)
    config = tasks.task_config(target, repository)
    github = tasks.access.engine(repository)
    comments = github.pages(f"issues/{number}/comments")
    checkpoints = ReadCheckpoints(github, config, number, report_cache=REPORTS, fields=frozenset({"reports"}))
    state = checkpoints.load(comments)
    entry = next((e for e in entries(state or {}) if e["event"] == identifier), None)
    if entry is None:
        raise TaskNotFound("report not found")
    try:
        value = checkpoints.reports.get(entry["data"]["digest"], entry["data"]["artifacts"])
    except report_store.ReportDataError:
        # Missing or changed evidence is an integrity state, never an empty or substituted report.
        raise CheckpointError("report_data") from None
    publication = entry.get("publication") or {}
    content = report_store.report_content(value) if report_store.is_report(value) else value
    return {
        "report": {
            **report_summary(entry),
            "kind": publication.get("kind") or "",
            "title": publication.get("title") or "",
            "details": publication.get("details") or {},
            "digest": entry["data"]["digest"],
            "artifacts": entry["data"]["artifacts"],
            "content": content,
        }
    }
