"""Diagnose runner termination from the OIDC-bound Actions run."""

import re
from datetime import datetime

from .admission import RUN_BRANCH_PREFIX


def runner_termination(reader, fork, config_sha, observation):
    """Diagnose only the OIDC-bound job after exchange loss; unavailable evidence stays unknown."""
    runner = observation.get("runner", {})
    session, identity = runner.get("session", ""), runner.get("run_id", "")
    if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", session):
        return None
    if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]*/1", identity):
        return None
    run_id, attempt = identity.split("/")
    path = f"repos/{fork}/actions/runs/{run_id}/attempts/{attempt}"
    try:
        run = reader.request("GET", path)
        if (
            str(run.get("id")) != run_id
            or run.get("run_attempt") != 1
            or run.get("head_branch") != RUN_BRANCH_PREFIX + session
            or run.get("head_sha") != config_sha
            or run.get("event") != "workflow_dispatch"
            or run.get("status") != "completed"
        ):
            return None
        jobs = reader.request("GET", path + "/jobs?per_page=100").get("jobs", [])
    except Exception:
        # Diagnostics must not turn runner loss into an automatically retried transport error.
        return None
    conclusion = run.get("conclusion")
    if any(job.get("conclusion") == "timed_out" for job in jobs):
        conclusion = "timed_out"
    labels = {
        "timed_out": "timed out",
        "cancelled": "was cancelled",
        "failure": "failed",
        "success": "finished without delivering the result",
    }
    label = labels.get(conclusion, "ended without a result")
    duration = ""
    try:
        starts = [datetime.fromisoformat(job["started_at"].replace("Z", "+00:00")) for job in jobs]
        ends = [datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00")) for job in jobs]
        seconds = int((max(ends) - min(starts)).total_seconds())
        if 0 <= seconds <= 86400:
            duration = f" after {seconds // 60}m {seconds % 60}s of job time"
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return (
        f"Runner {label}{duration}: https://github.com/{fork}/actions/runs/{run_id}. "
        "No result was saved; work may be incomplete. No automatic restart was attempted. "
        "Inspect the run before a writer retries."
    )
