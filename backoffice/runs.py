"""Actions evidence for recorded turns, read with the Backoffice App's repository token."""

import json
import re
from datetime import datetime
from urllib.parse import urlencode

from github import NotFound, Transient
from service.admission import DISPATCH, EXECUTOR_WORKFLOW, RUN_BRANCH_PREFIX
from service.reconciler import pages


def job_record(job):
    """Project GitHub job facts; missing timestamps and runner assignment stay unknown."""
    duration = None
    if job.get("started_at") and job.get("completed_at"):
        try:
            duration = (
                datetime.fromisoformat(job["completed_at"]) - datetime.fromisoformat(job["started_at"])
            ).total_seconds()
            if duration < 0:
                duration = None
        except (TypeError, ValueError):
            pass
    return {
        "id": job["id"],
        "name": job["name"],
        "url": job["html_url"],
        "status": job["status"],
        "conclusion": job.get("conclusion"),
        "durationS": duration,
        "runner": job.get("runner_name") or None,
        "labels": job.get("labels", []),
    }


class Runs:
    """Resolve assigned runs or historical dispatches for one visible fork."""

    def __init__(self, reader, fork):
        """Use the already authorized fork transport."""
        self.reader, self.fork = reader, fork
        self.root = f"repos/{fork}/actions"

    def for_turn(self, row):
        """Prefer the recorded assignment; old rows can only establish associated dispatches."""
        row = dict(row)
        assigned = json.loads(row["runner"]) if row.get("runner") else None
        sessions = json.loads(row["runner_sessions"] or "[]")
        if assigned:
            sessions = [assigned.get("session")]
        runs, messages = [], []
        for session in dict.fromkeys(sessions):
            if not isinstance(session, str) or not DISPATCH.fullmatch(session):
                continue
            identity = assigned.get("run_id") if assigned else None
            try:
                if isinstance(identity, str) and re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", identity):
                    run_id, attempt = map(int, identity.split("/"))
                    run = self.reader.request("GET", f"{self.root}/runs/{run_id}/attempts/{attempt}")
                    found = [run] if run.get("id") == run_id and run.get("run_attempt") == attempt else []
                else:
                    query = urlencode({"branch": RUN_BRANCH_PREFIX + session, "event": "workflow_dispatch"})
                    found = pages(
                        self.reader, f"{self.root}/workflows/{EXECUTOR_WORKFLOW}/runs?{query}", "workflow_runs"
                    )
                    # Only attempt 1 can claim a Concurro session; later reruns are unrelated to its turns.
                    found = [self.reader.request("GET", f"{self.root}/runs/{run['id']}/attempts/1") for run in found]
                found = [
                    run
                    for run in found
                    if run.get("head_branch") == RUN_BRANCH_PREFIX + session
                    and run.get("event") == "workflow_dispatch"
                    and str(run.get("path", "")).split("@", 1)[0] == f".github/workflows/{EXECUTOR_WORKFLOW}"
                ]
                for run in found:
                    runs.append(self.record(run, session, "assigned" if assigned else "dispatched"))
                if not found:
                    messages.append(
                        "No matching run is available for a recorded session. GitHub history may have been deleted."
                    )
            except (NotFound, Transient, RuntimeError, OSError, ValueError, KeyError, TypeError):
                messages.append("Actions run details could not be loaded from GitHub.")
        if not runs and not messages:
            messages.append("No Actions run identity was recorded for this turn.")
        return {"runs": runs, "message": " ".join(dict.fromkeys(messages)) or None}

    def record(self, run, session, association):
        """Load every job of the exact attempt without hiding a run when its jobs are unavailable."""
        run_id, attempt = run["id"], run["run_attempt"]
        jobs = None
        try:
            jobs = [
                job_record(job)
                for job in pages(self.reader, f"{self.root}/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
            ]
        except (NotFound, Transient, RuntimeError, OSError, ValueError, KeyError, TypeError):
            pass
        return {
            "id": run_id,
            "attempt": attempt,
            "url": f"https://github.com/{self.fork}/actions/runs/{run_id}/attempts/{attempt}",
            "workflow": run["path"],
            "status": run["status"],
            "conclusion": run.get("conclusion"),
            "session": session,
            "association": association,
            "jobs": jobs,
        }
