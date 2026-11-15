"""A standalone review round's progress as an App-owned Check Run.

The check is a derived view of checkpoint state: it shows activity only, always ends
neutral, and its failures never change a verdict or authorize work.
"""

import sys
import time
from datetime import datetime, timezone

import markup
import reason_guidance
from actions import BUNDLE_REASON
from github import Rejected
from markup import code, link, text
from policy import digest

NAME = "Concurro review - PR {pr}"
QUEUED, REVIEWING, FINISHED = "Queued", "Reviewing", "Finished"
ACTIVE = ("queued", "review", "publishing")
COMPLETE = "Review of this snapshot complete; re-request {reviewer} for reassessment."
RETRY = f"Once the cause is fixed, a writer can comment {code('/concurro retry')}."
REASONS = {
    "awaiting_credentials": (
        "Review paused: a model provider has no funding consent or usable credentials for this pull request. "
        f"A writer can comment {code('/concurro funding <provider> personal')} or "
        f"{code('/concurro funding <provider> org')}, then {code('/concurro retry')}. "
        "Credentials themselves are set up with an operator."
    ),
    "budget_exhausted": "Review paused: this task's model-call budget is used up. An operator must raise it.",
    "stopped": f"Review stopped by a writer. Comment {code('/concurro retry')} to continue.",
}
CLOSED = "The pull request was closed before the review finished."
RESUME = (
    "Concurro's configuration or instructions changed after this pull request was enrolled. "
    f"A writer can comment {code('/concurro resume')} to continue with the current settings."
)
CONFIG_REASON = "controller configuration changed; a writer can use /concurro resume"


def now():
    """The current UTC time, to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_round(state):
    """The round in progress or last finished, or None before the first."""
    rounds = state.get("rounds") or []
    return rounds[-1] if rounds else None


def pull_url(state, config):
    """The reviewed pull request on GitHub; the only navigation a check offers."""
    return f"https://github.com/{config['repository']}/pull/{state['pr']}"


def review_links(state, config, number):
    """Links to the round's published native reviews, by reviewer profile."""
    return [
        link(text(receipt["provider"]), f"{pull_url(state, config)}#pullrequestreview-{receipt['review']}")
        for receipt in state.get("review_receipts", [])
        if receipt.get("round") == number
    ]


def reason(state):
    """Why a round stopped, in public terms with the applicable command; never private diagnostics."""
    stage, recorded = state["stage"], state.get("reason") or ""
    if stage in REASONS:
        return REASONS[stage]
    if stage == "closed":
        return CLOSED
    if recorded in (BUNDLE_REASON, CONFIG_REASON):
        return RESUME
    operator = reason_guidance.operator_summary(recorded, stage)
    if operator:
        return operator + " " + reason_guidance.OPERATOR_ACTION
    summary = markup.first_sentence(recorded) or markup.one_line(recorded)[:300]
    return (text(summary) + " " if summary else "The review could not continue. ") + RETRY


def snapshot_lines(record):
    """The reviewed snapshot a finished check records."""
    return [
        "Reviewed head " + code(record["head"]),
        f"Base {code(record['base_ref'])} at {code(record['base_tip'])} · merge base {code(record['base'])}",
        f"Round {record['round']}",
    ]


def desired(state, config):
    """The check's status, conclusion and output for the task's current state."""
    record = current_round(state)
    stage = state["stage"]
    panel = record["panel"]
    done = sum(provider in state.get("reviews", {}) for provider in panel)
    progress = f"{done} of {len(panel)} reviewers complete."
    if stage == "queued":
        return {
            "status": "queued",
            "output": {"title": QUEUED, "summary": "Review accepted for " + code(record["head"][:7]) + "."},
        }
    if stage in ("review", "publishing"):
        summary = progress + (" Publishing reviews." if stage == "publishing" else "")
        return {"status": "in_progress", "output": {"title": REVIEWING, "summary": summary}}
    links = review_links(state, config, record["round"])
    lines = []
    if stage == "complete" and record.get("outcome") == "complete":
        reviewer = (config.get("actors") or {}).get("reviewer", {}).get("login", "the reviewer")
        lines.append(COMPLETE.format(reviewer=text(reviewer)))
    elif record.get("outcome") == "cancelled":
        lines.append("The review request was removed before the review finished. " + progress)
    elif record.get("outcome") == "closed":
        # Still true once the PR is reopened: that round ended with the closure.
        lines.append(CLOSED)
    else:
        lines.append(reason(state))
    if links:
        lines.append("Reviews: " + " · ".join(links))
    lines.append(" · ".join(snapshot_lines(record) + ["Finished " + (record.get("completed") or now())]))
    return {
        "status": "completed",
        # Every terminal outcome is neutral: the check reports activity, never a verdict.
        "conclusion": "neutral",
        "output": {"title": FINISHED, "summary": "\n\n".join(lines)},
    }


def external_id(state, config, check):
    """Correlates a run with its task, round and display attempt; not an idempotency key."""
    return f"concurro:{config['repository']}#{state['pr']}:{check['round']}:{check['attempt']}"


# Readbacks that must miss an uncertain create before another is sent; visibility can lag a reply.
READBACKS = 2
# How long a run of unknown creation is still looked for after another was created in its place.
RECHECK_SECONDS = 3600
SUPERSEDED = {
    "status": "completed",
    "conclusion": "neutral",
    "output": {"title": FINISHED, "summary": "Superseded by a later check of this pull request's review."},
}


class Waiting(RuntimeError):
    """An uncertain create is not visible yet; it is looked for again before any other write."""


