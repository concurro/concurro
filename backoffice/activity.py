"""Recorded turns as the overview's activity feed and a target's Actions runs.

Both read the service's diagnostics rows only (`backoffice.turns.Diagnostics.recent`), so neither
calls GitHub: a target's run list names the runs the service recorded for its turns, and the feed
reports what those turns did. Run status comes from the recorded turn, not from a fresh Actions read.
"""

import json

from .turns import ROLES, STAGES, iso

# Turn outcomes as the frontend's `ActionsRun.status`; a turn with no outcome yet is queued or running.
RUN_STATUS = {
    "ok": "success",
    "failed": "failure",
    "uncertain": "failure",
    "lost": "failure",
    "timed_out": "failure",
    "terminated": "cancelled",
    "queue_expired": "cancelled",
}
# How each outcome reads in the feed, and the tone it carries.
OUTCOME_TEXT = {
    "ok": ("finished", "success"),
    "failed": ("failed", "danger"),
    "uncertain": ("ended with an uncertain result", "warning"),
    "lost": ("was lost", "danger"),
    "timed_out": ("timed out", "danger"),
    "terminated": ("was stopped", "warning"),
    "queue_expired": ("expired in the queue", "warning"),
}
ACTIVITY_ITEMS = 12
TARGET_RUNS = 50


def actor(row) -> str:
    """The turn's agent profile, falling back to its provider."""
    agent = json.loads(row["agent"]) if row["agent"] else {}
    return agent.get("profile") or row["provider"] or "concurro"


def role(row) -> str:
    """The role the stage assigns, for feed text; unmapped stages read as themselves."""
    return ROLES.get(row["stage"], STAGES.get(row["stage"], row["stage"]))


def assignment(row) -> tuple[int, int] | None:
    """The `(run_id, attempt)` the service recorded for this turn, when it recorded one."""
    assigned = json.loads(row["runner"]) if row["runner"] else None
    identity = assigned.get("run_id") if isinstance(assigned, dict) else None
    if not isinstance(identity, str):
        return None
    run, _, attempt = identity.partition("/")
    return (int(run), int(attempt)) if run.isdigit() and attempt.isdigit() else None


def feed(rows, limit: int = ACTIVITY_ITEMS) -> list[dict]:
    """Recorded turns as activity items, newest first."""
    items = []
    for row in rows[:limit]:
        owner, _, repo = row["repository"].partition("/")
        outcome = row["outcome"] or ""
        text, tone = OUTCOME_TEXT.get(
            outcome, ("is running", "neutral") if row["started"] else ("is queued", "neutral")
        )
        items.append(
            {
                "id": row["id"],
                "at": iso(row["finished"] or row["started"] or row["submitted"]),
                "actor": actor(row),
                "text": f"{text} the {STAGES.get(row['stage'], row['stage'])} turn",
                "task": {"owner": owner, "repo": repo, "issue": row["issue"]},
                "tone": tone,
            }
        )
    return items


def runs(rows, limit: int = TARGET_RUNS) -> list[dict]:
    """The Actions runs recorded for these turns, newest first; turns without one are left out."""
    found = []
    for row in rows:
        identity = assignment(row)
        if identity is None:
            continue
        run, attempt = identity
        elapsed = row["elapsed_seconds"]
        if row["finished"] and row["started"]:
            elapsed = row["finished"] - row["started"]
        found.append(
            {
                "id": run,
                "attempt": attempt,
                "title": f"{role(row)} turn {row['turn']}",
                "status": RUN_STATUS.get(row["outcome"] or "", "in_progress" if row["started"] else "queued"),
                "issue": row["issue"],
                "turn": row["turn"],
                "startedAt": iso(row["started"] or row["submitted"]),
                "durationS": round(elapsed) if elapsed else 0,
            }
        )
        if len(found) == limit:
            break
    return found
