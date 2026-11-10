"""Language-independent path envelopes and generated-file policy."""

from fnmatch import fnmatchcase
from functools import lru_cache

MAX_SCOPE_PATH_CHARACTERS = 200
DEFAULT_LIMITS = {
    "primary_files": None,
    "scope_paths": None,
    "scope_path_characters": MAX_SCOPE_PATH_CHARACTERS,
    "changed_files": None,
    "overlay_files": None,
    "workspace_files": None,
}


def limits(config):
    """Return effective operator limits without modifying the pinned configuration."""
    return {**DEFAULT_LIMITS, **config.get("limits", {})}


def file_count_error(count, plan, config):
    """Identify the authority that refuses an over-limit cumulative candidate."""
    configured = limits(config)["changed_files"]
    if configured is not None and count > configured:
        return f"candidate changes {count} files, over limits.changed_files ({configured})"
    approved = scope_of(plan)["max_files"]
    if approved is not None and count > approved:
        return f"candidate changes {count} files, over the approved budget of {approved}"
    return None


def glob_matches(path, pattern):
    """Match path segments with ** covering zero or more directories."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(maxsize=None)
    def match(index, pattern_index):
        """Memoize wildcard branches within this bounded path comparison."""
        if pattern_index == len(patterns):
            return index == len(parts)
        if patterns[pattern_index] == "**":
            return match(index, pattern_index + 1) or index < len(parts) and match(index + 1, pattern_index)
        return (
            index < len(parts)
            and fnmatchcase(parts[index], patterns[pattern_index])
            and match(index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _strings(value):
    """Return whether a value is a non-empty list of distinct non-empty strings."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, str) and v.strip() for v in value)
        and len(set(value)) == len(value)
    )


def _relative(value):
    """Accept relative repository paths and globs without traversal or absolute roots."""
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(p not in ("", ".", "..") for p in value.split("/"))
    )


def relative_path(value):
    """Expose the repository-relative path predicate without changing its validation rules."""
    return _relative(value)


def scope_pattern(value):
    """Treat a trailing slash as a recursive directory scope, without relaxing path validation."""
    return value + "**" if isinstance(value, str) and value.endswith("/") else value


def scope_of(plan):
    """Return path and file limits, ignoring legacy line budgets without mutating the plan."""
    scope = plan.get("scope")
    if scope is not None:
        return {"paths": [scope_pattern(path) for path in scope["paths"]], "max_files": scope.get("max_files")}
    return {"paths": list(plan["files"]), "max_files": len(plan["files"])}


def in_scope(path, plan):
    """Whether a path lies inside the plan's envelope."""
    return any(glob_matches(path, pattern) for pattern in scope_of(plan)["paths"])


def scope_errors(plan, config=None):
    """Why a plan's `scope` is invalid, or None."""
    bounds = limits(config or {})
    scope = plan.get("scope")
    if scope is None:
        return None
    # Legacy allowances stay bound to existing approvals; new schemas omit them.
    if not isinstance(scope, dict) or set(scope) - {"max_changed_lines", "max_files"} != {"paths"}:
        return "plan scope needs paths"
    if not _strings(scope["paths"]):
        return "plan scope paths must contain distinct non-empty strings"
    maximum = bounds["scope_paths"]
    if maximum is not None and len(scope["paths"]) > maximum:
        return f"plan scope paths exceed limits.scope_paths ({maximum})"
    length = bounds["scope_path_characters"]
    if length is not None and any(len(v) > length for v in scope["paths"]):
        return f"plan scope paths exceed limits.scope_path_characters (at most {length} characters each)"
    if not all(_relative(scope_pattern(v)) for v in scope["paths"]):
        return "plan scope paths must be relative paths, directories or globs without empty, . or .. components"
    if "max_files" in scope and (type(scope["max_files"]) is not int or scope["max_files"] < len(plan["files"])):
        return "plan scope max_files must cover its primary files"
    if not all(in_scope(path, plan) for path in plan["files"]):
        return "plan files must lie inside the plan scope"
    return None


def generated(path, policy):
    """Whether target configuration marks a path as generated (aggregate bounds still apply)."""
    return any(glob_matches(path, pattern) for pattern in policy.get("generated", []))