def find(github, state, head, identifier):
    """The runs earlier creates left on a commit, oldest first, by the App's own name and correlation value."""
    name = NAME.format(pr=state["pr"])
    runs = github.pages(f"commits/{head}/check-runs?filter=all", "check_runs")
    return sorted(
        (run for run in runs if run.get("name") == name and run.get("external_id") == identifier),
        key=lambda run: run["id"],
    )


def create(state, github, config, check, identifier, wanted, checkpoint):
    """The run's ID: an existing run, else a new one whose uncertainty is saved before it is sent.

    `external_id` correlates but does not deduplicate. After a create of unknown outcome,
    including one cut short by a restart, the run is looked for `READBACKS` times before
    another is sent, and `recheck` is the time until which a second run may still surface.
    """
    matches = find(github, state, check["head"], identifier)
    if matches:
        check.pop("uncertain", None)
        # GitHub shows the newest run of a name: that one stays live, any older is finished.
        if len(matches) > 1:
            check.setdefault("recheck", time.time() + RECHECK_SECONDS)
        return matches[-1]["id"], False
    if 0 < check.get("uncertain", 0) <= READBACKS:
        check["uncertain"] += 1
        raise Waiting("check creation outcome unknown")
    # Once an earlier create's outcome was unknown, a second run may still surface later.
    repeated = bool(check.get("uncertain"))
    check["uncertain"] = 1
    check.setdefault("recheck", time.time() + RECHECK_SECONDS)
    checkpoint()
    body = {
        "name": NAME.format(pr=state["pr"]),
        "head_sha": check["head"],
        "external_id": identifier,
        # Without it GitHub links the App's homepage.
        "details_url": pull_url(state, config),
        **wanted,
    }
    try:
        created = github.repo("check-runs", "POST", body)
    except Rejected:
        # Definitely not created: nothing to look for.
        check.pop("uncertain")
        if not repeated:
            check.pop("recheck")
        raise
    check.pop("uncertain")
    if not repeated:
        check.pop("recheck")
    return created["id"], True


def finish_others(github, state, head, identifier, keep):
    """Finish every unfinished run of one correlation value except `keep`, so none stays queued."""
    for run in find(github, state, head, identifier):
        if run["id"] != keep and run.get("status") != "completed":
            github.repo(f"check-runs/{run['id']}", "PATCH", SUPERSEDED)


def newest(github, state, check, identifier):
    """Move the live check to the newest run found, which is the one GitHub displays.

    A run of unknown creation can become visible after an older one was adopted; the
    desired status is then sent to it, and the older run is finished with the others.
    """
    matches = find(github, state, check["head"], identifier)
    if matches and matches[-1]["id"] != check["id"]:
        check.update(id=matches[-1]["id"], sent=None)


def retire(state, config, check):
    """Keep reconciling a replaced check while one of its runs may still be unfinished or surface.

    A replaced check that never completed has all its runs finished; one with an open
    recheck window keeps its own run and has any other finished.
    """
    unfinished = not check.get("completed")
    until = max(check.get("recheck", 0), time.time() + RECHECK_SECONDS if unfinished else 0)
    if until:
        state.setdefault("check_rechecks", []).append(
            {
                "head": check["head"],
                "identifier": external_id(state, config, check),
                "keep": None if unfinished else check["id"],
                "until": until,
            }
        )


def reconcile(state, github):
    """Settle the runs of earlier checks; one scan cannot rule out a run not visible yet, so each stays until its window ends."""
    remaining = []
    for old in state.get("check_rechecks", []):
        finish_others(github, state, old["head"], old["identifier"], old["keep"])
        if time.time() < old["until"]:
            remaining.append(old)
    if remaining:
        state["check_rechecks"] = remaining
    elif state.pop("check_rechecks", None) is not None:
        return True
    return False


def unsettled(state):
    """Whether a check update is owed or a run of unknown creation may still surface."""
    check = state.get("check") or {}
    return bool(check.get("pending") or check.get("recheck") or state.get("check_rechecks"))


def sync(state, github, config, checkpoint):
    """Create or update the round's check to match the state; return whether it is current.

    A round gets a new run, as does work restarted after its run completed. A failure is
    recorded as pending in the checkpoint and retried on a later save.
    """
    record = current_round(state)
    if record is None:
        return True
    check = state.get("check")
    wanted = desired(state, config)
    restarted = check and check.get("completed") and wanted["status"] != "completed"
    if not check or check["round"] != record["round"] or restarted:
        attempt = (check["attempt"] + 1) if check and check["round"] == record["round"] else 1
        if check:
            retire(state, config, check)
        check = state["check"] = {"round": record["round"], "attempt": attempt, "head": record["head"], "id": None}
        checkpoint()
    signature = digest(wanted)
    identifier = external_id(state, config, check)
    try:
        if reconcile(state, github):
            checkpoint()
        if check.get("sent") == signature and not check.get("recheck"):
            if check.pop("pending", None):
                checkpoint()
            return True
        if check["id"] is None:
            check["id"], created = create(state, github, config, check, identifier, wanted, checkpoint)
            if created:
                check["sent"] = signature
            checkpoint()
        if check.get("recheck"):
            newest(github, state, check, identifier)
        if check.get("sent") != signature:
            github.repo(f"check-runs/{check['id']}", "PATCH", wanted)
            check["sent"] = signature
        if check.get("recheck"):
            finish_others(github, state, check["head"], identifier, check["id"])
            if time.time() >= check["recheck"]:
                check.pop("recheck")
        check["completed"] = wanted["status"] == "completed"
        check.pop("pending", None)
        checkpoint()
        return True
    except Exception as error:
        # Display only: never a reason to stop or repeat review work. Saved, so a later tick retries it.
        check["pending"] = True
        checkpoint()
        print(f"Concurro check update failed ({type(error).__name__}); retained for retry.", file=sys.stderr)
        return False
