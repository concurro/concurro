"""Regenerate readable-rendering previews offline from current renderers and simulated tasks."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from itertools import count
from unittest.mock import patch
from uuid import UUID

from support import CONFIG, ROOT, setUpModule, tearDownModule

from controller import Controller
from fakes import FakeGitHub
from review_fixtures import Reviewers
from test_dashboard_snapshots import DashboardSnapshotTests
from test_pending_reviews import PendingReviewTests
from test_pr_descriptions import COMMIT, DESCRIPTION, DescribedAgents
from test_pr_templates import MAPPING, TEMPLATE
from test_readable_rendering import samples
from test_report_view import views

DIRECTORY = ROOT / "docs/reports/readable-rendering"
PROVENANCE = (
    "Generated offline from current code and simulated fixtures. No live GitHub evidence.\n\n"
    "Regenerate with `python engine/tests/render_readable_previews.py`; add `--check` to detect stale previews."
)


class PreviewClock:
    """Provide fixed receipt timestamps for reproducible fixture operation identities."""

    @staticmethod
    def now(tz=None):
        """Return the preview epoch in the requested timezone."""
        return datetime(2026, 11, 10, tzinfo=timezone.utc).astimezone(tz)


class RepairAgents(Reviewers):
    """Exercise structured descriptions through one requested repair round."""

    def ask(self, provider, stage, context):
        """Add the current description contract to the existing repair fixture."""
        result = super().ask(provider, stage, context)
        if stage == "develop":
            result.update(description=deepcopy(DESCRIPTION), commit=deepcopy(COMMIT))
        return result


def lifecycle(agents, template=False):
    """Run the real controller to handoff over in-memory transports and retain commit messages."""
    config = {**deepcopy(CONFIG), "autonomous": True}
    if template:
        config["pr_template_sections"] = deepcopy(MAPPING)
    github = FakeGitHub(config)
    if template:
        github.trees[github.base][".github/PULL_REQUEST_TEMPLATE.md"] = TEMPLATE
    original, commits = github.repo, []

    def repo(path, method="GET", data=None):
        """Capture publication messages without changing the simulated API response."""
        result = original(path, method, data)
        if path == "git/commits" and method == "POST":
            commits.append(data["message"])
        return result

    with patch.object(github, "repo", side_effect=repo):
        for _ in range(40):
            controller = Controller(github, agents, config)
            controller.advance(7)
            if controller.state and controller.state.get("handoff_ready"):
                reference = {**controller.checkpoints.reference(), "digest": "0" * 64}
                with patch.object(controller.checkpoints, "reference", return_value=reference):
                    dashboard = controller.summary_body()
                return dashboard, github, commits
    raise AssertionError("Preview did not reach handoff: " + str(controller.state.get("reason")))


def document(title, *sections):
    """Wrap verbatim rendered output with reproducible fixture provenance."""
    return "\n\n".join(["# " + title, PROVENANCE, *sections]).rstrip() + "\n"


def render():
    """Render all five preview documents without reading stored golden output."""
    result = {"A": document("Readable report preview", *views().values())}
    fixture = DashboardSnapshotTests()
    fixture.setUp()
    try:
        handoff = fixture.scenarios()["handoff"]
    finally:
        fixture.doCleanups()
    result["B"] = document("Presentation preview", handoff, *samples().values())
    dashboard, _, _ = lifecycle(DescribedAgents())
    pending = PendingReviewTests()
    pending.setUp()
    try:
        assert pending.publish()
        result["C"] = document(
            "Single-review publication preview",
            "## Completed task dashboard\n\n" + dashboard,
            "## Separate finding-publication fixture\n\n" + pending.gh.app_reviews[0]["body"],
            "## Thread comments\n\n" + "\n\n---\n\n".join(c["body"] for c in pending.gh.review_comments),
            "## Report archive\n\nSee [readable archive previews](A.md).",
            "## Validation boundary\n\nUncertain same-ID resubmission stays disabled pending authorized live validation. "
            "All uncertain service writes stop for an operator.",
        )
    finally:
        pending.doCleanups()
    for name, agents, template in (("D", RepairAgents({"claude"}), False), ("E", DescribedAgents(), True)):
        dashboard, github, commits = lifecycle(agents, template)
        sections = [
            "## Issue dashboard\n\n" + dashboard,
            "## PR title\n\n" + github.pr["title"],
            "## PR description\n\n" + github.pr["body"],
            "## Published review\n\n" + github.app_reviews[-1]["body"],
            "## Commit messages\n\n" + "\n\n".join("```text\n" + message + "\n```" for message in commits),
        ]
        if template:
            sections.append("## Report archive\n\nSee [readable archive previews](A.md).")
        else:
            sections.append(
                "## Rollout\n\nUse an idle window or explicit writer `/concurro invalidate` recovery "
                "for the trusted bundle changes."
            )
        result[name] = document(
            "Repository template preview" if template else "Structured description preview", *sections
        )
    return result


def main():
    """Write previews, or fail if the committed/uncommitted documents differ from current output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report stale previews without writing")
    args = parser.parse_args()
    setUpModule()
    try:
        with (
            patch("controller.datetime", PreviewClock),
            patch("conversation.datetime", PreviewClock),
            patch("controller.uuid.uuid4", side_effect=(UUID(int=i) for i in count(1))),
        ):
            documents = render()
    finally:
        tearDownModule()
    stale = []
    for name, body in documents.items():
        path = DIRECTORY / (name + ".md")
        if args.check:
            if not path.exists() or path.read_text() != body:
                stale.append(path.name)
        else:
            path.write_text(body)
    if stale:
        parser.exit(1, "Stale previews: " + ", ".join(stale) + "\n")
    print("A–E previews " + ("match current renderers." if args.check else "regenerated."))


if __name__ == "__main__":
    main()
