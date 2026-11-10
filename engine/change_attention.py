"""Deterministic visibility for editable workflow and project-policy changes."""

from pathlib import PurePosixPath

import markup

POLICY_NAMES = {"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md", "CODEOWNERS", "GOVERNANCE.md"}
PREVIEW_BYTES = 2000


def paths(changed, config):
    """Select all workflow/platform and configured or conventional policy-document paths."""
    documents = set(config.get("policy_documents", []))
    return sorted(
        {p for p in changed if p.startswith(".github/") or p in documents or PurePosixPath(p).name in POLICY_NAMES}
    )


def notice(changed, config, base, head):
    """Bound display only, keeping an exact count and a pinned full-change link when shortened."""
    selected = paths(changed, config)
    if not selected:
        return ""
    lines, used = [], 0
    for path in selected:
        line = "- " + markup.code(path)
        if used + len(line.encode()) > PREVIEW_BYTES:
            break
        lines.append(line)
        used += len(line.encode())
    if len(lines) < len(selected):
        repository = config.get("fork", config["repository"])
        lines.append(
            f"- {len(selected) - len(lines)} more; "
            + markup.link("complete change", f"https://github.com/{repository}/compare/{base}...{head}")
        )
    return "Workflow and policy changes (" + str(len(selected)) + "):\n\n" + "\n".join(lines)
