"""Actions evidence for recorded turns, read with the Backoffice App's repository token."""

import json
import re
from datetime import datetime
from urllib.parse import urlencode

from github import NotFound, Transient
from service.admission import DISPATCH, EXECUTOR_WORKFLOW, RUN_BRANCH_PREFIX
from service.reconciler import pages

from .turns import ROLES, STAGES


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


# Actions run status as the frontend's `ActionsRun.status`; a completed run reads from its conclusion.
QUEUED = ("queued", "waiting", "pending", "requested")
CONCLUSION = {"success": "success", "cancelled": "cancelled", "skipped": "cancelled", "stale": "cancelled"}
TARGET_RUNS = 50


def run_status(run) -> str:
    """One listed run as the frontend spells it: GitHub's own status, never a turn's outcome."""
    if run.get("status") != "completed":
        return "queued" if run.get("status") in QUEUED else "in_progress"
    return CONCLUSION.get(run.get("conclusion") or "", "failure")


def elapsed(run) -> int:
    """Wall-clock seconds GitHub reports for the run; 0 while it is still going."""
    started, updated = run.get("run_started_at"), run.get("updated_at")
    if run.get("status") != "completed" or not started or not updated:
        return 0
    try:
        return max(0, round((datetime.fromisoformat(updated) - datetime.fromisoformat(started)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def recorded_run(row):
    """The `(run, attempt)` the service recorded as having served this turn, if it recorded one."""
    assigned = json.loads(row["runner"]) if row["runner"] else None
    identity = assigned.get("run_id") if isinstance(assigned, dict) else None
    if not isinstance(identity, str):
        return None
    run, _, attempt = identity.partition("/")
    return (run, attempt) if run.isdigit() and attempt.isdigit() else None


def sessions_of(row) -> list:
    """Every runner session dispatched for this turn."""
    found = json.loads(row["runner_sessions"] or "[]")
    return [session for session in found if isinstance(session, str)]


def served(rows) -> dict:
    """Index recorded turns by the run that served them, and by the sessions merely dispatched.

    A turn whose assignment names another run was not served by this one, however its session
    was dispatched; keeping the two apart preserves the distinction `for_turn` already makes.
    """
    by_run, by_session = {}, {}
    for row in rows:
        identity = recorded_run(row)
        if identity:
            by_run.setdefault(identity[0], []).append(row)
            continue
        for session in sessions_of(row):
            by_session.setdefault(session, []).append(row)
    return {"run": by_run, "session": by_session}


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

    def for_target(self, lookup, limit=TARGET_RUNS):
        """GitHub's own recent executor runs, attributed to the turns the service recorded for them.

        `lookup(sessions)` supplies the recorded turns of those runner sessions, so attribution
        never depends on how recent a turn is. One run serves every turn of its session, so each
        run appears once; a run with no recorded turn is still listed, without a task.

        Only attempt 1 can serve a session (`gateway/exchange`), so a rerun's own status is not
        the turns' evidence: their attempt is read exactly when the listing returns a later one.
        """
        try:
            query = urlencode({"event": "workflow_dispatch", "per_page": limit})
            listing = self.reader.request("GET", f"{self.root}/workflows/{EXECUTOR_WORKFLOW}/runs?{query}")
            listed = listing["workflow_runs"] if isinstance(listing.get("workflow_runs"), list) else []
        except (NotFound, Transient, RuntimeError, OSError, ValueError, KeyError, TypeError):
            return {"runs": [], "message": "Actions runs could not be loaded from GitHub."}
        branches = {str(run.get("head_branch") or "").removeprefix(RUN_BRANCH_PREFIX): run for run in listed}
        index = served(lookup([session for session in branches if DISPATCH.fullmatch(session)]))
        found, unreadable = [], False
        for run in listed:
            session = str(run.get("head_branch") or "").removeprefix(RUN_BRANCH_PREFIX)
            turns = index["run"].get(str(run.get("id")))
            association = "assigned" if turns else None
            if not turns:
                turns = index["session"].get(session)
                association = "dispatched" if turns else None
            served_run = run
            if turns and run.get("run_attempt") != 1:
                try:
                    served_run = self.reader.request("GET", f"{self.root}/runs/{run['id']}/attempts/1")
                except (NotFound, Transient, RuntimeError, OSError, ValueError, KeyError, TypeError):
                    unreadable = True
                    continue
            found.append(self.attributed(served_run, sorted(turns or [], key=lambda row: row["turn"]), association))
        message = (
            "Some reruns could not be read from GitHub; their original attempts are not shown." if unreadable else None
        )
        return {"runs": found, "message": message}

    def attributed(self, run, turns, association) -> dict:
        """One listed run with the task and turn numbers it served, if any turn records it."""
        record = {
            "id": run["id"],
            "attempt": run.get("run_attempt"),
            "title": "Executor run",
            "status": run_status(run),
            "association": association,
            "task": None,
            "issue": None,
            "turn": None,
            "startedAt": run.get("run_started_at") or run.get("created_at"),
            "durationS": elapsed(run),
        }
        if not turns:
            return record
        row = turns[0]
        numbers = [turn["turn"] for turn in turns]
        owner, _, repo = row["repository"].partition("/")
        label = ROLES.get(row["stage"], STAGES.get(row["stage"], row["stage"]))
        span = f"turn {numbers[0]}" if len(numbers) == 1 else f"turns {numbers[0]}–{numbers[-1]}"
        return record | {
            "title": f"{label} {span}",
            "task": {"owner": owner, "repo": repo, "issue": row["issue"]},
            "issue": row["issue"],
            "turn": numbers[0],
        }

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
