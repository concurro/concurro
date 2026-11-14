"""Read-only phase 0 evidence for the list index: App settings and webhook delivery history.

Run inside the service container, from the checkout root:

    python3 - /etc/concurro/service/settings.json < phase-0-capture.py > phase-0-host.json

Every request is a GET. The output holds counts, identifiers and numeric fields only: no
tokens, no payload text.
"""

import json
import sys
import time
from collections import Counter

from service import settings as service_settings
from service.identity import Identity, IdentityError
from service.transport import Transport
from service.webhook_recovery import Recovery

# Events the list index would apply.
APPLIED = ("issues", "issue_comment", "pull_request", "check_run", "check_suite", "workflow_run")
SAMPLES = 5


def newest(attempts):
    """The latest attempt of one delivery GUID."""
    return max(attempts, key=lambda item: item["id"])


def detail(identity, attempt, failures):
    """One delivery's request payload, or None when the App client cannot read it."""
    try:
        found = identity.api("GET", f"app/hook/deliveries/{attempt['id']}", identity.app_jwt())
        return (found.get("request") or {}).get("payload") or {}
    except IdentityError as error:
        failures[str(error)] += 1
        return None


def capture(settings, identity=None, reader=None, clock=time.time):
    """Collect the evidence as one JSON-serializable document."""
    identity = identity or Identity(settings)
    reader = reader or (lambda repository: Transport(repository, identity.credential(repository, "read"), time.time))
    app = identity.api("GET", "app", identity.app_jwt())
    bot = identity.verify()
    repositories, names = {}, {}
    for target in settings.targets:
        roles = {target.upstream: "upstream", target.fork: "fork"}
        for repository in target.repositories:
            installation = identity.installation(repository)
            granted = identity.api("GET", f"app/installations/{installation}", identity.app_jwt())
            number = reader(repository).request("GET", f"repos/{repository}")["id"]
            names[number] = repository
            repositories[repository] = {
                "role": roles[repository] if target.upstream != target.fork else "upstream and fork",
                "issues": repository in target.issue_repositories,
                "id": number,
                "installation_events": sorted(granted.get("events", [])),
                "installation_permissions": granted.get("permissions", {}),
            }
    scan = Recovery.__new__(Recovery)
    scan.identity = identity
    scan.installations = {repository: identity.installation(repository) for repository in repositories}
    started = clock()
    groups = scan.deliveries()
    seconds = clock() - started
    counts, statuses, days, fields, oldest = Counter(), Counter(), Counter(), Counter(), None
    picked = {"labeled": [], "check_run": [], "synchronize": []}
    for attempts in groups.values():
        item = newest(attempts)
        fields.update(key for key in ("event", "action", "delivered_at", "repository_id") if item.get(key) is not None)
        repository = names.get(item.get("repository_id"), "other")
        event, action = item.get("event"), item.get("action")
        counts[f"{repository} {event}.{action}"] += 1
        if event in APPLIED and repository != "other":
            statuses[str(item.get("status_code"))] += 1
            days[str(item.get("delivered_at"))[:10]] += 1
            oldest = min(filter(None, (oldest, item.get("delivered_at"))), default=None)
            kind = (
                "labeled"
                if event == "issues" and action in ("labeled", "unlabeled")
                else action
                if event == "pull_request"
                else event
            )
            if kind in picked and len(picked[kind]) < SAMPLES:
                picked[kind].append((repository, item))
    failures, samples = Counter(), {"bot_label_events": [], "check_runs": [], "synchronize": []}
    for _, item in picked["labeled"]:
        payload = detail(identity, item, failures)
        if payload is not None:
            label = (payload.get("label") or {}).get("name", "")
            samples["bot_label_events"].append(
                {
                    "sent_by_bot": (payload.get("sender") or {}).get("login") == bot,
                    "stage_label": label.startswith("concurro:stage:"),
                }
            )
    for repository, item in picked["check_run"]:
        payload = detail(identity, item, failures)
        if payload is not None:
            run = payload.get("check_run") or {}
            samples["check_runs"].append(
                {
                    "repository": repository,
                    "listed_pull_requests": len(run.get("pull_requests") or []),
                    "has_head_sha": bool(run.get("head_sha")),
                }
            )
    for repository, item in picked["synchronize"]:
        payload = detail(identity, item, failures)
        if payload is not None:
            pull = payload.get("pull_request") or {}
            now = reader(repository).request("GET", f"repos/{repository}/pulls/{pull.get('number')}")
            samples["synchronize"].append(
                {
                    "payload": [pull.get("additions"), pull.get("deletions")],
                    "now": [now.get("additions"), now.get("deletions")],
                    "head_unchanged": (pull.get("head") or {}).get("sha") == now["head"]["sha"],
                }
            )
    return {
        "app_events": sorted(app.get("events", [])),
        "app_permissions": app.get("permissions", {}),
        "repositories": repositories,
        "history": {
            "deliveries": len(groups),
            "scan_seconds": round(seconds, 1),
            "oldest_applied": oldest,
            "summary_fields_present": dict(fields),
            "applied_per_day": dict(sorted(days.items())),
            "applied_status_codes": dict(statuses),
            "by_repository_event_action": dict(sorted(counts.items())),
        },
        "samples": samples,
        "unreadable_details": dict(failures),
    }


if __name__ == "__main__":
    json.dump(capture(service_settings.load(sys.argv[1])), sys.stdout, indent=2, sort_keys=True)
