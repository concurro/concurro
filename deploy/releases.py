"""Resolve exact main releases and wait for both repositories' CI-published images."""

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPOSITORIES = ("concurro/concurro", "concurro/concurro-backoffice")


def github(path):
    """Read public release metadata; an optional GH_TOKEN provides authenticated API quota."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "concurro-host-update"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request("https://api.github.com/repos/" + path, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise SystemExit(f"GitHub returned HTTP {error.code} while checking releases; try again later.") from None
    except (URLError, TimeoutError, ValueError):
        raise SystemExit("Unable to read GitHub release metadata; try again later.") from None


def resolve(releases=()):
    """Pin both heads once, then wait for successful push CI at exactly those commits."""
    if not releases:
        releases = [github(repository + "/commits/main")["sha"] for repository in REPOSITORIES]
    if len(releases) != 2 or any(not re.fullmatch(r"[0-9a-f]{40}", release) for release in releases):
        raise SystemExit("Usage: update-concurro.sh [SERVICE_FULL_SHA WEB_FULL_SHA]")
    pending = dict(zip(REPOSITORIES, releases, strict=True))
    for repository, release in pending.items():
        print(f"Selected {repository}: {release}", file=sys.stderr, flush=True)
    while pending:
        for repository, release in list(pending.items()):
            query = urlencode({"head_sha": release, "event": "push", "branch": "main", "per_page": 1})
            runs = github(repository + "/actions/workflows/ci.yml/runs?" + query)["workflow_runs"]
            if not runs:
                print(f"Waiting for CI to appear: {repository} {release}", file=sys.stderr, flush=True)
                continue
            run = runs[0]
            if run["head_sha"] != release or run["event"] != "push" or run["head_branch"] != "main":
                raise SystemExit(f"GitHub returned CI for a different release: {repository}")
            if run["status"] != "completed":
                print(f"Waiting for CI: {run['html_url']}", file=sys.stderr, flush=True)
                continue
            if run["conclusion"] != "success":
                raise SystemExit(f"CI {run['conclusion']}: {run['html_url']}. No deployment started.")
            del pending[repository]
        if pending:
            time.sleep(30)
    return releases


if __name__ == "__main__":
    try:
        print(*resolve(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit("Update cancelled before deployment.") from None
