"""The overview's activity feed: what the service's recorded turns did, newest event first.

Rows come from `backoffice.turns.Diagnostics.recent`, so the feed reads no GitHub. A target's
Actions runs are not derived here: `backoffice.runs.Runs.for_target` reads GitHub for their status.
"""

import json

from .turns import STAGES, iso

# A recorded status that ends a turn without an outcome: the runner stopped reporting.
UNSETTLED = {"lost": ("stopped reporting on", "danger"), "cancelled": ("had its runner end", "warning")}
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


def actor(row) -> str:
    """The turn's agent profile, falling back to its provider."""
    agent = json.loads(row["agent"]) if row["agent"] else {}
    return agent.get("profile") or row["provider"] or "concurro"


def event(row) -> tuple[str, str]:
    """What the turn did and the tone it carries; a lost runner is never still running."""
    if row["outcome"]:
        return OUTCOME_TEXT.get(row["outcome"], (f"ended as {row['outcome']}", "warning"))
    if row["status"] in UNSETTLED:
        return UNSETTLED[row["status"]]
    return ("is running", "neutral") if row["started"] else ("is queued", "neutral")


def feed(rows, limit: int = ACTIVITY_ITEMS) -> list[dict]:
    """Recorded turns as activity items, newest first."""
    items = []
    for row in rows[:limit]:
        owner, _, repo = row["repository"].partition("/")
        text, tone = event(row)
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
