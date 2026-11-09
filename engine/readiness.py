"""Current-head review and CI eligibility for marking a draft PR ready."""

from model_config import assigned
from policy import approval_valid, in_scope, path_allowed

PASSING = ("success", "neutral", "skipped")


def latest_by(items, key, ident):
    """Keep the highest-ID item per key, preserving first-key order and first-wins ties."""
    latest = {}
    for item in items:
        name = key(item)
        if name not in latest or ident(item) > ident(latest[name]):
            latest[name] = item
    return latest


def review_reasons(state, pr, files, threads, config):
    """Concurro's publication/review conditions, independent of GitHub merge policy."""
    reasons = []
    if pr.get("state") != "open":
        reasons.append("PR is closed")
    if pr.get("user", {}).get("login") != config["bot_login"] or pr.get("number") != state.get("pr"):
        reasons.append("PR identity differs from the controller publication")
    head = pr.get("head", {})
    if (
        head.get("sha") != state.get("head")
        or head.get("ref") != state["branch"]
        or head.get("repo", {}).get("full_name", "").lower() != config.get("fork", config["repository"]).lower()
    ):
        reasons.append("head changed outside the controller")
    if pr.get("base", {}).get("ref") != config["base"]:
        reasons.append("PR targets a different base branch")
    if not approval_valid(state, config):
        reasons.append("approved plan missing or stale")
    for provider in assigned(config, "pr_reviewers"):
        report = state.get("reviews", {}).get(provider, {})
        if (
            report.get("head") != state.get("head")
            or report.get("verdict") != "approve"
            or report.get("findings")
            or report.get("input_revision") != state.get("input_revision", 0)
        ):
            reasons.append(provider + " approval missing or stale")
    if state.get("pr_outbox") or state.get("conversation", {}).get("pending"):
        reasons.append("review publication incomplete")
    if state.get("repair_requested") or any(f["status"] == "open" for f in state.get("findings", [])):
        reasons.append("open review findings")
    if any(f["status"] == "withdrawn" for f in state.get("findings", [])):
        reasons.append("withdrawn findings awaiting retirement")
    if any(not t["isResolved"] for t in threads):
        reasons.append("unresolved review threads")
    if not files or set(files) != set(state.get("changed_files", [])):
        reasons.append("PR file set differs from controller evidence")
    if not all(path_allowed(p, config) and in_scope(p, state["plan"]) for p in files):
        reasons.append("unapproved path")
    return reasons


def ci_refs(pr):
    """The current head and, when available, GitHub's current test-merge commit."""
    return list(
        dict.fromkeys(
            [pr["head"]["sha"]]
            + ([pr["merge_commit_sha"]] if pr.get("merge_commit_sha") and not pr.get("merged") else [])
        )
    )


def empty_external_suite(suite, populated):
    """Ignore only explicitly empty, queued non-Actions suites with no reported conclusion."""
    app = suite.get("app") or {}
    slug = app.get("slug")
    count = suite.get("latest_check_runs_count")
    identifier = suite.get("id")
    return (
        type(identifier) is int
        and identifier > 0
        and identifier not in populated
        and isinstance(slug, str)
        and bool(slug.strip())
        and slug.lower() != "github-actions"
        and type(count) is int
        and count == 0
        and suite.get("status") == "queued"
        and "conclusion" in suite
        and suite["conclusion"] is None
    )


def ci_reasons(observations):
    """Require a real success and no nonpassing latest check run or commit status."""
    reasons, success = [], False
    for sha, checks, statuses, *extra in observations:
        suites, runs = extra if extra else ([], [])
        # Workflow IDs are required; checks and statuses deliberately accept missing IDs as zero.
        workflows = latest_by(
            runs,
            lambda run: (run.get("workflow_id", run["id"]), run.get("event"), run.get("head_branch")),
            lambda run: run["id"],
        )
        active_suites = {r.get("check_suite_id") for r in workflows.values()}
        superseded = {r.get("check_suite_id") for r in runs} - active_suites - {None}
        populated = {(check.get("check_suite") or {}).get("id") for check in checks}
        for suite in suites:
            if suite.get("id") in superseded:
                continue
            if suite.get("head_sha") == sha and empty_external_suite(suite, populated):
                continue
            if suite.get("head_sha") != sha or suite.get("status") != "completed":
                reasons.append("CI suite pending: " + str(suite.get("id")))
            elif suite.get("conclusion") not in PASSING:
                reasons.append("CI suite unsuccessful: " + str(suite.get("id")))
        for run in workflows.values():
            name = run.get("name") or str(run["id"])
            if run.get("head_sha") != sha or run.get("status") != "completed":
                reasons.append("CI workflow pending: " + name)
            elif run.get("conclusion") not in PASSING:
                reasons.append("CI workflow unsuccessful: " + name)
        latest = latest_by(
            (check for check in checks if (check.get("check_suite") or {}).get("id") not in superseded),
            lambda check: (check.get("app", {}).get("id"), (check.get("check_suite") or {}).get("id"), check["name"]),
            lambda check: check.get("id", 0),
        )
        for check in latest.values():
            conclusion = check.get("conclusion")
            if check.get("head_sha") != sha or check.get("status") != "completed":
                reasons.append("CI pending: " + check["name"])
            elif conclusion == "success":
                success = True
            elif conclusion not in ("neutral", "skipped"):
                reasons.append("CI unsuccessful: " + check["name"])
        latest = latest_by(statuses, lambda status: status["context"], lambda status: status.get("id", 0))
        for name, status in latest.items():
            if status.get("state") == "success":
                success = True
            else:
                reasons.append("CI " + str(status.get("state", "unknown")) + ": " + name)
    if not success:
        reasons.append("CI has no successful result yet")
    return list(dict.fromkeys(reasons))
