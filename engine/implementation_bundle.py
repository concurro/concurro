"""Prove an implementation-instruction upgrade without rebinding design approvals."""

from __future__ import annotations

import hashlib
import re
import subprocess
from typing import TYPE_CHECKING

import protocol
from policy import digest

if TYPE_CHECKING:
    from records import BundleView, TaskState


SKILL = "bundle/skills/concurro-implement/SKILL.md"
HISTORY_LIMIT = 128
RETRY_REASON = (
    "Implementation instructions changed; a writer may use /concurro retry to adopt "
    "the verified implementer-only update without repeating design approval."
)


def matches(bundle: BundleView, approved_bundle: str | None, approved_skill: object) -> bool:
    """Prove every resource except the implementer skill matches the approved bundle."""
    return (
        isinstance(approved_skill, str)
        and re.fullmatch(protocol.DIGEST_PATTERN, approved_skill) is not None
        and digest({**bundle.hashes, SKILL: approved_skill}) == approved_bundle
    )


def accepted(state: TaskState, bundle: BundleView) -> bool:
    """Accept only the exact execution bundle a writer previously adopted."""
    receipt = state.get("implementation_bundle", {})
    return (
        receipt.get("new_bundle") == bundle.identity
        and receipt.get("approved_bundle") == state.get("bundle_id")
        and matches(bundle, state.get("bundle_id"), state.get("implementation_skill"))
    )


def current(state: TaskState, bundle: BundleView) -> bool:
    """Require the adopted execution identity, including after a rollback."""
    if state.get("implementation_bundle"):
        return accepted(state, bundle)
    return state.get("bundle_id") == bundle.identity


def original_skill(state: TaskState, bundle: BundleView) -> str | None:
    """Find a proven original skill hash, using bounded local history for legacy tasks."""
    if "implementation_skill" in state:
        value = state["implementation_skill"]
        return value if matches(bundle, state.get("bundle_id"), value) else None
    # Only this one resource may be substituted. An exact aggregate match proves
    # that shared instructions, schemas, charter and target instructions are unchanged.
    try:
        commits = subprocess.run(
            ["git", "-C", str(bundle.root), "log", f"-{HISTORY_LIMIT}", "--format=%H", "--", SKILL],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
        for commit in commits:
            if re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
                continue
            result = subprocess.run(
                ["git", "-C", str(bundle.root), "show", f"{commit}:{SKILL}"], capture_output=True, text=True, timeout=5
            )
            if result.returncode:
                continue
            value = hashlib.sha256(result.stdout.encode()).hexdigest()
            if matches(bundle, state.get("bundle_id"), value):
                return value
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    return None
