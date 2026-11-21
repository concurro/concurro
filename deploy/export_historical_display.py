"""One-time, read-only migration of verified Python backoffice display evidence.

Run with PYTHONPATH=engine:contracts/python from the source checkout. No controller,
HTTP server, command client or writer GitHub credential is started.
"""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import sponsorship
from backoffice import review_tasks
from backoffice.auth import User
from backoffice.github import Access
from backoffice.settings import load
from backoffice.tasks import TaskNotFound, Tasks
from service.identity import Identity


def export_view(reader, kind, repository, number):
    """Reuse the verified legacy projector and retain the checkpoint reference and report bodies."""
    before = reader.state(repository, number, "summary", refresh=True)
    view = (
        reader.task_view(repository, number)
        if kind == "contribution"
        else review_tasks.view(reader, repository, number)
    )
    state = reader.state(repository, number, refresh=True)
    if not state:
        raise TaskNotFound("no verified historical checkpoint")
    after = reader.state(repository, number, "summary", refresh=True)
    if (before or {}).get("_revision") != (after or {}).get("_revision"):
        raise ValueError("historical checkpoint changed during export")
    reports = {
        entry["event"]: review_tasks.report(reader, repository, number, entry["event"])
        for entry in review_tasks.entries(state)
    }
    # Consent remains current in Rust; it is never revived from a checkpoint snapshot.
    view["task"].pop("sponsorships", None)
    view["issueWriter"] = False
    return {
        "kind": kind,
        "repository": repository,
        "number": number,
        "view": view,
        "provenance": {
            "checkpoint": (after or {}).get("_audit"),
            "source_revision": (after or {}).get("_revision"),
            "state_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
            "checkpoint_version": state.get("version"),
            "controller": state.get("controller"),
            "reader_app_id": reader.settings.reader.app_id,
        },
        "reports": reports,
        "usage": {
            "status": "available" if isinstance(state.get("credentials"), dict) else "unavailable",
            "charged_calls": [
                {
                    "id": identifier,
                    "kind": record.get("kind"),
                    "calls": record.get("calls"),
                    "profile": record.get("provider"),
                    "route": record.get("route"),
                    "version": record.get("version"),
                    "provider": sponsorship.ROUTES.get(record.get("route"), {}).get("sponsorship_provider")
                    or {"codex": "openai", "claude": "anthropic"}.get(record.get("provider")),
                    "sponsor": record.get("account") if record.get("kind") == "contributed" else None,
                }
                for identifier, record in (state.get("credentials") or {}).items()
            ],
        },
    }


def export(reader, deployment, observed_at, inventory=None):
    """Read all visible enrolled issues and standalone reviews, or an explicit source inventory."""
    datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if not deployment:
        raise ValueError("deployment is required")
    discover = inventory is None
    if inventory is None:
        inventory = []
        for target, _ in reader.visible_targets():
            for repository in target.issue_repositories:
                for issue in reader.access.engine(repository).pages("issues?state=all&per_page=100"):
                    labels = {label["name"] for label in issue.get("labels", [])}
                    if "pull_request" not in issue and (
                        "concurro" in labels or any(label.startswith("concurro:stage:") for label in labels)
                    ):
                        inventory.append({"kind": "contribution", "repository": repository, "number": issue["number"]})
            for pull in reader.access.engine(target.upstream).pages("pulls?state=all&per_page=100"):
                try:
                    review_tasks.load(reader, target.upstream, pull["number"])
                except TaskNotFound:
                    continue
                inventory.append({"kind": "review", "repository": target.upstream, "number": pull["number"]})
    views = []
    for source in inventory:
        try:
            views.append(export_view(reader, source["kind"], source["repository"], source["number"]))
        except TaskNotFound:
            if not discover:
                raise
    return {"format": 1, "deployment": deployment, "observed_at": observed_at, "views": views}


def main():
    """Write a private migration manifest; user identity is verified by the existing access checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--user-token-file", type=Path, required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    settings = load(args.settings)
    access = Access(Identity(settings.reader), args.user_token_file.read_text().strip())
    user = access.transport(settings.targets[0].upstream, as_user=True).request("GET", "user")
    reader = Tasks(
        settings, access, User(user["id"], user["login"], user.get("name") or "", user.get("avatar_url") or "")
    )
    inventory = json.loads(args.inventory.read_text()) if args.inventory else None
    manifest = export(reader, args.deployment, datetime.now(UTC).isoformat(), inventory)
    args.manifest.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
