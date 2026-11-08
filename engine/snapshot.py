"""Validate the immutable repository references sent to a native runner turn."""

import re
from policy import Blocked


def inspection(repository, candidate, stage, base_branch):
    """The snapshot a call reads: controller-supplied refs, and the candidate for develop and local review."""
    if not isinstance(repository, dict) or set(repository) != {"refs", "head"}:
        raise Blocked("repository inspection requires pinned refs")
    refs, head = repository["refs"], repository["head"]
    if (
        not isinstance(refs, dict)
        or head not in refs
        or not all(isinstance(c, str) and re.fullmatch(r"[a-f0-9]{40}", c) for c in refs.values())
    ):
        raise Blocked("repository inspection requires pinned refs")
    if stage not in ("intake", "plan", "plan_qc", "develop", "review"):
        raise Blocked("unknown native runner role")
    if candidate is not None and stage != "develop":
        raise Blocked("only implementation may overlay a candidate")
    return {
        "refs": refs,
        "head": head,
        "base_branch": base_branch,
        "candidate": None if candidate is None else {"files": candidate},
    }
